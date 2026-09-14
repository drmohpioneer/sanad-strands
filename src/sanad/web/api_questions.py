"""Authenticated question projections and adapters to the shared Steward commands."""

from datetime import timedelta
from secrets import token_urlsafe
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from sanad.auth.claim import ClaimService
from sanad.concierge import reuse
from sanad.concierge.answer_command import active_orders, owned_questions
from sanad.concierge.plan import order_line
from sanad.domain import PatientScope, Principal, TenantScope, VersionRef
from sanad.domain.entities import QuestionDetails
from sanad.scribe.patients import panel
from sanad.scribe.repository import ScribeRepository
from sanad.steward.service import Steward
from sanad.steward.types import CommandResult
from sanad.store import keys
from sanad.store.records import CommandEnvelope, Doctor, WebSession, from_record
from sanad.web.routes import require_session


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(strict=True, ge=1)
    text: str = Field(max_length=700)


class VersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(strict=True, ge=1)


class SendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=160)
    listing_token: str
    n: int = Field(strict=True, ge=1)
    mission_version: int = Field(strict=True, ge=1)
    reusable_id: str
    reusable_version: int = Field(strict=True, ge=1)


class ReuseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=160)
    offer_id: str


def question_command(
    steward: Steward,
    actor: Principal,
    scope: PatientScope,
    mission_id: str,
    action: str,
    body: AnswerBody | VersionBody | SendBody | ReuseBody,
    *,
    command_id: str | None = None,
    expected_versions: tuple[VersionRef, ...] = (),
) -> CommandEnvelope:
    payload: dict[str, JsonValue]
    if isinstance(body, SendBody):
        payload = {
            "type": "SendQuestion",
            "mission_id": mission_id,
            **body.model_dump(exclude={"command_id"}),
        }
    elif isinstance(body, ReuseBody):
        offer = steward.store.get(
            TenantScope(doctor_id=scope.doctor_id), "reuse_offer", body.offer_id
        )
        if (
            offer is None
            or offer.body.get("mission_id") != mission_id
            or offer.body.get("patient_id") != scope.patient_id
        ):
            raise HTTPException(404)
        payload = {"type": "ReuseAnswer", "offer_id": body.offer_id}
    elif isinstance(body, AnswerBody):
        payload = {
            "type": "AnswerQuestion",
            "mission_id": mission_id,
            "expected_version": body.expected_version,
            "answer_text": body.text,
        }
    else:
        payload = {
            "type": "DeferQuestion" if action == "defer" else "AnswerQuestion",
            "mission_id": mission_id,
            "expected_version": body.expected_version,
        }
        if action == "close":
            payload["close_only"] = True
    return CommandEnvelope(
        command_id=command_id or "browser-question:" + body.command_id,
        scope=scope,
        principal=actor,
        requested_at=steward.clock(),
        payload=payload,
        expected_versions=expected_versions,
    )


def question_action(steward: Steward, command: CommandEnvelope) -> CommandResult:
    """Shared dispatch retains the complete immutable command outcome."""
    return steward.handle(command)


def question_router(claims: ClaimService) -> APIRouter:
    router = APIRouter()
    guard = require_session("doctor")

    @router.get("/api/questions")
    def questions(request: Request, session: Annotated[WebSession, Depends(guard)]) -> object:
        tenant = TenantScope(doctor_id=session.doctor_id)
        row = claims.store.get(tenant, "doctor", session.doctor_id)
        assert row
        doctor = from_record(row, Doctor)
        now = request.app.state.telegram.steward.clock()
        values = owned_questions(claims.store, doctor.id)
        answers = reuse.answer_set(claims.store, doctor.id)
        repo = ScribeRepository(claims.store, lambda: now)
        token = keys.digest(token_urlsafe(32))
        previous = reuse.listings(claims.store, doctor.id)
        intent = repo.intent(
            doctor,
            "doctor_questions",
            {"text": "Browser question listing."},
            token,
            sequence=1 + max((i.conversation_sequence for i in previous), default=0),
        )
        intent = intent.model_copy(
            update={
                "question_listing_token": token,
                "question_listing_targets": tuple((p.id, m.id) for p, m in values),
                "question_bindings": tuple(
                    reuse.binding(claims.store, m, answers) for _, m in values
                ),
                "question_listing_expires_at": now + timedelta(hours=1),
                "status": "suppressed",
                "suppression_reason": "browser_listing",
                "work_clock": None,
            }
        )
        actor = claims.store.authorize(doctor.telegram_bot_id, session.subject).principal
        result = repo.commit(actor, "ScribeReply", "browser-questions:" + token, intents=(intent,))
        if result.status != "accepted":
            raise HTTPException(409)
        items = []
        for n, (patient, mission) in enumerate(values, 1):
            assert isinstance(mission.details, QuestionDetails)
            source = reuse.choose(answers, mission.details.question_text)
            orders = active_orders(claims.store, patient.scope)
            items.append(
                {
                    "id": mission.id,
                    "version": mission.version,
                    "n": n,
                    "patient_name": patient.display_name,
                    "context_line": order_line(orders[0], doctor.language)
                    if orders
                    else "No active medication plan is recorded.",
                    "text": mission.details.question_text,
                    "hours_waiting": max(
                        0, int((now - mission.created_at).total_seconds() // 3600)
                    ),
                    "proposed_reply": {
                        "text": source.answer_text[:159] + "…"
                        if len(source.answer_text) > 160
                        else source.answer_text,
                        "reusable_id": source.id,
                        "version": source.version,
                    }
                    if source
                    else None,
                }
            )
        return {"listing_token": token, "questions": items}

    @router.post("/api/questions/{mission_id}/{action}")
    def action(
        mission_id: str,
        action: Literal["send", "answer", "defer", "close", "reuse"],
        body: AnswerBody | VersionBody | SendBody | ReuseBody,
        request: Request,
        session: Annotated[WebSession, Depends(guard)],
    ) -> object:
        expected_type = {
            "answer": AnswerBody,
            "send": SendBody,
            "reuse": ReuseBody,
            "defer": VersionBody,
            "close": VersionBody,
        }[action]
        if type(body) is not expected_type:
            raise HTTPException(422)
        owned = [
            (p, claims.store.get(p.scope, "mission", mission_id))
            for p in panel(claims.store, TenantScope(doctor_id=session.doctor_id))
        ]
        match = next(
            ((p, m) for p, m in owned if m is not None and m.body.get("kind") == "QUESTION"), None
        )
        if match is None:
            raise HTTPException(404)
        patient, _ = match
        row = claims.store.get(patient.scope, "doctor", session.doctor_id)
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
        steward = request.app.state.telegram.steward
        result = question_action(
            steward,
            question_command(
                steward,
                actor,
                patient.scope,
                mission_id,
                action,
                body,
            ),
        )
        if result.status != "accepted":
            raise HTTPException(
                403 if result.status == "forbidden" else 409, detail=result.reason_code
            )
        return {
            "status": result.status,
            "reason": result.reason_code,
            "offer_id": next(
                (r.id for r in result.resulting_versions if r.entity_type == "reuse_offer"), None
            ),
        }

    return router
