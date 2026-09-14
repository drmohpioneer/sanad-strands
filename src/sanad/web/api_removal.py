"""Typed-name removal through the current doctor session and CSRF guard."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from sanad.auth.claim import ClaimService
from sanad.presentation.removal import words
from sanad.steward.removal import normalized_name
from sanad.store.records import CommandEnvelope, WebSession
from sanad.web.routes import require_session


class RemovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(strict=True, ge=1)
    command_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=160)


def removal_router(claims: ClaimService) -> APIRouter:
    router = APIRouter()

    @router.post("/api/patients/{patient_id}/remove")
    def remove(
        patient_id: str,
        body: RemovalBody,
        request: Request,
        session: Annotated[WebSession, Depends(require_session("doctor"))],
    ) -> object:
        patient = claims.patient(session.doctor_id, patient_id)
        if patient is None:
            raise HTTPException(404)
        if normalized_name(body.name) != normalized_name(patient.display_name):
            raise HTTPException(422, detail="Type the patient's name to confirm.")
        actor = claims.store.authorize(claims.scope.bot_id, session.subject).principal.model_copy(
            update={"session_id": session.id}
        )
        command = CommandEnvelope(
            command_id="removal:" + body.command_id,
            principal=actor,
            scope=patient.scope,
            requested_at=claims.clock(),
            payload={
                "type": "RemovePatient",
                "expected_version": body.expected_version,
                "name": body.name,
            },
        )
        result = request.app.state.telegram.steward.handle(command)
        if result.status != "accepted":
            raise HTTPException(
                403 if result.status == "forbidden" else 409,
                detail="This record changed. Refresh and try again.",
            )
        return {"status": "accepted", "message": words(patient.display_name)["result"]}

    return router
