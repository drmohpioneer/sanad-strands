"""CSRF-protected doctor corrections with explicit browser session authority."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from sanad.auth.claim import ClaimService
from sanad.domain import PatientScope, Principal
from sanad.steward.corrections import COMMANDS
from sanad.store.records import CommandEnvelope, Doctor, WebSession, from_record
from sanad.web.routes import require_session


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=160)
    expected_binding_epoch: int = Field(ge=0)
    expected_delivery_epoch: int = Field(ge=0)
    action: dict[str, JsonValue]


def correction_router(claims: ClaimService) -> APIRouter:
    router = APIRouter()

    @router.post("/api/patients/{patient_id}/corrections")
    def action(
        patient_id: str,
        body: ActionBody,
        request: Request,
        session: Annotated[WebSession, Depends(require_session("doctor"))],
    ) -> object:
        scope = PatientScope(doctor_id=session.doctor_id, patient_id=patient_id)
        if claims.patient(session.doctor_id, patient_id) is None:
            raise HTTPException(404)
        if body.action.get("type") not in COMMANDS:
            raise HTTPException(422)
        row = claims.store.get(scope, "doctor", session.doctor_id)
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
        command = CommandEnvelope(
            command_id="browser-correction:" + body.command_id,
            scope=scope,
            principal=actor,
            requested_at=steward.clock(),
            payload=body.action,
            expected_binding_epoch=body.expected_binding_epoch,
            expected_delivery_epoch=body.expected_delivery_epoch,
        )
        result = steward.handle(command)
        if result.status != "accepted":
            raise HTTPException(
                403 if result.status == "forbidden" else 409, detail=result.reason_code
            )
        offers = [
            claims.store.get(scope, r.entity_type, r.id)
            for r in result.resulting_versions
            if r.entity_type == "correction_offer"
        ]
        # Resulting versions include consumed offers too. Only pending offers
        # ask the caller for confirmation, including when a command is replayed.
        return {
            "status": result.status,
            "offers": [r.body for r in offers if r and r.body.get("consumed_at") is None],
        }

    return router
