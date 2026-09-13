"""Exchange-path redaction and restrictive browser response headers."""

import logging
import re

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_PATH = re.compile(r"/(ad|d|pl|p)/[^\s?\#\"'<>]+")
_START = re.compile(r"([?&]start=)[^\s&#\"'<>]+")
HEADERS = {
    # "no-referrer" makes browsers send "Origin: null" on same-origin form posts
    # (Fetch standard), which the exchange's same-origin check refuses; "same-origin"
    # still sends nothing to other sites, so exchange paths never leave this host.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "style-src 'self'; font-src 'self'; script-src 'self'; img-src 'self' data:; "
        "connect-src 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
}


class SessionRefused(Exception):
    """A session refusal with a safe public reason, handled on pages and APIs."""

    def __init__(self, reason: str, status_code: int = 401):
        self.reason, self.status_code = reason, status_code
        super().__init__(reason)


def redact(text: str) -> str:
    return _START.sub(r"\1<redacted>", _PATH.sub(r"/\1/<redacted>", text))


class ExchangeRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg, record.args = redact(message), ()
        if record.msg != message and record.exc_info:
            record.exc_info, record.exc_text = None, None
        return True


_REDACTOR = ExchangeRedactor()


def install_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error", "httpx", "sanad.web"):
        logger = logging.getLogger(name)
        if _REDACTOR not in logger.filters:
            logger.addFilter(_REDACTOR)


class BrowserSecurity:
    def __init__(self, app: ASGIApp):
        self.app = app
        install_redaction()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        request = Request(scope)
        login = getattr(request.app.state, "login", None)
        static_demo = request.method == "GET" and (
            path in {"/demo", "/demo/patient", "/demo/admin"}
            or re.fullmatch(r"/assets/[^/]+", path) is not None
        )
        if login is not None and not static_demo:
            session = login.session(request.cookies.get("sanad_session", ""))
            if session is not None and session.role == "admin":
                method = request.method
                public = (
                    method == "GET"
                    and (path in {"/health", "/demo"} or re.fullmatch(r"/(?:assets|p)/[^/]+", path))
                    or method in {"GET", "POST"}
                    and re.fullmatch(r"/(?:ad|d|pl)/[^/]+", path)
                )
                administrator = (
                    method == "GET"
                    and path in {"/admin", "/api/admin/applications"}
                    or method == "POST"
                    and (
                        path == "/api/admin/logout"
                        or re.fullmatch(r"/api/admin/applications/[^/]+/(?:approve|reject)", path)
                        or re.fullmatch(r"/api/admin/doctors/[^/]+/(?:suspend|reinstate)", path)
                    )
                )
                if not public and not administrator:
                    login.revoke(session, reason="admin_wrong_role", path=redact(path))
                    from sanad.web.pages import admin_denied_page

                    message = "Not available from an administrator session."
                    response = (
                        JSONResponse({"detail": message}, status_code=403, headers=HEADERS)
                        if path.startswith("/api/")
                        else HTMLResponse(admin_denied_page(), status_code=403, headers=HEADERS)
                    )
                    await response(scope, receive, send)
                    return
        browser = path not in {"/health", "/tg"}

        async def guarded_send(message: Message) -> None:
            if message["type"] == "http.response.start" and browser:
                message["headers"] = [
                    *message.get("headers", []),
                    *((k.lower().encode(), v.encode()) for k, v in HEADERS.items()),
                ]
                logging.getLogger("sanad.web").info(
                    "%s %s %s", scope.get("method"), redact(path), message["status"]
                )
            await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except SessionRefused as error:
            if error.status_code >= 500:
                from sanad.api.failures import RequestFailure

                raise RequestFailure("authorization_unavailable") from None
            from sanad.web.pages import LOGIN_REFUSALS, refused_page
            from sanad.web.routes import clear_cookies

            response = (
                JSONResponse(
                    {"detail": LOGIN_REFUSALS[error.reason]}, status_code=error.status_code
                )
                if path.startswith("/api/")
                else HTMLResponse(refused_page(error.reason), status_code=error.status_code)
            )
            # The revoked credential grants no access. Retain it for an authority
            # refusal so parallel patient requests can all recover the same reason;
            # expiry, logout and session replacement still clear their cookies.
            if error.status_code == 401 and error.reason != "patient_access_changed":
                clear_cookies(response)
            await response(scope, receive, guarded_send)
