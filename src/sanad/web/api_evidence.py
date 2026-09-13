"""Doctor-only evidence projections and decisions through the Steward."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from sanad.auth.claim import ClaimService
from sanad.domain import Principal, TenantScope
from sanad.evidence import templates
from sanad.evidence.doctor import action_choices, command_for, owned, refusal_key
from sanad.store.records import Doctor, Duplicate, Evidence, WebSession, from_record
from sanad.web.routes import SESSION_COOKIE, require_session


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=160)
    evidence_version: int | None = Field(default=None, strict=True, ge=1)


class AssociationBody(ActionBody):
    mission_id: str = Field(min_length=1, max_length=160)


class RejectionBody(ActionBody):
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
        body: ActionBody,
        *,
        mission_id: str | None = None,
        reason: str | None = None,
    ) -> object:
        value = find(session, id)
        if body.evidence_version is not None:
            row = claims.store.get(value.scope, "evidence", f"{id}:{body.evidence_version}")
            if row is None:
                raise HTTPException(404)
            value = from_record(row, Evidence)
        target = mission_id or value.mission_id
        if (
            action in {"associate", "accept"}
            and target
            and not claims.store.get(value.scope, "mission", target)
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
        command = command_for(
            request.app.state.telegram.steward,
            actor,
            value,
            action,
            "evidence-api:" + session.subject + ":" + body.command_id,
            mission_id=target,
            reason=reason,
        )
        prior = claims.store.lookup_command(command)
        result = request.app.state.telegram.steward.handle(command)
        duplicate = isinstance(prior, Duplicate) and result.status == "accepted"
        doctor_row = claims.store.get(value.scope, "doctor", session.doctor_id)
        assert doctor_row
        language = from_record(doctor_row, Doctor).language
        if result.status not in {"accepted", "duplicate"}:
            return JSONResponse(
                status_code=409,
                content={
                    "reason": result.reason_code or result.status,
                    "detail": templates.render(
                        refusal_key(result.reason_code, result.status), language
                    ),
                },
            )
        return {
            "status": "duplicate" if duplicate else result.status,
            "detail": templates.render(
                "doctor_evidence_already_handled"
                if duplicate
                else "doctor_evidence_action_recorded",
                language,
            ),
            "evidence": find(session, id).model_dump(mode="json"),
        }

    @router.get("/api/patients/{patient_id}/evidence")
    def heads(patient_id: str, session: Annotated[WebSession, Depends(session_guard)]) -> object:
        if claims.patient(session.doctor_id, patient_id) is None:
            raise HTTPException(404)
        row = claims.store.get(
            TenantScope(doctor_id=session.doctor_id), "doctor", session.doctor_id
        )
        assert row
        language = from_record(row, Doctor).language
        return [
            {
                **e.model_dump(mode="json"),
                "actions": [
                    {"action": action, "mission_id": mission_id, "label": label}
                    for action, mission_id, label in action_choices(claims.store, e, language)
                ],
            }
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
        return act(request, session, id, "associate", body, mission_id=body.mission_id)

    @router.post("/api/evidence/{id}/accept")
    def accept(
        id: str,
        request: Request,
        session: Annotated[WebSession, Depends(session_guard)],
        body: ActionBody | None = None,
    ) -> object:
        return act(request, session, id, "accept", body or ActionBody())

    @router.post("/api/evidence/{id}/confirm-identity")
    def confirm_identity(
        id: str,
        request: Request,
        session: Annotated[WebSession, Depends(session_guard)],
        body: ActionBody | None = None,
    ) -> object:
        return act(request, session, id, "confirm_identity", body or ActionBody())

    @router.post("/api/evidence/{id}/reject")
    def reject(
        id: str,
        body: RejectionBody,
        request: Request,
        session: Annotated[WebSession, Depends(session_guard)],
    ) -> object:
        return act(request, session, id, "reject", body, reason=body.reason)

    return router
