"""Session-authorized record projections and private media streaming."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import JsonValue

from sanad.auth.claim import ClaimService
from sanad.domain import PatientScope, TenantScope
from sanad.evidence.doctor import owned
from sanad.evidence.templates import render as render_evidence
from sanad.scribe.crosscheck import render_card
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.scribe.proposal import Proposal
from sanad.steward.types import records
from sanad.store.records import (
    Doctor,
    IntakeDraft,
    MediaWork,
    PatientMedia,
    WebSession,
    from_record,
)
from sanad.web.routes import SESSION_COOKIE, require_session


def displayed_order(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    instruction = body.get("structured_instruction")
    if not isinstance(instruction, dict):
        return body
    frequency, timing = instruction.get("frequency"), instruction.get("timing")
    if (
        isinstance(frequency, str)
        and isinstance(timing, str)
        and frequency.strip().casefold() == timing.strip().casefold()
    ):
        return {**body, "structured_instruction": {**instruction, "timing": None}}
    return body


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
        doctor_row = claims.store.get(scope, "doctor", session.doctor_id)
        assert doctor_row
        from sanad.domain.language import effective

        language = effective(from_record(doctor_row, Doctor).language)
        from sanad.corrections import Correction
        from sanad.steward.corrections import current_facts, rendered_notice

        profile = claims.store.get_patient_profile(scope)
        corrections = [
            from_record(r, Correction) for r in records(claims.store, scope, "correction")
        ]
        reviews = [r.body for r in records(claims.store, scope, "review")]
        missions = []
        for row in records(claims.store, scope, "mission"):
            related = [
                r
                for r in reviews
                if r.get("source_mission_id") == row.id
                or r.get("source_id") == row.id
                or any(
                    c.id == r.get("source_id") and row.id in c.affected_mission_ids
                    for c in corrections
                )
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
            from sanad.domain import Mission
            from sanad.domain.entities import MonitorDetails
            from sanad.monitor.executor import current_details

            mission_body = dict(row.body)
            mission = from_record(row, Mission)
            if isinstance(mission.details, MonitorDetails) and any(
                mission.id in c.affected_mission_ids for c in corrections
            ):
                mission_body["details"] = current_details(claims.store, scope, mission).model_dump(
                    mode="json"
                )
            missions.append({**mission_body, "review_status": status})
        orders = []
        versions = tuple(records(claims.store, scope, "care_order_version"))
        for head in records(claims.store, scope, "care_order_head"):
            history = [
                displayed_order(r.body) for r in versions if r.body.get("order_id") == head.id
            ]
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
                "card_text": list(render_card(p, language)),
            }
            for p in proposals
            if p.selected_patient_id == patient_id
            and p.status == "pending"
            and p.expires_at > claims.clock()
        ]
        media = [
            from_record(r, PatientMedia) for r in records(claims.store, scope, "patient_media")
        ]
        media_sources = {
            m.id: (
                claims.store.get(m.media_scope, "inbound_receipt", m.source_receipt_id)
                or claims.store.get(tenant, "inbound_receipt", m.source_receipt_id)
            )
            for m in media
        }
        from sanad.store import keys

        source_heads = {
            keys.digest(r.id + ":readings"): r
            for r in records(claims.store, scope, "evidence_head")
        }
        correctable_facts = []
        for r in current_facts(claims.store, scope, include_detached=True):
            fact_source_head = source_heads.get(str(r.body.get("root_fact_id") or r.id))
            correctable_facts.append(
                {
                    **r.body,
                    "correction_evidence_id": (
                        f"{fact_source_head.id}:{fact_source_head.body['current_version']}"
                    )
                    if fact_source_head
                    else None,
                }
            )
        return {
            # Browser fields from the already scoped patient/review reads.
            "last_activity_at": max(
                (str(r.body["accepted_at"]) for r in records(claims.store, scope, "audit_event")),
                default=None,
            ),
            "consents": [
                {
                    "version": r.version,
                    "policy_text_version": r.body["policy_text_version"],
                    "accepted_at": r.body["accepted_at"],
                    "withdrawn_at": r.body.get("withdrawn_at"),
                }
                for r in records(claims.store, scope, "consent")
            ],
            "bindings": [
                {"status": r.body["status"], "confirmed_at": r.body["doctor_confirmed_at"]}
                for r in records(claims.store, scope, "patient_binding")
            ],
            "age": patient.age,
            "timezone": patient.timezone,
            "review_history": [r for r in reviews if r.get("state") == "resolved"],
            "patient_id": patient.id,
            "display_name": patient.display_name,
            "contact_status": patient.contact_status,
            "record_version": patient.record_version,
            "medication_list_seen": [
                {
                    "evidence_id": e.evidence_id,
                    "printed_date": e.printed_date,
                    "items": [v.model_dump(mode="json") for v in e.extracted_values],
                    "label": render_evidence("history_label", language),
                    "active_order": False,
                }
                for e in owned(claims.store, session.doctor_id)
                if e.scope == scope and e.category in {"prescription", "medication_list"}
            ],
            "facts": [r.body for r in current_facts(claims.store, scope)],
            "correctable_facts": correctable_facts,
            "fact_history": [r.body for r in records(claims.store, scope, "clinical_fact")],
            "corrections": [
                {
                    **c.model_dump(mode="json"),
                    "notice": rendered_notice(claims.store, c, session.doctor_id),
                }
                for c in corrections
            ],
            "correction_authority": {
                "binding_epoch": profile.binding_epoch,
                "delivery_epoch": profile.delivery_epoch,
            }
            if profile
            else None,
            "orders": orders,
            "order_heads": orders,
            "missions": missions,
            "followups": [r.body for r in records(claims.store, scope, "followup")],
            "reviews": [r for r in reviews if r.get("state") != "resolved"],
            "proposals": pending,
            "pending_proposal": pending[-1] if pending else None,
            "media": [
                {
                    "media_id": m.id,
                    "kind": m.kind,
                    "date": source.body["received_at"]
                    if (source := media_sources[m.id]) is not None
                    else m.created_at.isoformat(),
                    "mime": m.mime,
                    "uploaded_by_you": bool(
                        source and source.body.get("source_subject") == session.subject
                    ),
                }
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
