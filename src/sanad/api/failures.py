"""Public request failure taxonomy; no identifiers or exception content in logs."""

import asyncio
import logging
from typing import Literal

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sanad.store.records import AuthorizationUnavailable
from sanad.store.retry import BACKOFF, transient_conflict

REASONS = {
    "store_busy": "The service is busy. Try again in a moment.",
    "authorization_unavailable": "Access could not be checked; please try again.",
    "receipt_persist_failed": "Not received. Try again.",
    "media_storage_unavailable": "The file is unavailable; please try again.",
    "ingress_conflict": "The request could not be saved; please try again.",
    "ingress_exception": "The request could not be received; please try again.",
    "configuration": "The service is starting; please try again.",
    "unhandled": "The request could not be completed; please try again.",
}
type Reason = Literal[
    "store_busy",
    "authorization_unavailable",
    "receipt_persist_failed",
    "media_storage_unavailable",
    "ingress_conflict",
    "ingress_exception",
    "configuration",
    "unhandled",
]


def store_busy(error: BaseException) -> bool:
    from botocore.exceptions import ClientError  # type: ignore[import-untyped]

    if not isinstance(error, ClientError):
        return False
    code = error.response.get("Error", {}).get("Code")
    transient = {
        "ThrottlingException",
        "ProvisionedThroughputExceededException",
        "ProvisionedThroughputExceeded",  # Transaction cancellation reason.
        "ThrottlingError",  # Transaction cancellation reason on on-demand tables.
        "InternalServerError",
        "ServiceUnavailable",
        "Throttling",
        "SlowDown",
        "InternalError",
        "RequestLimitExceeded",
    }
    reasons = {r.get("Code") for r in error.response.get("CancellationReasons", [])}
    return (
        code in transient
        or (code == "TransactionCanceledException" and bool(reasons & transient))
        or error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) >= 500
    )


class RequestFailure(Exception):
    def __init__(self, reason: Reason):
        self.reason = reason
        super().__init__(reason)


def response(reason: Reason) -> JSONResponse:
    return JSONResponse(
        {"reason": reason, "detail": REASONS[reason]},
        status_code=503 if reason == "configuration" else 500 if reason == "unhandled" else 409,
    )


def route_family(path: str) -> str:
    if path == "/tg":
        return "telegram"
    if path == "/events":
        return "worker"
    if path.startswith("/internal/"):
        return "tick"
    if path.startswith(("/pl/", "/d/", "/ad/")):
        return "login"
    if path.startswith(("/api/patient/", "/pp")):
        return "patient"
    if path.startswith(("/admin", "/api/admin/")):
        return "admin"
    return "web"


def exception_metadata(error: BaseException | None) -> str:
    if error is None:
        return "exception_class=unknown module=unknown function=unknown"
    frame = error.__traceback__
    while frame and frame.tb_next:
        frame = frame.tb_next
    module = frame.tb_frame.f_globals.get("__name__", "unknown") if frame else "unknown"
    function = frame.tb_frame.f_code.co_name if frame else "unknown"
    return f"exception_class={type(error).__name__} module={module} function={function}"


def log_failure(reason: str, family: str, error: BaseException | None = None) -> None:
    if reason == "unhandled":
        logging.getLogger("sanad.api.lambda_entry").warning(
            "request_failed reason=%s route_family=%s %s", reason, family, exception_metadata(error)
        )
        return
    logging.getLogger("sanad.api.lambda_entry").warning(
        "request_failed reason=%s route_family=%s", reason, family
    )


class RequestFailures:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracked(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        for attempt in range(4):
            try:
                await self.app(scope, receive, tracked)
                return
            except Exception as error:
                if started:
                    raise
                conflict = transient_conflict(error)
                if conflict and scope["method"] == "GET" and attempt < 3:
                    await asyncio.sleep(BACKOFF[attempt])
                    continue
                reason: Reason = (
                    error.reason
                    if isinstance(error, RequestFailure)
                    else "store_busy"
                    if store_busy(error)
                    else "authorization_unavailable"
                    if isinstance(error, AuthorizationUnavailable)
                    else "ingress_conflict"
                    if conflict
                    else "unhandled"
                )
                log_failure(reason, route_family(scope["path"]), error)
                await response(reason)(scope, receive, send)
                return
