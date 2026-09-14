"""Contact-specific freshness and rendering at the shared gateway."""

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from sanad.contact.policy import DRAFT_CONTACT_POLICY as POLICY
from sanad.contact.templates import patient_text
from sanad.domain import FollowUpTask, Mission, PatientScope
from sanad.domain.language import default_language, effective
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
        if intent.template_id == "patient_visit_brief":
            from sanad.domain.entities import VisitDetails

            if not isinstance(mission.details, VisitDetails) or (
                mission.details.window_start and mission.details.window_start <= now
            ):
                return "visit_window_passed"
        if (
            mission.details.kind == "TASK"
            and mission.details.completion_rule == "unsupported_action"
        ):
            return "unsupported_task"
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
        from sanad.monitor.reschedule import slot_id

        slots = {slot_id(mission, i, prompt=True): at for i, at in enumerate(mission.details.slots)}
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
    if intent.template_id == "patient_visit_brief" and isinstance(source, Mission):
        from sanad.concierge.visits import brief_text

        return {"text": brief_text(store, source, from_record(patient_row, Patient))}
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
    language = effective(doctor.language if doctor else default_language, audience="doctor")
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
        if (isinstance(source, Mission) and source.kind == "MEDICATION") or (
            isinstance(source, FollowUpTask) and source.kind == "MEDICATION_DAY3"
        ):
            text = render("doctor_medication_done", language, title=title)
            from sanad.concierge.records import ReportFactPayload
            from sanad.scribe.records import ClinicalFact
            from sanad.steward.corrections import current_facts

            for report_row in current_facts(store, intent.scope):
                report = from_record(report_row, ClinicalFact).payload
                if not isinstance(report, ReportFactPayload) or not report.target_ref:
                    continue
                if (report.target_ref.entity_type, report.target_ref.id) != (
                    source.entity_type,
                    source.id,
                ):
                    continue
                if report.anchor_unknown and report.report_kind == "medication_start":
                    text += "\n" + render("doctor_medication_anchor_unknown", language)
                if (
                    report.report_kind == "barrier"
                    and report.barrier_type
                    and isinstance(source, FollowUpTask)
                    and report_row.id in source.source_report_ids
                ):
                    text += "\n" + render(
                        "doctor_medication_barrier",
                        language,
                        type=report.barrier_type,
                        text=report.text,
                    )
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
        if isinstance(source, Mission) and source.barrier_type and source.barrier_reason:
            text += "\n" + render(
                "doctor_medication_barrier",
                language,
                type=source.barrier_type,
                text=source.barrier_reason,
            )
            if source.barrier_attempts:
                from sanad.resolver.templates import doctor_summary

                text += "\n" + render(
                    "doctor_barrier_attempt",
                    language,
                    summary=doctor_summary(source.barrier_attempts[-1], language),
                )
    if isinstance(source, Mission) and source.details.kind == "MONITOR":
        from sanad.monitor.report import doctor_table

        text += "\n" + doctor_table(store, intent.scope, source, language)
    return {"text": text}


def task_done_payload(
    mission: Mission, language: str, accept: str, reopen: str, patient_name: str
) -> dict[str, JsonValue]:
    from sanad.contact.templates import render

    return {
        "text": render("doctor_task_done", language, title=mission.title, patient=patient_name),
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": render("doctor_task_accept_button", language),
                        "callback_data": accept,
                    },
                    {
                        "text": render("doctor_task_reopen_button", language),
                        "callback_data": reopen,
                    },
                ]
            ]
        },
    }
