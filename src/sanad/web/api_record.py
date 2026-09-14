"""Session-authorized record projections and private media streaming."""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import JsonValue

from sanad.api.failures import RequestFailure, store_busy
from sanad.auth.claim import ClaimService
from sanad.domain import PatientScope, TenantScope
from sanad.evidence.templates import render as render_evidence
from sanad.scribe.crosscheck import render_card
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.scribe.proposal import Proposal
from sanad.scribe.records import CareOrderVersion, ClinicalFact
from sanad.steward.types import bounded_records as records
from sanad.store.records import (
    Doctor,
    Evidence,
    EvidenceHead,
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
        from sanad.presentation.removal import words as removal_words
        from sanad.steward.corrections import current_facts, rendered_notice

        profile = claims.store.get_patient_profile(scope)
        binding = (
            claims.store.get(scope, "patient_binding", patient.active_binding_id)
            if patient.active_binding_id
            else None
        )
        consent = (
            claims.store.get(scope, "consent", str(binding.body["consent_id"])) if binding else None
        )
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
            if isinstance(mission.details, MonitorDetails) and mission.details.time_history:
                from sanad.monitor.reschedule import local_instant

                mission_body["schedule_history"] = [
                    {
                        "old": [
                            local_instant(
                                h.effective_date, t, mission.details.timezone or mission.timezone
                            ).isoformat()
                            for t in h.old_times
                        ],
                        "new": [
                            local_instant(
                                h.effective_date, t, mission.details.timezone or mission.timezone
                            ).isoformat()
                            for t in h.new_times
                        ],
                    }
                    for h in mission.details.time_history
                ]
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
        held_medications = []
        for order in orders:
            if order["status"] != "stopped" or not order["current_version"]:
                continue
            version = CareOrderVersion.model_validate(order["current_version"])
            instruction = version.structured_instruction
            if not isinstance(instruction, OrderCandidate) or instruction.action != "stop":
                continue
            observation = version.provenance.source_observation_id
            drug = instruction.drug
            history_hold = any(
                fact.provenance.source_observation_id == observation
                and fact.payload.text.casefold().startswith(
                    drug.casefold() + ": doctor instructed hold;"
                )
                for fact in (
                    from_record(r, ClinicalFact)
                    for r in current_facts(claims.store, scope, bounded=True)
                    if r.entity_type == "clinical_fact"
                )
            )
            if not history_hold and not re.search(
                r"\bhold\b", instruction.action_quote or "", re.I
            ):
                continue
            reason = "no reason given"
            source_text = next(
                (p.source_text for p in proposals if p.source_receipt_id == observation), ""
            )
            for clause in re.split(r"[,;\n]", source_text):
                if re.search(r"(?<!\w)" + re.escape(drug) + r"(?!\w)", clause, re.I):
                    match = re.search(r"\b(?:because|due to)\s+(.+?)[.]*$", clause, re.I)
                    if match:
                        reason = match[1].strip()
            held_medications.append(
                {
                    "order_id": order["id"],
                    "drug": drug,
                    "reason": reason,
                    "since": version.confirmed_at.isoformat(),
                }
            )
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
        for r in current_facts(claims.store, scope, include_detached=True, bounded=True):
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
            "contact_preferences": {
                "reminders": "enabled"
                if profile
                and profile.routine_contact_enabled
                and patient.contact_status == "active"
                else "paused",
                "quiet_hours": consent.body["quiet_hours"] if consent else [],
                "timezone": patient.timezone,
            },
            "record_version": patient.record_version,
            "profile_version": profile.version if profile else None,
            "removed_at": profile.removed_at.isoformat()
            if profile and profile.removed_at
            else None,
            "removal_words": removal_words(patient.display_name),
            "medication_list_seen": [
                {
                    "evidence_id": e.evidence_id,
                    "printed_date": e.printed_date,
                    "items": [v.model_dump(mode="json") for v in e.extracted_values],
                    "label": render_evidence("history_label", language),
                    "active_order": False,
                }
                for head in (
                    from_record(r, EvidenceHead)
                    for r in records(claims.store, scope, "evidence_head")
                )
                if (
                    evidence_row := claims.store.get(
                        scope, "evidence", f"{head.id}:{head.current_version}"
                    )
                )
                for e in (from_record(evidence_row, Evidence),)
                if e.scope == scope and e.category in {"prescription", "medication_list"}
            ],
            "facts": [r.body for r in current_facts(claims.store, scope, bounded=True)],
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
            "held_medications": held_medications,
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
                    **(
                        {
                            "pages": [
                                p.page_index
                                for p in from_record(media_work, MediaWork).document_pages
                                if p.blob_ref
                            ]
                        }
                        if m.mime == "application/pdf"
                        and (
                            media_work := claims.store.get(
                                m.media_scope, "media_work", m.media_work_id
                            )
                        )
                        else {}
                    ),
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
        page: int | None = None,
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
            raise RequestFailure("media_storage_unavailable")
        from sanad.media.limits import MAX_DOCUMENT_BYTES

        reference, mime = work.source_blob_ref, media.mime
        if page is not None:
            selected = next((m for m in work.document_pages if m.page_index == page), None)
            if work.mime != "application/pdf" or selected is None or not selected.blob_ref:
                raise HTTPException(404)
            reference, mime = selected.blob_ref, "image/png"
        try:
            data = storage.get(
                media.media_scope,
                reference,
                MAX_DOCUMENT_BYTES
                if mime == "application/pdf"
                else DRAFT_SCRIBE_POLICY.max_photo_bytes,
            )
        except Exception as error:
            raise RequestFailure(
                "store_busy" if store_busy(error) else "media_storage_unavailable"
            ) from None
        if (
            request.app.state.login.require(request.cookies.get(SESSION_COOKIE, ""), "doctor")
            is None
        ):
            raise HTTPException(401)
        return Response(
            data,
            media_type=mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                **(
                    {"Content-Disposition": 'attachment; filename="document.pdf"'}
                    if mime == "application/pdf"
                    else {}
                ),
            },
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
