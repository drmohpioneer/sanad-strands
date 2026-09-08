"""Versioned scheduling commands after accountability; no external IO."""

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sanad.auth.service import revise
from sanad.contact.ladder import plan_next_contact, quiet_at, tomorrow
from sanad.contact.policy import DRAFT_CONTACT_POLICY as POLICY
from sanad.contact.templates import patient_text
from sanad.coordinator.integration import coordinate
from sanad.domain import FollowUpTask, Mission, PatientScope, ReviewKind
from sanad.domain import events as ev
from sanad.domain.entities import TERMINAL_STATES
from sanad.domain.transitions import transition_followup, transition_mission
from sanad.steward.apply import CommitBuilder
from sanad.steward.types import CommandResult, records
from sanad.store import keys
from sanad.store.protocol import Store
from sanad.store.records import (
    Consent,
    OutboundIntent,
    Patient,
    PatientProfile,
    StoredRecord,
    from_record,
    to_record,
)

if TYPE_CHECKING:
    from sanad.steward.service import Steward


def schedule(steward: "Steward", record: StoredRecord) -> CommandResult:
    from sanad.steward.service import system_command

    scope = PatientScope(doctor_id=str(record.doctor_id), patient_id=str(record.patient_id))
    row = steward.store.get(scope, record.entity_type, record.id)
    if row is None:
        return CommandResult(status="stale_version")
    command = system_command(
        scope,
        f"sweep:contact:{row.id}:{row.version}",
        {
            "type": "_ScheduleContact",
            row.entity_type + "_id": row.id,
        },
        steward.clock(),
        lane=row.entity_type,
    )
    command = command.model_copy(update={"expected_versions": (row.ref,)})
    return steward.handle(command)


def valid_patient_projection(store: Store, row: StoredRecord) -> bool:
    patient = from_record(row, Patient)
    old_row = store.get(patient.scope, "patient", patient.id)
    if not old_row:
        return False
    old = from_record(old_row, Patient)
    mutable = {"version", "record_version", "updated_at", "contact_status", "resume_at"}
    return (
        patient.model_dump(exclude=mutable) == old.model_dump(exclude=mutable)
        and (
            patient.contact_status == old.contact_status
            or (old.contact_status, patient.contact_status)
            in {("active", "unreachable"), ("unreachable", "active")}
            or (
                old.contact_status == "paused"
                and patient.contact_status == "active"
                and old.resume_at is not None
                and old.resume_at <= patient.updated_at
                and patient.resume_at is None
            )
        )
        and (patient.resume_at is None or patient.resume_at == old.resume_at)
    )


def prime(mission: Mission, at: datetime) -> Mission:
    """An operational discovery wake, including newly confirmed/bound missions."""
    if mission.work_clock is None:
        return mission
    clock = mission.work_clock.model_copy(
        update={"next_action_at": min(at, mission.work_clock.next_action_at)}
    )
    return mission.model_copy(update={"work_clock": clock})


def _rearm(builder: CommitBuilder, source: Mission | FollowUpTask, at: datetime | None) -> None:
    if source.work_clock is None:
        return
    at = min(at, source.work_clock.next_action_at) if at else source.work_clock.next_action_at
    if at <= builder.now:
        at = builder.now + builder.policy.operations.retry_backoff(1)
    changed = revise(
        source, builder.now, work_clock=source.work_clock.model_copy(update={"next_action_at": at})
    )
    builder.put(to_record(changed, builder.scope))


def _slot_review(builder: CommitBuilder, source: Mission | FollowUpTask, slot: str) -> None:
    builder.effect(
        ev.CreateReview(
            event_id=builder.command.command_id + ":quiet:" + slot,
            review_kind=ReviewKind.binding_review,
            source_type="slot",
            source_id=slot,
            source_version=1,
            owner_doctor_id=source.doctor_id,
            patient_id=source.patient_id,
            source_mission_id=source.id
            if isinstance(source, Mission)
            else source.parent_mission_id,
            review_at=builder.now + builder.policy.timing.result_review_interval,
        ),
        source,
    )


def _missed(builder: CommitBuilder, source: Mission | FollowUpTask, slot: str) -> None:
    # Stable per source/window, even if a later wake revisits the same expired slot.
    event_id = keys.digest(f"missed:{source.entity_type}:{source.id}:{slot}")
    if builder.store.get(builder.scope, "audit_event", event_id) is None:
        builder.audit("CONTACT_WINDOW_MISSED", event_id, (to_record(source, builder.scope).ref,))


@coordinate
def prepare(
    builder: CommitBuilder, source: Mission | FollowUpTask, profile: PatientProfile
) -> None:
    now, store, scope = builder.now, builder.store, builder.scope
    patient_row = store.get(scope, "patient", scope.patient_id)
    if patient_row is None:
        return
    patient = from_record(patient_row, Patient)
    if patient.consent_id is None:
        return
    consent_row = store.get(scope, "consent", patient.consent_id)
    if consent_row is None:
        return
    consent = from_record(consent_row, Consent)
    refs = (patient_row.ref, consent_row.ref)
    builder.command = builder.command.model_copy(
        update={
            "expected_versions": tuple(dict.fromkeys((*builder.command.expected_versions, *refs)))
        }
    )
    active_missions = [
        from_record(r, Mission)
        for r in records(store, scope, "mission")
        if r.body["state"] not in TERMINAL_STATES | {"proposed", "awaiting_link"}
    ]
    if patient.contact_status == "paused" and patient.resume_at and patient.resume_at <= now:
        patient = revise(patient, now, contact_status="active", resume_at=None)
        builder.put(to_record(patient, scope))
        if profile.routine_paused_until:
            profile = revise(profile, now, routine_paused_until=None)
            builder.put(to_record(profile, scope))
    elif patient.contact_status == "unreachable" and any(
        m.state == "open" and m.unanswered_delivered_count == 0 for m in active_missions
    ):
        patient = revise(patient, now, contact_status="active")
        builder.put(to_record(patient, scope))
    if (
        isinstance(source, Mission)
        and source.state == "blocked"
        and source.resume_at
        and source.resume_at <= now
    ):
        doctor = store.get(scope, "doctor_authority", scope.doctor_id)
        result = transition_mission(
            source,
            ev.ResumeContact(
                event_id=builder.command.command_id + ":resume",
                consent_active=profile.consent_active and consent.routine_contact_enabled,
                order_active=all(
                    (r := store.get(scope, "care_order", ref.id)) is not None
                    and r.ref == ref
                    and r.body.get("status") == "active"
                    for ref in source.order_refs
                ),
                doctor_active=doctor is not None and doctor.body.get("approved") is True,
            ),
            now,
            builder.policy.timing,
        )
        builder.add(result)
        if isinstance(result, ev.TransitionResult) and isinstance(result.aggregate, Mission):
            builder.puts[("mission", source.id)] = to_record(
                prime(result.aggregate, now + builder.policy.operations.retry_backoff(1)), scope
            )
        return
    intents = [
        from_record(r, OutboundIntent)
        for r in records(store, scope, "outbound_intent")
        if r.body.get("notification_purpose") == "routine_prompt"
    ]
    own = [
        i
        for i in intents
        if any(
            ref.entity_type == source.entity_type and ref.id == source.id
            for ref in i.source_versions
        )
    ]
    accepted = [
        i.accepted_at
        for i in intents
        if i.contact_kind == "chase" and i.status == "provider_accepted" and i.accepted_at
    ]
    if accepted:
        profile = profile.model_copy(update={"last_chase_accepted_at": max(accepted)})
    for intent in own:
        if intent.status in {"queued", "sending"} and intent.expires_at <= now and intent.slot_id:
            _missed(builder, source, intent.slot_id)
    if any(
        i.status in {"queued", "sending"}
        or (i.status == "provider_accepted" and i.contact_feedback == "pending")
        for i in own
    ):
        _rearm(builder, source, now + builder.policy.operations.retry_backoff(1))
        return
    if (
        not profile.binding_active
        or not profile.consent_active
        or consent.withdrawn_at
        or not consent.routine_contact_enabled
        or not profile.routine_contact_enabled
        or patient.contact_status in {"opted_out", "frozen", "awaiting_link"}
    ):
        return
    pause = profile.routine_paused_until
    if (
        isinstance(source, Mission)
        and source.details.kind == "TASK"
        and (source.details.completion_rule == "unsupported_action")
    ):
        return
    if isinstance(source, Mission) and _visit_brief(
        builder, source, patient, consent, profile, own
    ):
        return
    if isinstance(source, Mission) and source.details.kind != "MONITOR":
        plan = plan_next_contact(
            source, patient, consent, profile, POLICY, builder.policy.timing, now
        )
        if plan is None:
            _arm_visit_brief(builder, source, patient, consent, own)
            return
        at = plan.at
        # A consumed/uncertain slot or exhausted window is never replayed as a new contact.
        occupied = any(
            i.slot_id == plan.slot_id and i.status not in {"suppressed", "failed"} for i in intents
        )
        budget_failed = any(
            i.slot_id == plan.slot_id and i.suppression_reason == "budget" for i in own
        )
        if plan.window_end <= now or occupied or budget_failed:
            if plan.window_end <= now:
                _missed(builder, source, plan.slot_id)
            at = tomorrow(now, patient, consent, POLICY)
            if profile.last_chase_accepted_at:
                at = max(at, profile.last_chase_accepted_at + POLICY.chase_min_gap)
            if at >= source.escalation_at:
                _rearm(builder, source, None)
                return
        template = "patient_chase_" + (
            "medication_start" if source.kind == "MEDICATION" else source.kind.lower()
        )
        emit = at <= now
        if emit:
            patient_text(store, source, patient, template)
        event = ev.ContactScheduled(
            event_id=builder.command.command_id,
            next_contact_at=at,
            slot_id="chase:" + at.astimezone(ZoneInfo(patient.timezone)).date().isoformat(),
            template_id=template,
            kind="chase",
            emit=emit,
            expires_at=min(at + POLICY.scheduled_prompt_window, source.escalation_at),
        )
        builder.add(transition_mission(source, event, now, builder.policy.timing))
        _arm_visit_brief(builder, source, patient, consent, own)
        return
    if isinstance(source, Mission):
        if source.state not in {"open", "waiting_patient"} or source.details.kind != "MONITOR":
            return
        slots = [
            (f"monitor:{source.id}:{index}", at, at + POLICY.scheduled_prompt_window)
            for index, at in enumerate(source.details.slots)
        ]
        from sanad.monitor.executor import current_details
        from sanad.monitor.slots import filled

        occupied_slots = filled(current_details(store, builder.scope, source))
        slots = [
            (slot, at, end)
            for slot, at, end in slots
            if int(slot.rsplit(":", 1)[1]) not in occupied_slots
        ]
    else:
        if source.state != "scheduled" or source.prompt_at is None or source.due_at is None:
            return
        slots = [
            (
                source.consent_slot_id or source.id,
                source.prompt_at,
                min(source.prompt_at + POLICY.day3_prompt_window, source.due_at),
            )
        ]
    next_at = None
    for slot, at, end in slots:
        if end <= now:
            if not any(
                i.slot_id == slot and i.status in {"provider_accepted", "uncertain"} for i in own
            ):
                _missed(builder, source, slot)
            continue
        if any(
            i.slot_id == slot and i.status in {"provider_accepted", "uncertain", "failed"}
            for i in own
        ):
            continue
        if at > now:
            next_at = at if next_at is None else min(at, next_at)
            continue
        if (
            quiet_at(at, patient.timezone, consent.quiet_hours)
            and slot not in consent.scheduled_slot_consents
            and slot.removeprefix("monitor:") not in consent.scheduled_slot_consents
        ):
            _slot_review(builder, source, slot)
            continue
        if pause and pause > now:
            next_at = min(pause, end) if next_at is None else min(next_at, pause, end)
            continue
        template = (
            "patient_monitor_prompt" if isinstance(source, Mission) else "patient_day3_prompt"
        )
        patient_text(store, source, patient, template)
        if isinstance(source, Mission):
            builder.add(
                transition_mission(
                    source,
                    ev.ContactScheduled(
                        event_id=builder.command.command_id,
                        next_contact_at=at,
                        slot_id=slot,
                        template_id=template,
                        kind="scheduled",
                        expires_at=end,
                    ),
                    now,
                    builder.policy.timing,
                )
            )
        else:
            builder.add(
                transition_followup(
                    source,
                    ev.PromptScheduled(
                        event_id=builder.command.command_id,
                        slot_id=slot,
                        expires_at=end,
                    ),
                    now,
                    builder.policy.timing,
                )
            )
        # Serialize slots through one version. A short clock discovers another current slot.
        changed_row = builder.puts[(source.entity_type, source.id)]
        changed = from_record(changed_row, type(source))
        if changed.work_clock:
            changed = changed.model_copy(
                update={
                    "work_clock": changed.work_clock.model_copy(
                        update={
                            "next_action_at": min(
                                changed.work_clock.next_action_at,
                                now + builder.policy.operations.retry_backoff(1),
                            )
                        }
                    )
                }
            )
            builder.puts[(source.entity_type, source.id)] = to_record(changed, scope)
        return
    _rearm(builder, source, next_at)


def _brief_key(source: Mission) -> str:
    return f"visit-brief:{source.id}:{source.deadline_generation}"


def _arm_visit_brief(
    builder: CommitBuilder,
    source: Mission,
    patient: Patient,
    consent: Consent,
    own: list[OutboundIntent],
) -> None:
    from sanad.concierge.visits import brief_at

    at = brief_at(source, patient, consent)
    if (
        at is None
        or at <= builder.now
        or any(_brief_key(source) in i.source_event_ids for i in own)
    ):
        return
    row = builder.puts.get((source.entity_type, source.id))
    if row:
        current = from_record(row, Mission)
        builder.puts[(source.entity_type, source.id)] = to_record(prime(current, at), builder.scope)
    else:
        _rearm(builder, source, at)


def _visit_brief(
    builder: CommitBuilder,
    source: Mission,
    patient: Patient,
    consent: Consent,
    profile: PatientProfile,
    own: list[OutboundIntent],
) -> bool:
    from sanad.concierge.visits import brief_at
    from sanad.contact.ladder import after_quiet
    from sanad.domain.entities import VisitDetails

    at = brief_at(source, patient, consent)
    if at is None or at > builder.now or not isinstance(source.details, VisitDetails):
        return False
    if any(_brief_key(source) in i.source_event_ids for i in own):
        return False
    end = min(source.due_at, source.details.window_start or source.due_at)
    if end <= builder.now or patient.contact_status == "unreachable":
        return False
    if source.contact_count >= POLICY.per_mission_chase_limit:
        return False
    at = after_quiet(
        max(
            at,
            builder.now,
            profile.routine_paused_until or at,
            (profile.last_chase_accepted_at + POLICY.chase_min_gap)
            if profile.last_chase_accepted_at
            else at,
        ),
        patient,
        consent,
    )
    if at >= end:
        return False
    if at > builder.now:
        _rearm(builder, source, at)
        return True
    builder.add(
        transition_mission(
            source,
            ev.ContactScheduled(
                event_id=_brief_key(source),
                next_contact_at=at,
                slot_id="chase:" + at.astimezone(ZoneInfo(patient.timezone)).date().isoformat(),
                template_id="patient_visit_brief",
                kind="chase",
                expires_at=min(at + POLICY.scheduled_prompt_window, end),
            ),
            builder.now,
            builder.policy.timing,
        )
    )
    return True
