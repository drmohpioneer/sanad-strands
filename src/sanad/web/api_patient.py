"""Patient-scoped projections and adapters to the accepted durable text commands."""

import base64
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from sanad.auth.login import LoginService
from sanad.channels.telegram.router import route_receipt
from sanad.concierge.plan import load
from sanad.domain import ObservationRef, PatientScope
from sanad.media.upload import upload_state
from sanad.safety import screen_text, to_incident_facts
from sanad.steward.credential_message import is_credential_message
from sanad.steward.types import records
from sanad.store import keys
from sanad.store.records import (
    InboundReceipt,
    OperationalClock,
    OutboundIntent,
    StoredRecord,
    WebSession,
    from_record,
    to_record,
)
from sanad.web.routes import require_session


class CommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$")


class MessageBody(CommandBody):
    text: str = Field(min_length=1, max_length=4096)


class ReminderBody(CommandBody):
    reminders: Literal["stop", "resume"]


class QuietBody(CommandBody):
    quiet_hours: tuple[
        Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")],
        Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")],
    ]


class ConfirmBody(CommandBody):
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{20,100}$")


def patient_scope(session: WebSession) -> PatientScope:
    return PatientScope(doctor_id=session.doctor_id, patient_id=session.patient_id or "")


def uploads(
    login: LoginService, session: WebSession, receipt_rows: tuple[StoredRecord, ...] | None = None
) -> list[dict[str, str]]:
    scope = patient_scope(session)
    evidence = list(records(login.store, scope, "evidence"))
    result = []
    for row in receipt_rows if receipt_rows is not None else login.store.patient_receipts(scope):
        receipt = from_record(row, InboundReceipt)
        if receipt.source_subject != session.subject or receipt.kind not in {"photo", "document"}:
            continue
        media = login.store.get(scope, "media_work", keys.digest(receipt.id))
        association = str(media.body.get("association_ref", "")) if media else ""
        linked = (
            association.split(":", 1)[1]
            if association.startswith(("evidence:", "duplicate:"))
            else None
        )
        candidates = [
            e
            for e in evidence
            if e.body.get("observation_id") == receipt.id
            or linked is not None
            and e.body.get("evidence_id") == linked
        ]
        latest = max(candidates, key=lambda e: e.version) if candidates else None
        result.append(
            {
                "id": receipt.id,
                "received_at": receipt.received_at.isoformat(),
                **upload_state(receipt, media, latest),
            }
        )
    return sorted(result, key=lambda r: (r["received_at"], r["id"]), reverse=True)


def submit(
    login: LoginService,
    request: Request,
    session: WebSession,
    body: CommandBody,
    text: str,
    *,
    preference: bool = False,
    token: str | None = None,
) -> dict[str, JsonValue]:
    runtime = request.app.state.telegram
    # Danger precedes ordinary reservation, deduplication and patient work locks.
    verdict = screen_text(text, policy=runtime.safety_policy)
    digest = keys.digest(
        body.model_dump_json()
        + (":confirm" if token else ":preference" if preference else ":message")
    )
    transport = "web-preference" if preference else "web-message"
    transport_key = keys.digest(session.id + ":" + body.command_id + ":" + digest)
    scope = patient_scope(session)
    receipt_id = keys.inbound(transport, keys.digest(transport_key)).pk
    if verdict.level == "danger":
        facts, severity = to_incident_facts(
            verdict, source=ObservationRef(observation_id=receipt_id), policy=runtime.safety_policy
        )
        runtime.urgent.raise_incident(
            scope, facts.unique_source_key, facts.as_payload(), severity, runtime.clock()
        )
    reserved = login.store.reserve_browser_command(session, body.command_id, digest)
    if reserved == "conflict":
        raise HTTPException(409)
    if reserved not in {"created", "existing"}:
        raise HTTPException(401)
    auth = login.store.authorize(session.scope.bot_id, session.subject)
    if (
        auth.principal.actor_kind != "patient"
        or auth.principal.doctor_id != scope.doctor_id
        or auth.principal.patient_id != scope.patient_id
    ):
        raise HTTPException(401)
    now = login.clock()
    payload: dict[str, JsonValue] = {
        "kind": "callback" if token else "text",
        "text": text,
        "web_session_id": session.id,
    }
    if token:
        payload["callback_token_hash"] = keys.digest(token)
    receipt = InboundReceipt(
        id=receipt_id,
        scope=scope,
        transport=transport,
        transport_key=transport_key,
        source_subject=session.subject,
        source_chat=auth.private_chat_id or "",
        channel="web",
        kind="callback" if token else "text",
        payload=payload,
        principal=auth.principal,
        received_at=now,
        created_at=now,
        updated_at=now,
        safety_screen_state="screened",
        safety_policy_version=verdict.policy_version,
        safety_result=verdict.model_dump(mode="json"),
        work_clock=OperationalClock(next_action_at=now, work_lane="ingress"),
    )
    accepted = login.store.accept_inbound(transport_key, to_record(receipt, scope))
    if accepted.record is None or accepted.status not in {"created", "existing"}:
        raise HTTPException(503)
    if verdict.level == "danger":
        from sanad.safety import render_urgent
        from sanad.store.records import Patient

        patient_row = login.store.get(scope, "patient", scope.patient_id)
        assert patient_row is not None
        emergency = render_urgent(
            "patient_emergency",
            language=from_record(patient_row, Patient).language,
            gender="u",
            policy=runtime.safety_policy,
        )
        for row in records(login.store, scope, "outbound_intent"):
            urgent = from_record(row, OutboundIntent)
            if (
                urgent.template_id == "patient_emergency"
                and runtime.dispatcher.origin_receipt_id(urgent) == receipt.id
            ):
                runtime.dispatcher.dispatch_one(row.scoped_key(scope), transport, runtime.clock())
        return {"status": "received", "confirmation_token": None, "emergency": emergency}
    routed = route_receipt(runtime, accepted.record.scoped_key(scope), owner=transport)
    if routed.template_id == "patient_callback_stale":
        raise HTTPException(409)
    confirmation: str | None = None
    for row in records(login.store, scope, "outbound_intent"):
        intent = from_record(row, OutboundIntent)
        if runtime.dispatcher.origin_receipt_id(intent) != receipt.id:
            continue
        if runtime.dispatcher.web_reply(intent):
            runtime.dispatcher.dispatch_one(row.scoped_key(scope), transport, runtime.clock())
        markup = (intent.payload or {}).get("reply_markup")
        if (
            preference
            and not token
            and isinstance(body, ReminderBody)
            and body.reminders == "resume"
            and isinstance(markup, dict)
        ):
            keyboard = markup.get("inline_keyboard")
            if (
                isinstance(keyboard, list)
                and keyboard
                and isinstance(keyboard[0], list)
                and keyboard[0]
                and isinstance(keyboard[0][0], dict)
            ):
                confirmation = str(keyboard[0][0].get("callback_data", ""))
    saved = login.store.get(scope, "inbound_receipt", receipt.id)
    return {
        "status": "accepted" if saved and saved.body.get("state") == "completed" else "received",
        "confirmation_token": confirmation,
    }


def patient_router(login: LoginService) -> APIRouter:
    router = APIRouter()
    guard = require_session("patient")

    @router.get("/api/patient/uploads")
    def list_uploads(session: Annotated[WebSession, Depends(guard)]) -> list[dict[str, str]]:
        return uploads(login, session)[:30]

    @router.get("/api/patient/conversation")
    def conversation(
        session: Annotated[WebSession, Depends(guard)], cursor: str | None = None
    ) -> dict[str, JsonValue]:
        scope = patient_scope(session)
        receipt_rows = login.store.patient_receipts(scope)
        states = {u["id"]: u for u in uploads(login, session, receipt_rows)}
        items: list[dict[str, JsonValue]] = []
        for row in receipt_rows:
            r = from_record(row, InboundReceipt)
            if r.source_subject != session.subject or r.kind not in {"text", "photo", "document"}:
                continue
            items.append(
                {
                    "id": r.id,
                    "at": r.received_at.isoformat(),
                    "direction": "inbound",
                    "text": str((r.payload or {}).get("text", "")),
                    "upload": dict(states[r.id]) if r.id in states else None,
                }
            )
        for row in records(login.store, scope, "outbound_intent"):
            i = from_record(row, OutboundIntent)
            if (
                i.audience == "patient"
                and i.status == "provider_accepted"
                and i.accepted_at
                and i.recipient_subject in {None, session.subject}
                and i.recipient_ref
                == login.store.authorize(session.scope.bot_id, session.subject).private_chat_id
            ):
                credential = is_credential_message(i.template_id, i.delivered_text)
                items.append(
                    {
                        "id": i.id,
                        "at": i.accepted_at.isoformat(),
                        "direction": "outbound",
                        **(
                            {"legacy": False, "credential": True}
                            if credential
                            else {"text": i.delivered_text, "legacy": i.delivered_text is None}
                        ),
                    }
                )
        items.sort(key=lambda r: (str(r["at"]), str(r["id"])))
        if cursor:
            try:
                if len(cursor) > 2048:
                    raise ValueError
                values = json.loads(base64.urlsafe_b64decode(cursor))
                if (
                    not isinstance(values, list)
                    or len(values) != 3
                    or values[0] != keys.digest(session.id)
                    or not all(isinstance(v, str) for v in values)
                ):
                    raise ValueError
                items = [r for r in items if (str(r["at"]), str(r["id"])) < (values[1], values[2])]
            except (ValueError, TypeError):
                raise HTTPException(400) from None
        page = items[-50:]
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps([keys.digest(session.id), page[0]["at"], page[0]["id"]]).encode()
            ).decode()
            if len(items) > 50
            else None
        )
        return {"items": list(page), "cursor": next_cursor}

    @router.post("/api/patient/messages")
    def message(
        body: MessageBody, request: Request, session: Annotated[WebSession, Depends(guard)]
    ) -> dict[str, JsonValue]:
        if not body.text.strip():
            raise HTTPException(422)
        return submit(login, request, session, body, body.text)

    @router.get("/api/patient/preferences")
    def preferences(session: Annotated[WebSession, Depends(guard)]) -> dict[str, JsonValue]:
        snap = load(login.store, patient_scope(session), login.clock())
        if snap is None:
            raise HTTPException(401)
        return {
            "reminders": "enabled"
            if snap.consent.routine_contact_enabled and snap.patient.contact_status == "active"
            else "paused",
            "quiet_hours": list(snap.consent.quiet_hours),
            "timezone": snap.patient.timezone,
            "language": snap.patient.language,
        }

    @router.post("/api/patient/preferences")
    def preference(
        body: ReminderBody | QuietBody,
        request: Request,
        session: Annotated[WebSession, Depends(guard)],
    ) -> dict[str, JsonValue]:
        if isinstance(body, QuietBody) and body.quiet_hours[0] == body.quiet_hours[1]:
            raise HTTPException(422)
        text = (
            body.reminders + " reminders"
            if isinstance(body, ReminderBody)
            else "quiet hours " + " ".join(body.quiet_hours)
        )
        return submit(login, request, session, body, text, preference=True)

    @router.post("/api/patient/preferences/confirm")
    def confirm(
        body: ConfirmBody, request: Request, session: Annotated[WebSession, Depends(guard)]
    ) -> dict[str, JsonValue]:
        action = login.store.get(patient_scope(session), "patient_action", keys.digest(body.token))
        if (
            action is None
            or action.body.get("actor_subject") != session.subject
            or action.body.get("action") != "resume"
        ):
            raise HTTPException(409)
        from sanad.concierge.records import PatientAction

        offer = from_record(action, PatientAction)
        digest = keys.digest(body.model_dump_json() + ":confirm")
        transport_key = keys.digest(session.id + ":" + body.command_id + ":" + digest)
        replay = login.store.get(
            patient_scope(session),
            "inbound_receipt",
            keys.inbound("web-preference", keys.digest(transport_key)).pk,
        )
        profile = login.store.get_patient_profile(patient_scope(session))
        if replay is None and (
            offer.consumed_at
            or offer.expires_at <= login.clock()
            or profile is None
            or offer.delivery_epoch != profile.delivery_epoch
            or offer.binding_epoch != profile.binding_epoch
            or offer.consent_version != profile.consent_version
        ):
            raise HTTPException(409)
        return submit(login, request, session, body, "", preference=True, token=body.token)

    return router
