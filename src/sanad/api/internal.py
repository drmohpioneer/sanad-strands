"""Authenticated internal HTTP boundaries, explicitly wired by the app factory."""

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from sanad.api.failures import RequestFailure, store_busy
from sanad.channels.telegram.router import RouteResult, TelegramRuntime, route_receipt
from sanad.domain import PatientScope
from sanad.ops.nonce_store import TickVerifier
from sanad.ops.worker import worker_body
from sanad.steward.inline import DeliveryScope, dispatch_inline
from sanad.store.keys import AccountScope, IntakeScope, ScopedKey, digest
from sanad.store.records import AuthorizationUnavailable, Claim, InboundReceipt, from_record
from sanad.store.retry import transient_conflict

logger = logging.getLogger(__name__)


def complete_refusal(
    runtime: TelegramRuntime, key: ScopedKey, claims: list[Claim], code: str
) -> None:
    row = runtime.store.get(key.scope, "inbound_receipt", key.pk)
    if row is None:
        return
    receipt = from_record(row, InboundReceipt)
    if receipt.state in {"completed", "needs_attention"}:
        return
    claim = next((c for c in reversed(claims) if c.record_key == key), None)
    if claim is None:
        claim = runtime.store.claim_work(
            key,
            receipt.version,
            "lambda-worker",
            runtime.clock(),
            runtime.accounts.policy.operations.claim_ttl,
        )
    if claim is None:
        return
    row = runtime.store.get(key.scope, "inbound_receipt", key.pk)
    if row is None:
        return
    receipt = from_record(row, InboundReceipt)
    if not receipt.processing_claim or (
        receipt.processing_claim.generation != claim.generation
        or receipt.processing_claim.owner != claim.owner
    ):
        return
    if isinstance(key.scope, PatientScope):
        from sanad.concierge.plan import authorized
        from sanad.steward.patient import PatientTurnCommit
        from sanad.steward.types import CommandResult

        auth = runtime.store.authorize(runtime.settings.bot_id, receipt.source_subject)
        snapshot = (
            authorized(runtime.store, auth.principal, auth.binding, runtime.clock())
            if auth.binding
            else None
        )
        if snapshot is None:
            return
        lease = runtime.store.acquire_patient(
            key.scope,
            "worker-refusal",
            runtime.clock(),
            runtime.steward.policy_provider(key.scope).operations.lease_ttl,
        )
        if lease is None:
            return
        try:
            tx = PatientTurnCommit(runtime.steward, snapshot, receipt, auth.principal, claim, lease)
            tx.refuse(CommandResult(status="forbidden", reason_code=code))
        finally:
            runtime.store.release_patient(lease)
    else:
        runtime.accounts.finish_receipt(
            receipt,
            claim,
            result_code=code,
            template_id="worker_refused",
            text="This request could not be completed. Please try again.",
        )


def run_receipt(runtime: TelegramRuntime, key: ScopedKey) -> RouteResult:
    from sanad.steward.apply import EffectsRejected
    from sanad.steward.reviews import ReviewRefused
    from sanad.steward.service import InvalidCommandPayload
    from sanad.store.claims import issued

    claims: list[Claim] = []
    token = issued.set(claims)
    try:
        try:
            result = route_receipt(runtime, key, owner="lambda-worker")
        except (EffectsRejected, ReviewRefused, InvalidCommandPayload):
            result = RouteResult(route="refused", status="invalid_input")
        if result.status in {
            "forbidden",
            "invalid_input",
            "invalid_action",
            "unsupported",
            "needs_confirmation",
            "binding_changed",
            "authority_changed",
        }:
            complete_refusal(runtime, key, claims, result.status)
        return result
    except Exception as error:
        if store_busy(error):
            return RouteResult(route="busy", status="store_busy")
        if isinstance(error, AuthorizationUnavailable):
            raise RequestFailure("authorization_unavailable") from None
        if transient_conflict(error):
            raise RequestFailure("ingress_conflict") from None
        raise
    finally:
        try:
            for claim in reversed(claims):
                try:
                    runtime.store.defer_inbound(claim, runtime.clock())
                except Exception as error:
                    if not store_busy(error):
                        raise
                    # Durable claim expiry owns recovery if the store is still busy.
                    pass
        finally:
            issued.reset(token)


def process_event(runtime: TelegramRuntime, event: dict[str, Any]) -> dict[str, str]:
    if event.get("type") != "process_receipt":
        raise ValueError("unsupported worker event")
    key = ScopedKey.model_validate(event["receipt"])
    result = run_receipt(runtime, key)
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


def run_sweep(runtime: TelegramRuntime, sweep: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Release unfinished receipts claimed by this tick, including handled sweep conflicts."""
    from sanad.store.claims import issued

    claims: list[Claim] = []
    token = issued.set(claims)
    try:
        return sweep()
    finally:
        try:
            for claim in reversed(claims):
                try:
                    runtime.store.defer_inbound(claim, runtime.clock())
                except Exception as error:
                    if not store_busy(error):
                        raise
                    # Durable claim expiry owns recovery if the store is still busy.
                    pass
        finally:
            issued.reset(token)


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
        from sanad.steward.apply import EffectsRejected
        from sanad.steward.reviews import ReviewRefused
        from sanad.steward.service import InvalidCommandPayload

        try:
            result = await run_in_threadpool(run_sweep, runtime, sweep)
        except (EffectsRejected, ReviewRefused, InvalidCommandPayload):
            raise RequestFailure("ingress_exception") from None
        logger.info(
            "tick result skipped=%s oldest_due=%s lane_capped=%s",
            result.get("skipped", {}),
            result.get("oldest_due", {}),
            result.get("lane_capped", []),
        )
        return {"accepted": True, "nonce": nonce, "sweep": result}

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
        except (ValueError, KeyError, TypeError, AttributeError):
            return Response(status_code=400)
        return await run_in_threadpool(process_event, runtime, event)

    return router
