"""Contact-specific freshness and rendering at the shared gateway."""

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from sanad.contact.policy import DRAFT_CONTACT_POLICY as POLICY
from sanad.contact.templates import patient_text
from sanad.domain import FollowUpTask, Mission, PatientScope
from sanad.domain.language import default_language
from sanad.steward.types import records
from sanad.store.protocol import Store
from sanad.store.records import OutboundIntent, Patient, from_record


def contact_freshness(store: Store, intent: OutboundIntent, now: datetime) -> str | None:
    assert isinstance(intent.scope, PatientScope)
    patient_row = store.get(intent.scope, "patient", intent.scope.patient_id)
    if not patient_row:
        return "patient_missing"
    patient = from_record(patient_row, Patient)
    refs = [r for r in intent.source_versions if r.entity_type in {"mission", "followup"}]
    if len(refs) != 1:
        return "contact_source"
    ref = refs[0]
    row = store.get(intent.scope, ref.entity_type, ref.id)
    if not row:
        return "source_version"
    if intent.contact_kind == "chase":
        day = now.astimezone(ZoneInfo(patient.timezone)).date().isoformat()
        if intent.slot_id != "chase:" + day or ref.entity_type != "mission":
            return "slot_invalid"
        mission = from_record(row, Mission)
        if (
            mission.state not in {"open", "waiting_patient"}
            or mission.contact_count >= POLICY.per_mission_chase_limit
        ):
            return "contact_ineligible"
        for r in records(store, intent.scope, "outbound_intent"):
            other = from_record(r, OutboundIntent)
            if (
                other.id != intent.id
                and other.contact_kind == "chase"
                and other.status == "provider_accepted"
                and other.accepted_at
                and now < other.accepted_at + POLICY.chase_min_gap
            ):
                return "budget"
    elif ref.entity_type == "mission":
        mission = from_record(row, Mission)
        if mission.details.kind != "MONITOR" or mission.state not in {"open", "waiting_patient"}:
            return "slot_invalid"
        slots = {f"monitor:{mission.id}:{i}": at for i, at in enumerate(mission.details.slots)}
        at = slots.get(intent.slot_id or "")
        if at is None or not at <= now < at + POLICY.scheduled_prompt_window:
            return "slot_invalid"
    else:
        task = from_record(row, FollowUpTask)
        if intent.slot_id != (task.consent_slot_id or task.id) or task.state != "scheduled":
            return "slot_invalid"
    return None


def payload(store: Store, intent: OutboundIntent) -> dict[str, JsonValue]:
    assert isinstance(intent.scope, PatientScope)
    ref = next(r for r in intent.source_versions if r.entity_type in {"mission", "followup"})
    row = store.get(intent.scope, ref.entity_type, ref.id)
    patient_row = store.get(intent.scope, "patient", intent.scope.patient_id)
    assert row and patient_row and intent.template_id
    source = (
        from_record(row, Mission)
        if ref.entity_type == "mission"
        else from_record(row, FollowUpTask)
    )
    return {
        "text": patient_text(store, source, from_record(patient_row, Patient), intent.template_id)
    }


def doctor_payload(store: Store, intent: OutboundIntent) -> dict[str, JsonValue]:
    from sanad.contact.templates import render
    from sanad.domain.deadlines import format_local
    from sanad.store.records import Doctor

    assert isinstance(intent.scope, PatientScope)
    ref = next(r for r in intent.source_versions if r.entity_type in {"mission", "followup"})
    row = store.get(intent.scope, ref.entity_type, ref.id)
    doctor_row = store.get(intent.scope, "doctor", intent.scope.doctor_id)
    assert row
    doctor = from_record(doctor_row, Doctor) if doctor_row else None
    language = doctor.language if doctor else default_language
    zone = doctor.timezone if doctor else "Africa/Cairo"
    source = (
        from_record(row, Mission)
        if ref.entity_type == "mission"
        else from_record(row, FollowUpTask)
    )
    title = (
        source.title
        if isinstance(source, Mission)
        else (
            ("Day-three follow-up" if source.kind == "MEDICATION_DAY3" else "Doctor follow-up")
            if language == "en"
            else ("متابعة اليوم الثالث" if source.kind == "MEDICATION_DAY3" else "متابعة الدكتور")
        )
    )
    if intent.notification_purpose == "DONE:FULFILLMENT":
        text = render("doctor_objective_done", language, title=title)
    else:
        from sanad.evidence.delivery import pending_verification
        from sanad.evidence.templates import render as render_evidence

        pending = isinstance(source, Mission) and pending_verification(store, intent.scope, source)
        renderer = render_evidence if pending else render
        text = renderer(
            "doctor_objective_deadline_pending_verification"
            if pending
            else "doctor_objective_deadline",
            language,
            title=title,
            due_local=format_local(source.due_at or source.review_at, zone),
        )
    return {"text": text}
