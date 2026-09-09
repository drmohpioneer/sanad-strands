"""Doctor-only evidence projections and decisions through the Steward."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from sanad.auth.claim import ClaimService
from sanad.domain import Principal
from sanad.evidence.doctor import decide, owned
from sanad.store.records import Evidence, WebSession
from sanad.web.routes import SESSION_COOKIE, require_session


class AssociationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mission_id: str = Field(min_length=1, max_length=160)


class RejectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)


def evidence_router(claims: ClaimService) -> APIRouter:
    router = APIRouter()
    from sanad.web.api_corrections import correction_router

    router.include_router(correction_router(claims))
    session_guard = require_session("doctor")

    def find(session: WebSession, id: str) -> Evidence:
        value = next(
            (e for e in owned(claims.store, session.doctor_id) if e.evidence_id == id), None
        )
        if value is None:
            raise HTTPException(404)
        return value

    def act(
        request: Request,
        session: WebSession,
        id: str,
        action: str,
        *,
        mission_id: str | None = None,
        reason: str | None = None,
    ) -> object:
        value = find(session, id)
        target = mission_id or value.mission_id
        if action != "reject" and (
            not target or not claims.store.get(value.scope, "mission", target)
        ):
            raise HTTPException(404)
        current = request.app.state.login.require(request.cookies.get(SESSION_COOKIE, ""), "doctor")
        if current is None or current.auth_epoch != session.auth_epoch:
            raise HTTPException(401)
        actor = Principal(
            subject=session.subject,
            actor_kind="doctor",
            verified_roles=frozenset({"doctor"}),
            doctor_id=session.doctor_id,
            auth_epoch=session.auth_epoch,
            session_id=session.id,
        )
        result = decide(
            request.app.state.telegram.steward,
            actor,
            value,
            action,
            "evidence-api:" + uuid4().hex,
            mission_id=target,
            reason=reason,
        )
        if result.status not in {"accepted", "duplicate"}:
            raise HTTPException(409)
        return {"status": result.status, "evidence": find(session, id).model_dump(mode="json")}

    @router.get("/api/patients/{patient_id}/evidence")
    def heads(patient_id: str, session: Annotated[WebSession, Depends(session_guard)]) -> object:
        if claims.patient(session.doctor_id, patient_id) is None:
            raise HTTPException(404)
        return [
            e.model_dump(mode="json")
            for e in owned(claims.store, session.doctor_id)
            if e.scope.patient_id == patient_id
        ]

    @router.get("/api/evidence/{id}")
    def versions(id: str, session: Annotated[WebSession, Depends(session_guard)]) -> object:
        value = find(session, id)
        return [
            row.body
            for version in range(1, value.version + 1)
            if (row := claims.store.get(value.scope, "evidence", f"{id}:{version}"))
        ]

    @router.post("/api/evidence/{id}/associate")
    def associate(
        id: str,
        body: AssociationBody,
        request: Request,
        session: Annotated[WebSession, Depends(session_guard)],
    ) -> object:
        return act(request, session, id, "associate", mission_id=body.mission_id)

    @router.post("/api/evidence/{id}/accept")
    def accept(
        id: str, request: Request, session: Annotated[WebSession, Depends(session_guard)]
    ) -> object:
        return act(request, session, id, "accept")

    @router.post("/api/evidence/{id}/confirm-identity")
    def confirm_identity(
        id: str, request: Request, session: Annotated[WebSession, Depends(session_guard)]
    ) -> object:
        return act(request, session, id, "confirm_identity")

    @router.post("/api/evidence/{id}/reject")
    def reject(
        id: str,
        body: RejectionBody,
        request: Request,
        session: Annotated[WebSession, Depends(session_guard)],
    ) -> object:
        return act(request, session, id, "reject", reason=body.reason)

    return router
