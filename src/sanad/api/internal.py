"""Authenticated internal HTTP boundaries, explicitly wired by the app factory."""

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from sanad.channels.telegram.router import TelegramRuntime, route_receipt
from sanad.domain import PatientScope
from sanad.ops.nonce_store import TickVerifier
from sanad.ops.worker import worker_body
from sanad.steward.inline import DeliveryScope, dispatch_inline
from sanad.store.keys import AccountScope, IntakeScope, ScopedKey, digest
from sanad.store.records import InboundReceipt, from_record

logger = logging.getLogger(__name__)


def process_event(runtime: TelegramRuntime, event: dict[str, Any]) -> dict[str, str]:
    if event.get("type") != "process_receipt":
        raise ValueError("unsupported worker event")
    key = ScopedKey.model_validate(event["receipt"])
    result = route_receipt(runtime, key, owner="lambda-worker")
    # Authority comes from the saved receipt and the fresh binding, never the event.
    try:
        row = runtime.store.get(key.scope, "inbound_receipt", key.pk)
        if row is not None:
            receipt = from_record(row, InboundReceipt)
            scopes = [DeliveryScope(key.scope)]
            principal = receipt.principal
            if principal and principal.actor_kind == "doctor" and principal.doctor_id:
                scopes.extend(
                    (
                        DeliveryScope(
                            IntakeScope(doctor_id=principal.doctor_id, intake_id="scribe")
                        ),
                        DeliveryScope(
                            IntakeScope(doctor_id=principal.doctor_id, intake_id=digest(receipt.id))
                        ),
                        DeliveryScope(runtime.accounts.scope, receipt.source_subject),
                    )
                )
            elif isinstance(key.scope, AccountScope):
                scopes = [DeliveryScope(key.scope, receipt.source_subject)]
                auth = runtime.store.authorize(runtime.settings.bot_id, receipt.source_subject)
                if auth.principal.actor_kind == "patient":
                    scopes.append(
                        DeliveryScope(
                            PatientScope(
                                doctor_id=auth.principal.doctor_id or "",
                                patient_id=auth.principal.patient_id or "",
                            )
                        )
                    )
            if (
                result.delivery_patient is not None
                and principal
                and principal.doctor_id == result.delivery_patient.doctor_id
                and (
                    principal.actor_kind == "doctor"
                    or principal.patient_id == result.delivery_patient.patient_id
                )
            ):
                scopes.append(DeliveryScope(result.delivery_patient))
            dispatch_inline(runtime.dispatcher, tuple(scopes))
    except Exception:
        # No exception body, no rollback, no send retry: the minute tick recovers.
        logger.warning("inline_dispatch_failed")
    runtime.count("route_" + result.route)
    logger.info("receipt worker completed status=%s", result.status)
    return {"route": result.route, "status": result.status}


def internal_router(
    verifier: TickVerifier,
    sweep: Callable[[], dict[str, Any]],
    runtime: TelegramRuntime,
) -> APIRouter:
    router = APIRouter()

    @router.post("/internal/tick")
    async def tick(request: Request) -> Any:
        body = await request.body()
        if len(body) > 4096:
            return Response(status_code=401)
        status = await run_in_threadpool(verifier.verify, request.headers, body)
        if status != 200:
            return Response(status_code=status)
        nonce = request.headers["x-sanad-nonce"]
        logger.info("tick accepted nonce=%s", nonce)
        return {"accepted": True, "nonce": nonce, "sweep": await run_in_threadpool(sweep)}

    @router.post("/events")
    async def worker(request: Request) -> Any:
        try:
            body = await request.body()
            if len(body) > 16384:
                return Response(status_code=401)
            import json

            event = json.loads(body)
            payload = worker_body(event)
            status = await run_in_threadpool(verifier.verify, event["headers"], payload)
            if status != 200:
                return Response(status_code=status)
            return await run_in_threadpool(process_event, runtime, event)
        except (ValueError, KeyError, TypeError, AttributeError):
            return Response(status_code=400)

    return router
