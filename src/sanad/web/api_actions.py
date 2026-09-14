"""Patient record actions through the existing scoped, fenced commands."""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from secrets import token_urlsafe
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from sanad.auth.claim import ClaimService
from sanad.domain import Mission, PatientScope, Principal, ReviewObligation, TenantScope, VersionRef
from sanad.domain import events as ev
from sanad.domain.entities import TERMINAL_STATES
from sanad.domain.transitions import ALLOWED_REVIEW_ACTIONS, LEGAL_TRANSITIONS
from sanad.evidence.doctor import action_choices, command_for
from sanad.liaison.snapshot import snapshot, source_row
from sanad.presentation import doctor_actions as words
from sanad.scribe.repository import ScribeRepository
from sanad.steward.service import Steward
from sanad.steward.types import CommandResult, command_result, records
from sanad.store import keys
from sanad.store.protocol import Store
from sanad.store.records import (
    CommandEnvelope,
    Doctor,
    Evidence,
    EvidenceHead,
    StoredRecord,
    WebSession,
    canonical_json,
    from_record,
)
from sanad.web.api_questions import AnswerBody, VersionBody, question_action, question_command
from sanad.web.routes import require_session


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_kind: Literal["mission", "review", "question", "evidence", "followup"]
    item_id: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(strict=True, ge=1)
    command_id: str = Field(min_length=1, max_length=160)
    due_at: datetime | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=1000)
    text: str | None = Field(default=None, max_length=700)
    listing_token: str | None = None
    expected_source_version: int | None = Field(default=None, strict=True, ge=1)
    disposition: str | None = None
    mission_id: str | None = None

    @model_validator(mode="after")
    def action_fields(self) -> Self:
        allowed = {
            "mission": {"extend", "close_unfulfilled"},
            "review": {"acknowledge", "resolve"},
            "question": {"answer", "defer", "close"},
            "evidence": {"associate", "accept", "reject", "confirm_identity"},
            "followup": set(),
        }
        if self.action not in allowed[self.item_kind]:
            raise ValueError("invalid_action")
        common = {"item_kind", "item_id", "action", "expected_version", "command_id"}
        required = set()
        optional = set()
        if self.item_kind == "mission" and self.action == "extend":
            required = {"due_at", "reason"}
        elif self.item_kind == "mission" and self.action == "close_unfulfilled":
            required = {"reason"}
        elif self.item_kind == "question" and self.action == "answer":
            required = {"text"}
        elif self.item_kind == "review":
            required = {"listing_token", "expected_source_version"}
            if self.action == "resolve":
                required |= {"reason", "disposition"}
        elif self.item_kind == "evidence":
            if self.action == "associate":
                required = {"mission_id"}
            elif self.action == "reject":
                required = {"reason"}
            optional = {"mission_id"}
        if self.model_fields_set - common - required - optional or any(
            getattr(self, field) is None for field in required
        ):
            raise ValueError("invalid_action_fields")
        if self.reason is not None and (
            not self.reason.strip() or (self.item_kind != "evidence" and len(self.reason) > 200)
        ):
            raise ValueError("reason_required")
        if self.due_at is not None and self.due_at.utcoffset() is None:
            raise ValueError("timezone_required")
        return self


def command_id(actor: Principal, body: ActionBody) -> str:
    data = body.model_dump(mode="json", exclude={"command_id"}, exclude_unset=True)
    body_hash = keys.digest(canonical_json(data).decode())
    return "web:" + keys.digest(actor.subject + "|" + body.command_id + "|" + body_hash)[:32]


def permitted_actions(
    store: Store,
    scope: PatientScope,
    kind: str,
    row: StoredRecord,
    *,
    removed: bool = False,
    language: str = "en",
) -> list[dict[str, JsonValue]]:
    def action(name: str) -> dict[str, JsonValue]:
        return {"action": name, "label": words.text(name, language)}

    if kind == "review":
        review = from_record(row, ReviewObligation)
        if review.state not in {"open", "acknowledged"}:
            return []
        result = [action("acknowledge")] if review.state == "open" else []
        return result + [
            {
                "action": "resolve",
                "disposition": choice.value,
                "label": words.disposition(choice.value, language),
            }
            for choice in sorted(ALLOWED_REVIEW_ACTIONS[review.review_kind])
        ]
    if kind in {"mission", "question"}:
        mission = from_record(row, Mission)
        if mission.state in TERMINAL_STATES:
            return []
        if kind == "question":
            if mission.kind != "QUESTION":
                return []
            if not any(
                r.body.get("review_kind") == "question_answer"
                and r.body.get("source_mission_id") == mission.id
                and r.body.get("state") != "resolved"
                for r in records(store, scope, "review")
            ):
                return []
            # A held answer awaiting its confirmed amendment is display-only.
            if mission.details.model_dump().get("held_answer"):
                return []
            profile = store.get_patient_profile(scope)
            if not removed and (profile is None or not profile.recipient_ref):
                return []
            result = [action("answer"), action("defer")]
            if not mission.danger_history and not any(
                r.body.get("state") == "open" for r in records(store, scope, "incident")
            ):
                result.append(action("close"))
            return result
        if removed or mission.kind == "QUESTION":
            return []
        legal = LEGAL_TRANSITIONS[mission.state]
        result = [action("extend")] if ev.DoctorExtend in legal else []
        if (
            ev.DoctorCloseUnfulfilled in legal
            and not mission.danger_history
            and not any(r.body.get("state") == "open" for r in records(store, scope, "incident"))
        ):
            result.append(action("close_unfulfilled"))
        return result
    if kind == "evidence":
        return [
            {"action": a, "mission_id": m, "label": label}
            for a, m, label in action_choices(store, from_record(row, Evidence), language)
        ]
    return []


def make_command(
    steward: Steward,
    actor: Principal,
    scope: PatientScope,
    body: ActionBody,
    row: StoredRecord,
) -> CommandEnvelope:
    id = command_id(actor, body)
    ref = VersionRef(entity_type=row.entity_type, id=row.id, version=body.expected_version)
    if body.item_kind == "question":
        qbody = (
            AnswerBody(
                command_id=body.command_id,
                expected_version=body.expected_version,
                text=body.text or "",
            )
            if body.action == "answer"
            else VersionBody(command_id=body.command_id, expected_version=body.expected_version)
        )
        return question_command(
            steward,
            actor,
            scope,
            body.item_id,
            body.action,
            qbody,
            command_id=id,
            expected_versions=(ref,),
        )
    if body.item_kind == "evidence":
        evidence = from_record(row, Evidence)
        # Keep the requested version in the unchanged evidence payload on replay.
        evidence = evidence.model_copy(update={"version": body.expected_version})
        command = command_for(
            steward,
            actor,
            evidence,
            body.action,
            id,
            mission_id=body.mission_id or evidence.mission_id,
            reason=body.reason,
        )
        return command.model_copy(update={"expected_versions": (ref,)})
    payload: dict[str, JsonValue]
    if body.item_kind == "review":
        payload = {
            "type": "AcknowledgeReview" if body.action == "acknowledge" else "ResolveReview",
            "review_id": body.item_id,
            "listing_token": body.listing_token,
            "expected_source_version": body.expected_source_version,
        }
        if body.action == "resolve":
            payload.update(action=body.disposition, reason=body.reason)
    else:
        payload = {
            "type": "ExtendMission" if body.action == "extend" else "CloseUnfulfilledMission",
            "mission_id": body.item_id,
            "reason": body.reason,
        }
        if body.action == "extend":
            assert body.due_at is not None
            payload.update(
                timing={
                    "instant": body.due_at.isoformat(),
                    "timezone": "UTC",
                    "original_expression": body.due_at.isoformat(),
                },
                grace_seconds=row.body.get("grace_seconds", 0),
            )
    return CommandEnvelope(
        command_id=id,
        principal=actor,
        scope=scope,
        requested_at=steward.clock(),
        payload=payload,
        expected_versions=(ref,),
    )


def cursor_value(session: WebSession, patient_id: str, position: str) -> str:
    data = json.dumps([patient_id, position], separators=(",", ":")).encode()
    signature = hmac.new(session.csrf_secret_ref.encode(), data, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(data).decode() + "." + signature


def cursor_position(session: WebSession, patient_id: str, value: str | None) -> str:
    if value is None:
        return ""
    try:
        data, _ = value.split(".")
        raw = base64.b64decode(data, altchars=b"-_", validate=True)
        owner, position = json.loads(raw)
        if (
            owner != patient_id
            or not isinstance(position, str)
            or not hmac.compare_digest(cursor_value(session, owner, position), value)
        ):
            raise ValueError
        return position
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(422, detail="invalid_cursor") from None


def action_router(claims: ClaimService) -> APIRouter:
    router = APIRouter()
    guard = require_session("doctor")

    def context(
        session: WebSession, patient_id: str
    ) -> tuple[PatientScope, Doctor, Principal, bool]:
        # Ownership precedes every clinical item read, including command replay.
        patient = claims.patient(session.doctor_id, patient_id)
        if patient is None:
            raise HTTPException(404)
        scope = patient.scope
        row = claims.store.get(
            TenantScope(doctor_id=session.doctor_id), "doctor", session.doctor_id
        )
        assert row
        doctor = from_record(row, Doctor)
        actor = Principal(
            subject=session.subject,
            user_id=session.subject,
            actor_kind="doctor",
            verified_roles=frozenset({"doctor"}),
            doctor_id=session.doctor_id,
            bot_id=doctor.telegram_bot_id,
            auth_epoch=session.auth_epoch,
            session_id=session.id,
        )
        profile = claims.store.get_patient_profile(scope)
        return scope, doctor, actor, bool(profile and profile.removed_at)

    @router.get("/api/patients/{patient_id}/actions")
    def listing(
        patient_id: str,
        request: Request,
        session: Annotated[WebSession, Depends(guard)],
        cursor: str | None = None,
    ) -> object:
        scope, doctor, _, removed = context(session, patient_id)
        after = cursor_position(session, patient_id, cursor)
        rows: list[tuple[str, StoredRecord]] = []
        for kind in ("review", "mission", "followup"):
            for row in records(claims.store, scope, kind):
                if row.body.get("state") in TERMINAL_STATES | {"resolved"}:
                    continue
                item_kind = "question" if row.body.get("kind") == "QUESTION" else kind
                rows.append((item_kind, row))
        rows.sort(key=lambda pair: pair[0] + ":" + pair[1].id)
        pending = [(k, r) for k, r in rows if k + ":" + r.id > after]
        selected = pending[:25]
        now = request.app.state.telegram.steward.clock()
        intent = (
            ScribeRepository(claims.store, lambda: now)
            .intent(
                doctor,
                "doctor_questions",
                {"text": "Browser record listing."},
                token_urlsafe(32),
            )
            .model_copy(
                update={
                    "review_listing": tuple(
                        snapshot(claims.store, from_record(r, ReviewObligation))
                        for k, r in selected
                        if k == "review"
                    ),
                    "review_listing_expires_at": now + timedelta(minutes=30),
                    "record_listing_session_id": session.id,
                    "record_listing_subject": session.subject,
                    "record_listing_auth_epoch": session.auth_epoch,
                    "record_listing_patient_id": patient_id,
                    "status": "suppressed",
                    "suppression_reason": "browser_listing",
                    "work_clock": None,
                    "expires_at": now + timedelta(minutes=30),
                }
            )
        )
        if claims.store.save_record_listing(intent, session) is None:
            raise HTTPException(409, detail=words.text("stale", "en"))
        items = []
        for kind, row in selected:
            source = (
                source_row(claims.store, from_record(row, ReviewObligation))
                if kind == "review"
                else None
            )
            items.append(
                {
                    "item_kind": kind,
                    "id": row.id,
                    "version": row.version,
                    "mission" if kind == "question" else kind: row.body,
                    "source": source.body if source else None,
                    "actions": permitted_actions(
                        claims.store, scope, kind, row, removed=removed, language="en"
                    ),
                }
            )
        return {
            "items": items,
            "listing_token": intent.id,
            "cursor": cursor_value(session, patient_id, selected[-1][0] + ":" + selected[-1][1].id)
            if len(pending) > 25
            else None,
            "words": {k.removeprefix("doctor_actions."): v["en"] for k, v in words.CATALOG.items()},
        }

    @router.post("/api/patients/{patient_id}/actions")
    def act(
        patient_id: str,
        body: ActionBody,
        request: Request,
        session: Annotated[WebSession, Depends(guard)],
    ) -> object:
        scope, doctor, actor, removed = context(session, patient_id)
        store = claims.store
        kind = "mission" if body.item_kind == "question" else body.item_kind
        row = store.get(scope, kind, body.item_id)
        if kind == "evidence":
            head = store.get(scope, "evidence_head", body.item_id)
            row = (
                store.get(
                    scope,
                    "evidence",
                    f"{body.item_id}:{body.expected_version}",
                )
                if head
                else None
            )
            if row is None and head is not None:
                row = store.get(
                    scope,
                    "evidence",
                    f"{body.item_id}:{from_record(head, EvidenceHead).current_version}",
                )
        if row is None or (
            kind == "mission"
            and ((row.body.get("kind") == "QUESTION") != (body.item_kind == "question"))
        ):
            raise HTTPException(404)
        steward = request.app.state.telegram.steward
        if body.item_kind == "review":
            bound = store.get(
                keys.IntakeScope(doctor_id=scope.doctor_id, intake_id="scribe"),
                "outbound_intent",
                body.listing_token or "",
            )
            if bound is None or (
                bound.body.get("record_listing_session_id") != session.id
                or bound.body.get("record_listing_subject") != session.subject
                or bound.body.get("record_listing_auth_epoch") != session.auth_epoch
                or bound.body.get("record_listing_patient_id") != patient_id
            ):
                return response(CommandResult(status="stale_version"), "en")
        id = command_id(actor, body)
        prior = store.lookup_record_action(session, scope, id)
        if prior is not None:
            effects = (
                ["mission_overdue"]
                if body.item_kind == "mission"
                and store.lookup_record_action(session, scope, id + ":deadline") is not None
                else []
            )
            return response(command_result(prior), "en", effects)
        if row.version != body.expected_version or (
            kind == "evidence"
            and head is not None
            and from_record(head, EvidenceHead).current_version != body.expected_version
        ):
            return response(CommandResult(status="stale_version"), "en")
        choices = permitted_actions(
            store, scope, body.item_kind, row, removed=removed, language="en"
        )
        if not any(
            c["action"] == body.action
            and c.get("disposition") == body.disposition
            and (
                body.item_kind != "evidence"
                or body.action != "associate"
                or c.get("mission_id") == body.mission_id
            )
            for c in choices
        ):
            return response(CommandResult(status="invalid_action"), "en")
        command = make_command(steward, actor, scope, body, row)
        result = (
            question_action(steward, command)
            if body.item_kind == "question"
            else steward.handle(command)
        )
        return response(result, "en", deadline_effects(store, command))

    return router


def deadline_effects(store: Store, command: CommandEnvelope) -> list[str]:
    if command.payload.get("type") not in {"ExtendMission", "CloseUnfulfilledMission"}:
        return []
    child = command.model_copy(
        update={
            "command_id": command.command_id + ":deadline",
            "payload": {"type": "_Deadline", "mission_id": command.payload["mission_id"]},
        }
    )
    return ["mission_overdue"] if store.lookup_command(child) is not None else []


def response(
    result: CommandResult, language: str, side_effects: list[str] | None = None
) -> JSONResponse:
    status = (
        200
        if result.status == "accepted"
        else 409
        if result.status in {"stale_version", "forbidden"}
        else 422
    )
    return JSONResponse(
        status_code=status,
        content={
            "status": result.status,
            "reason": result.reason_code,
            "outcome_label": result.outcome_label or "saved",
            "outcome_reason": result.outcome_reason,
            "detail": words.outcome(result.outcome_label, result.outcome_reason, language)
            if status == 200
            else words.text("stale" if status == 409 else "refused", language),
            "side_effects": side_effects or [],
            "resulting_versions": [r.model_dump() for r in result.resulting_versions],
        },
    )
