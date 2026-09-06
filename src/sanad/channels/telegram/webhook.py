"""Verify transport, screen full permitted text, and persist before HTTP acknowledgment."""

import hmac
import logging
from collections.abc import Callable

from fastapi import APIRouter, Request, Response
from pydantic import JsonValue, ValidationError
from starlette.concurrency import run_in_threadpool

from sanad.channels.telegram.router import TelegramRuntime
from sanad.channels.telegram.update import TelegramUpdate
from sanad.domain import PatientScope, TenantScope
from sanad.safety import screen_text
from sanad.store import keys
from sanad.store.keys import AccountScope, Scope, ScopedKey
from sanad.store.records import InboundReceipt, OperationalClock, to_record

logger = logging.getLogger(__name__)


def telegram_router(
    runtime: TelegramRuntime | None,
    *,
    process_receipts: bool = True,
    receipt_submit: Callable[[ScopedKey], None] | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/tg")
    async def telegram(request: Request) -> Response:
        if runtime is None:
            return Response(status_code=503)
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(
            supplied.encode(), runtime.settings.webhook_secret.get_secret_value().encode()
        ):
            runtime.count("unauthorized")
            logger.warning("telegram webhook authentication refused")
            return Response(status_code=401)
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 65536:
                return Response(status_code=400)
        try:
            update = TelegramUpdate.model_validate_json(bytes(body))
        except (ValueError, ValidationError):
            return Response(status_code=400)
        message, callback = update.message, update.callback_query
        sender = message.sender if message else callback.sender if callback else None
        chat = (
            message.chat
            if message
            else callback.message.chat
            if callback and callback.message
            else None
        )
        reason = None
        if update.bot_id is not None and update.bot_id != runtime.settings.bot_id:
            reason = "other_bot"
        elif message and message.via_bot and message.via_bot.id != runtime.settings.bot_id:
            reason = "other_bot"
        elif sender is None or sender.id is None:
            reason = "missing_sender"
        elif sender.is_bot:
            reason = "bot_sender"
        elif chat is None or chat.type != "private":
            reason = "non_private"
        elif str(chat.id) != sender.id:
            reason = "chat_mismatch"
        if reason:
            runtime.count("dropped_" + reason)
            logger.info("telegram update dropped: %s", reason)
            return Response(status_code=200)
        assert sender and sender.id and chat
        text = message.readable_text if message else ""
        try:
            # No identity lookup, role routing or ordinary lock precedes this call.
            verdict = screen_text(text, policy=runtime.safety_policy)
            auth = runtime.store.authorize(runtime.settings.bot_id, sender.id)
            principal = auth.principal
            scope: Scope = AccountScope(bot_id=runtime.settings.bot_id)
            if principal.actor_kind == "patient" and principal.doctor_id and principal.patient_id:
                candidate = PatientScope(
                    doctor_id=principal.doctor_id, patient_id=principal.patient_id
                )
                profile = runtime.store.get_patient_profile(candidate)
                if (
                    profile is not None
                    and profile.recipient_subject == sender.id
                    and profile.recipient_ref == str(chat.id)
                ):
                    scope = candidate
            elif "doctor" in principal.verified_roles and principal.doctor_id:
                scope = TenantScope(doctor_id=principal.doctor_id)
            payload: dict[str, JsonValue] = {"kind": message.kind if message else "callback"}
            handle = None
            if message:
                payload.update(text=text, message_id=message.message_id)
                parts = text.strip().split(maxsplit=1)
                if len(parts) == 2 and parts[0] == "/start":
                    payload.update(text="/start <redacted>", invitation_hash=keys.digest(parts[1]))
                handle = message.media.file_id if message.media else None
                if handle:
                    payload["media_handle"] = handle
            elif callback:
                payload.update(
                    callback_query_id=callback.id, callback_token_hash=keys.digest(callback.data)
                )
            transport_key = f"{runtime.settings.bot_id}:{update.update_id}"
            now = runtime.clock()
            receipt = InboundReceipt(
                id=keys.inbound("telegram", keys.digest(transport_key)).pk,
                scope=scope,
                transport="telegram",
                transport_key=transport_key,
                source_subject=sender.id,
                source_chat=str(chat.id),
                channel="telegram",
                principal=principal,
                kind=str(payload["kind"]),
                payload=payload,
                provider_media_handle=handle,
                received_at=now,
                created_at=now,
                updated_at=now,
                work_clock=OperationalClock(next_action_at=now, work_lane="ingress"),
                safety_screen_state="screened",
                safety_policy_version=verdict.policy_version,
                safety_result=verdict.model_dump(mode="json"),
            )
            accepted = runtime.store.accept_inbound(transport_key, to_record(receipt, scope))
        except Exception:
            runtime.count("store_unavailable")
            logger.warning("telegram receipt unavailable")
            return Response(status_code=503)
        if accepted.status not in {"created", "existing"} or accepted.record is None:
            runtime.count("receipt_conflict")
            return Response(status_code=503)
        if process_receipts and receipt_submit is not None and accepted.state != "completed":
            saved = InboundReceipt.model_validate(accepted.record.body)
            try:
                await run_in_threadpool(receipt_submit, accepted.record.scoped_key(saved.scope))
            except Exception:
                runtime.count("processing_failure")
                logger.warning("telegram hand-off deferred; durable receipt retained")
        return Response(status_code=200)

    return router
