"""Session-authorized record projections and private media streaming."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from sanad.auth.claim import ClaimService
from sanad.domain import PatientScope, TenantScope
from sanad.scribe.card import render_card
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.scribe.proposal import Proposal
from sanad.steward.types import records
from sanad.store.records import IntakeDraft, MediaWork, PatientMedia, WebSession, from_record
from sanad.web.routes import SESSION_COOKIE, require_session


def record_router(claims: ClaimService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/patients/{patient_id}")
    def record(
        patient_id: str, session: Annotated[WebSession, Depends(require_session("doctor"))]
    ) -> object:
        patient = claims.patient(session.doctor_id, patient_id)
        if patient is None:
            raise HTTPException(404)
        scope, tenant = patient.scope, TenantScope(doctor_id=session.doctor_id)
        reviews = [r.body for r in records(claims.store, scope, "review")]
        missions = []
        for row in records(claims.store, scope, "mission"):
            related = [
                r
                for r in reviews
                if r.get("source_mission_id") == row.id or r.get("source_id") == row.id
            ]
            status = (
                "pending"
                if any(r.get("state") == "open" for r in related)
                else "acknowledged"
                if any(r.get("state") == "acknowledged" for r in related)
                else "reviewed"
                if related
                else "not_required"
            )
            if any(
                r.get("review_kind") == "correction_disposition" and r.get("state") != "resolved"
                for r in related
            ):
                status = "correction_requested"
            missions.append({**row.body, "review_status": status})
        orders = []
        versions = tuple(records(claims.store, scope, "care_order_version"))
        for head in records(claims.store, scope, "care_order_head"):
            history = [r.body for r in versions if r.body.get("order_id") == head.id]
            history.sort(key=lambda r: int(str(r["order_version"])))
            orders.append(
                {
                    **head.body,
                    "head": head.body,
                    "history": history,
                    "current_version": next(
                        (r for r in history if r["id"] == head.body["current_version_id"]), None
                    ),
                }
            )
        proposals = [
            from_record(r, Proposal) for r in records(claims.store, tenant, "scribe_proposal")
        ]
        pending = [
            {
                "proposal_id": p.id,
                "status": p.status,
                "expires_at": p.expires_at.isoformat(),
                "blocked_items": [i.model_dump() for i in p.issues],
                "card_text": list(render_card(p)),
            }
            for p in proposals
            if p.selected_patient_id == patient_id
            and p.status == "pending"
            and p.expires_at > claims.clock()
        ]
        media = [
            from_record(r, PatientMedia) for r in records(claims.store, scope, "patient_media")
        ]
        return {
            "patient_id": patient.id,
            "display_name": patient.display_name,
            "contact_status": patient.contact_status,
            "record_version": patient.record_version,
            "facts": [r.body for r in records(claims.store, scope, "clinical_fact")],
            "orders": orders,
            "order_heads": orders,
            "missions": missions,
            "followups": [r.body for r in records(claims.store, scope, "followup")],
            "reviews": [r for r in reviews if r.get("state") != "resolved"],
            "proposals": pending,
            "pending_proposal": pending[-1] if pending else None,
            "media": [
                {"media_id": m.id, "kind": m.kind, "date": m.created_at.isoformat(), "mime": m.mime}
                for m in media
            ],
        }

    @router.get("/api/patients/{patient_id}/media/{media_id}")
    def media_bytes(
        request: Request,
        patient_id: str,
        media_id: str,
        session: Annotated[WebSession, Depends(require_session("doctor"))],
    ) -> Response:
        scope = PatientScope(doctor_id=session.doctor_id, patient_id=patient_id)
        row = (
            claims.store.get(scope, "patient_media", media_id)
            if claims.patient(session.doctor_id, patient_id)
            else None
        )
        if row is None:
            raise HTTPException(404)
        media = from_record(row, PatientMedia)
        work_row = claims.store.get(media.media_scope, "media_work", media.media_work_id)
        if work_row is None:
            raise HTTPException(404)
        work = from_record(work_row, MediaWork)
        if not work.source_blob_ref:
            raise HTTPException(404)
        storage = getattr(request.app.state, "media_store", None)
        if storage is None:
            raise HTTPException(503)
        try:
            data = storage.get(
                media.media_scope, work.source_blob_ref, DRAFT_SCRIBE_POLICY.max_photo_bytes
            )
        except Exception:
            raise HTTPException(503) from None
        if (
            request.app.state.login.require(request.cookies.get(SESSION_COOKIE, ""), "doctor")
            is None
        ):
            raise HTTPException(401)
        return Response(
            data,
            media_type=media.mime,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @router.get("/api/intake")
    def intake(session: Annotated[WebSession, Depends(require_session("doctor"))]) -> object:
        values = [
            from_record(r, IntakeDraft)
            for r in records(claims.store, TenantScope(doctor_id=session.doctor_id), "intake_draft")
        ]
        return [
            {
                "intake_id": d.id,
                "state": d.state,
                "kind": d.kind,
                "created_at": d.created_at.isoformat(),
                "review_at": d.review_at.isoformat(),
                "review_obligation_id": d.review_obligation_id,
                "media_work_ids": d.media_work_ids,
                "safety_epoch": d.safety_epoch,
            }
            for d in values
            if d.state == "pending"
        ]

    return router
