"""Patient-scoped projections and adapters to the accepted durable text commands."""

import base64
import json
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from sanad.auth.login import LoginService
from sanad.channels.telegram.router import route_receipt
from sanad.domain import ObservationRef, PatientScope
from sanad.media.upload import upload_state
from sanad.safety import screen_text, to_incident_facts
from sanad.steward.credential_message import is_credential_message
from sanad.steward.types import bounded_records as records
from sanad.store import keys
from sanad.store.records import (
    Consent,
    Cursor,
    InboundReceipt,
    OperationalClock,
    OutboundIntent,
    Patient,
    PatientClaim,
    StoredRecord,
    WebSession,
    from_record,
    item_record,
)
from sanad.web.receipts import persist
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
    for row in (
        receipt_rows
        if receipt_rows is not None
        else login.store.patient_receipts(scope, limit=30, kinds=("photo", "document"))[0]
    ):
        receipt = from_record(row, InboundReceipt)
        if receipt.source_subject != session.subject or receipt.kind not in {"photo", "document"}:
            continue
        media = item_record(row.media_snapshot) if row.media_snapshot else None
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
    accepted = persist(login.store, receipt)
    assert accepted.record is not None
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
    for row in receipt_replies(login, scope, receipt.id):
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
    completed = bool(saved and saved.body.get("state") == "completed")
    result: dict[str, JsonValue] = {
        "status": "accepted" if completed else "received",
        "confirmation_token": confirmation,
    }
    if preference:
        result.update(queued=not completed, token=receipt.id)
        if completed:
            result["preferences"] = preference_state(login, session)
    return result


def receipt_replies(
    login: LoginService, scope: PatientScope, receipt_id: str
) -> tuple[StoredRecord, ...]:
    # PatientTurn uses one exact reply key, or its deterministic refusal key.
    rows = []
    for suffix in ("", ":refused"):
        id = keys.digest(f"patient-turn:{receipt_id}{suffix}|patient|solicited_reply|")
        row = login.store.get(scope, "outbound_intent", id)
        if row:
            rows.append(row)
    return tuple(rows)


def preference_state(login: LoginService, session: WebSession) -> dict[str, JsonValue]:
    # Preferences need only three point reads; no care-plan projection on a poll.
    from sanad.store.records import Consent, Patient

    scope = patient_scope(session)
    patient = login.store.get(scope, "patient", scope.patient_id)
    if patient is None:
        raise HTTPException(401)
    p = from_record(patient, Patient)
    binding = login.store.get(scope, "patient_binding", p.active_binding_id or "")
    consent = (
        login.store.get(scope, "consent", str(binding.body["consent_id"])) if binding else None
    )
    if consent is None:
        raise HTTPException(401)
    c = from_record(consent, Consent)
    return {
        "reminders": "enabled"
        if c.routine_contact_enabled and p.contact_status == "active"
        else "paused",
        "quiet_hours": list(c.quiet_hours),
        "timezone": p.timezone,
        "language": p.language,
    }


def patient_router(login: LoginService) -> APIRouter:
    router = APIRouter()
    guard = require_session("patient")

    def decode_cursor(session: WebSession, value: str | None, family: str) -> list[Cursor | None]:
        if not value:
            return [None, None]
        try:
            if len(value) > 4096:
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(value))
            if (
                not isinstance(data, list)
                or len(data) != 4
                or data[:2] != [keys.digest(session.id), family]
            ):
                raise ValueError
            return [Cursor.model_validate(v) if v else None for v in data[2:]]
        except (ValueError, TypeError):
            raise HTTPException(400) from None

    def encode_cursor(session: WebSession, family: str, positions: list[Cursor | None]) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(
                [
                    keys.digest(session.id),
                    family,
                    *[c.model_dump() if c else None for c in positions],
                ]
            ).encode()
        ).decode()

    @router.get("/api/patient/agreement")
    def agreement(
        request: Request, session: Annotated[WebSession, Depends(guard)]
    ) -> dict[str, JsonValue]:
        if request.query_params:
            raise HTTPException(400)
        scope = patient_scope(session)
        row = login.store.get(scope, "patient", scope.patient_id)
        if row is None:
            raise HTTPException(401)
        patient = from_record(row, Patient)
        row = login.store.get(scope, "consent", patient.consent_id or "")
        if row is None:
            raise HTTPException(401)
        consent = from_record(row, Consent)
        text = None
        if consent.offer_claim_id:
            saved = login.store.get(session.scope, "patient_claim", consent.offer_claim_id)
            pending = from_record(saved, PatientClaim) if saved else None
            if (
                pending
                and pending.doctor_id == scope.doctor_id
                and pending.patient_id == scope.patient_id
                and pending.candidate_subject == session.subject
            ):
                offer = next(
                    (
                        o
                        for o in pending.consent_offers
                        if o.generation == consent.offer_generation
                        and o.digest == consent.policy_digest
                    ),
                    None,
                )
                text = offer.full_text if offer else None
        return {
            "version": consent.policy_text_version,
            "accepted_at": consent.accepted_at.isoformat(),
            "text": text,
        }

    @router.get("/api/patient/uploads")
    def list_uploads(
        session: Annotated[WebSession, Depends(guard)], cursor: str | None = None
    ) -> dict[str, JsonValue]:
        position = decode_cursor(session, cursor, "uploads")[0]
        rows, next_position = login.store.patient_receipts(
            patient_scope(session), position, limit=30, kinds=("photo", "document")
        )
        return {
            "items": [dict(item) for item in uploads(login, session, rows)],
            "cursor": encode_cursor(session, "uploads", [next_position, None])
            if next_position
            else None,
        }

    @router.get("/api/patient/conversation")
    def conversation(
        session: Annotated[WebSession, Depends(guard)], cursor: str | None = None
    ) -> dict[str, JsonValue]:
        scope = patient_scope(session)
        positions = decode_cursor(session, cursor, "conversation")
        inbound, next_in = login.store.patient_receipts(scope, positions[0], limit=50)
        outbound, next_out = login.store.patient_timeline(
            scope, "CONVERSATION#", positions[1], limit=50
        )
        states = {u["id"]: u for u in uploads(login, session, inbound)}
        items: list[tuple[dict[str, JsonValue], int, StoredRecord]] = []
        chat = login.store.authorize(session.scope.bot_id, session.subject).private_chat_id
        for row in inbound:
            r = from_record(row, InboundReceipt)
            if r.source_subject == session.subject and r.kind in {"text", "photo", "document"}:
                items.append(
                    (
                        {
                            "id": r.id,
                            "at": keys.instant(r.received_at),
                            "direction": "inbound",
                            "text": str((r.payload or {}).get("text", "")),
                            "upload": dict(states[r.id]) if r.id in states else None,
                        },
                        0,
                        row,
                    )
                )
        for row in outbound:
            i = from_record(row, OutboundIntent)
            if (
                i.audience == "patient"
                and i.status == "provider_accepted"
                and i.accepted_at
                and i.recipient_subject in {None, session.subject}
                and i.recipient_ref == chat
            ):
                credential = is_credential_message(i.template_id, i.delivered_text)
                items.append(
                    (
                        {
                            "id": i.id,
                            "at": keys.instant(i.accepted_at),
                            "direction": "outbound",
                            **(
                                {"legacy": False, "credential": True}
                                if credential
                                else {"text": i.delivered_text, "legacy": i.delivered_text is None}
                            ),
                        },
                        1,
                        row,
                    )
                )
        items.sort(key=lambda r: (str(r[0]["at"]), str(r[0]["id"])), reverse=True)
        # A source with filtered-out rows may hide newer visible history on its
        # next page. Do not pass its evaluated boundary in the other source.
        boundaries = [
            (keys.instant(datetime.fromisoformat(str(rows[-1].body[field]))), rows[-1].id)
            for rows, next_key, field in (
                (inbound, next_in, "received_at"),
                (outbound, next_out, "accepted_at"),
            )
            if rows and next_key is not None
        ]
        boundary = max(boundaries) if boundaries else None
        page = [
            item
            for item in items
            if boundary is None or (str(item[0]["at"]), str(item[0]["id"])) >= boundary
        ][:50]
        # Advance each source only through consumed entries. Unselected tail rows
        # stay on the next page even when the other source filled this page.
        next_positions = [next_in, next_out]
        for source, _rows, prefix in ((0, inbound, "RECEIPT#"), (1, outbound, "CONVERSATION#")):
            remaining = [r for r in items[len(page) :] if r[1] == source]
            if remaining:
                selected = [r for r in page if r[1] == source]
                if selected:
                    row = selected[-1][2]
                    at = row.body["received_at" if source == 0 else "accepted_at"]
                    from sanad.store._base import query_identity

                    pk = keys.partition(scope)
                    next_positions[source] = Cursor(
                        query=query_identity(pk, None, prefix, None) + ":desc",
                        position={
                            "PK": pk,
                            "SK": keys.timeline(
                                scope, prefix, datetime.fromisoformat(str(at)), row.id
                            ).sk,
                        },
                    )
                else:
                    next_positions[source] = positions[source]
            elif next_positions[source] is None:
                # Exhausted source sentinel prevents restarting it on later pages.
                from sanad.store._base import query_identity

                pk = keys.partition(scope)
                next_positions[source] = Cursor(
                    query=query_identity(pk, None, prefix, None) + ":desc",
                    position={"PK": pk, "SK": prefix},
                )
        more = len(items) > len(page) or next_in is not None or next_out is not None
        return {
            "items": [i[0] for i in reversed(page)],
            "cursor": encode_cursor(session, "conversation", next_positions) if more else None,
        }

    @router.post("/api/patient/messages")
    def message(
        body: MessageBody, request: Request, session: Annotated[WebSession, Depends(guard)]
    ) -> dict[str, JsonValue]:
        if not body.text.strip():
            raise HTTPException(422)
        return submit(login, request, session, body, body.text)

    @router.get("/api/patient/preferences")
    def preferences(
        request: Request, session: Annotated[WebSession, Depends(guard)], token: str | None = None
    ) -> dict[str, JsonValue]:
        result = preference_state(login, session)
        if token:
            receipt = login.store.get(patient_scope(session), "inbound_receipt", token)
            payload = receipt.body.get("payload") if receipt else None
            if (
                receipt is None
                or receipt.body.get("transport") != "web-preference"
                or not isinstance(payload, dict)
                or payload.get("web_session_id") != session.id
            ):
                raise HTTPException(404)
            result["queued"] = receipt.body.get("state") != "completed"
            if not result["queued"]:
                for row in receipt_replies(login, patient_scope(session), token):
                    intent = from_record(row, OutboundIntent)
                    if request.app.state.telegram.dispatcher.origin_receipt_id(intent) != token:
                        continue
                    markup = (intent.payload or {}).get("reply_markup")
                    if isinstance(markup, dict):
                        keyboard = markup.get("inline_keyboard")
                        if (
                            isinstance(keyboard, list)
                            and keyboard
                            and isinstance(keyboard[0], list)
                            and keyboard[0]
                            and isinstance(keyboard[0][0], dict)
                        ):
                            result["confirmation_token"] = keyboard[0][0].get("callback_data")
        return result

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
