"""Narrow write authority for the receipt-bound deterministic patient executor."""

from datetime import datetime
from typing import TYPE_CHECKING

from sanad.concierge.plan import authorized
from sanad.concierge.records import PatientAction, ReportFactPayload
from sanad.scribe.records import ClinicalFact
from sanad.store.records import (
    CommitRequest,
    Consent,
    Patient,
    PatientBinding,
    PatientProfile,
    StoredRecord,
    from_record,
)

if TYPE_CHECKING:
    from sanad.concierge.plan import Snapshot
    from sanad.store._base import Check, StoreBase

KINDS = frozenset(
    {"ConciergeReply", "CreateSupportTicket", "RecordPatientReply", "SetContactPreference"}
)
ALLOWED = frozenset(
    {
        "patient_action",
        "clinical_fact",
        "mission",
        "followup",
        "review",
        "patient",
        "patient_profile",
        "consent",
        "patient_binding",
        "evidence_annotation",
        "media_work",
        "outbound_intent",
    }
)


def guards(store: "StoreBase", request: CommitRequest, now: datetime) -> list["Check"] | None:
    from sanad.store._base import Check

    command = request.command
    actor = command.principal
    if (
        command.payload.get("type") not in KINDS
        or not actor.bot_id
        or not command.work_claim
        or not command.fence
        or not request.receipt_completion
    ):
        return None
    auth = store.authorize(actor.bot_id, actor.subject)
    if not auth.binding:
        return None
    snap = authorized(store, actor, auth.binding, now)
    if (
        not snap
        or snap.scope != command.scope
        or command.payload.get("receipt_id") != command.work_claim.record_key.pk
    ):
        return None
    receipt = store.get(snap.scope, "inbound_receipt", command.work_claim.record_key.pk)
    if (
        not receipt
        or receipt.body.get("source_subject") != actor.subject
        or receipt.body.get("source_chat") != snap.binding.private_chat_id
    ):
        return None
    if any(r.entity_type not in ALLOWED for r in request.puts):
        return None
    subject_row = store.get(auth.binding.scope, "subject_binding", auth.binding.id)
    doctor_row = store.get(snap.scope, "doctor", snap.doctor.id)
    if not subject_row or not doctor_row:
        return None
    checks = [
        Check(subject_row.key, subject_row.version),
        Check(doctor_row.key, doctor_row.version),
    ]
    for model in (snap.patient, snap.consent, snap.binding):
        row = store.get(snap.scope, model.entity_type, model.id)
        assert row
        checks.append(Check(row.key, row.version))
    changed = {
        r.entity_type: r
        for r in request.puts
        if r.entity_type in {"patient", "consent", "patient_binding", "patient_profile"}
    }
    if changed and command.payload.get("type") == "RecordPatientReply":
        if not _start_clarification_revision(snap, changed, now):
            return None
        changed = {}
    if changed:
        if command.payload.get("type") != "SetContactPreference" or set(changed) != {
            "patient",
            "consent",
            "patient_binding",
            "patient_profile",
        }:
            return None
        patient = from_record(changed["patient"], Patient)
        consent = from_record(changed["consent"], Consent)
        binding = from_record(changed["patient_binding"], PatientBinding)
        profile = from_record(changed["patient_profile"], PatientProfile)
        if not (
            consent.version
            == snap.consent.version + 1
            == patient.consent_version
            == binding.consent_version
            == profile.consent_version
            and profile.consent_active
            and profile.binding_active
            and patient.delivery_epoch == profile.delivery_epoch == snap.profile.delivery_epoch + 1
            and profile.routine_contact_enabled == consent.routine_contact_enabled
            and profile.routine_paused_until == patient.resume_at
        ):
            return None
        for old, new, fields in (
            (
                snap.patient,
                patient,
                {
                    "version",
                    "record_version",
                    "updated_at",
                    "consent_version",
                    "delivery_epoch",
                    "contact_status",
                    "resume_at",
                },
            ),
            (
                snap.consent,
                consent,
                {
                    "version",
                    "updated_at",
                    "routine_contact_enabled",
                    "quiet_hours",
                    "scheduled_slot_consents",
                },
            ),
            (snap.binding, binding, {"version", "updated_at", "consent_version"}),
            (
                snap.profile,
                profile,
                {
                    "version",
                    "updated_at",
                    "consent_version",
                    "delivery_epoch",
                    "routine_contact_enabled",
                    "routine_paused_until",
                },
            ),
        ):
            if old.model_dump(exclude=fields) != new.model_dump(exclude=fields):
                return None
        if not snap.consent.routine_contact_enabled and consent.routine_contact_enabled:
            if not any(
                r.entity_type == "patient_action"
                and r.body.get("action") == "resume"
                and r.body.get("consumed_at")
                for r in request.puts
            ):
                return None
    for row in request.puts:
        if (
            row.entity_type == "outbound_intent"
            and command.payload.get("type") == "RecordPatientReply"
            and _medication_suppression(store, snap, request, row, now, checks)
        ):
            continue
        if row.entity_type == "outbound_intent":
            previous = store.get(snap.scope, "outbound_intent", row.id)
            fields = {"version", "updated_at", "status", "suppression_reason", "work_clock"}
            if (
                not previous
                or previous.body.get("status") != "queued"
                or row.body.get("status") != "suppressed"
                or command.payload.get("type") != "SetContactPreference"
                or {k: v for k, v in previous.body.items() if k not in fields}
                != {k: v for k, v in row.body.items() if k not in fields}
            ):
                return None
        if row.entity_type == "clinical_fact":
            fact = from_record(row, ClinicalFact)
            if (
                fact.version != 1
                or fact.category != "patient_report"
                or fact.visibility != "patient_released"
                or not isinstance(fact.payload, ReportFactPayload)
                or fact.provenance.actor_kind != "patient"
                or fact.provenance.actor_id != actor.subject
                or fact.provenance.source_kind != "patient_report"
                or fact.provenance.source_observation_id != receipt.id
            ):
                return None
        if row.entity_type == "patient_action":
            action = from_record(row, PatientAction)
            if action.actor_subject != actor.subject:
                return None
            if action.consumed_at:
                previous = store.get(snap.scope, "patient_action", row.id)
                payload = receipt.body.get("payload")
                if (
                    not previous
                    or not isinstance(payload, dict)
                    or payload.get("callback_token_hash") != row.id
                ):
                    return None
                prior_action = from_record(previous, PatientAction)
                if (
                    prior_action.consumed_at
                    or prior_action.expires_at <= now
                    or prior_action.delivery_epoch != snap.profile.delivery_epoch
                    or prior_action.binding_epoch != snap.profile.binding_epoch
                    or prior_action.consent_version != snap.consent.version
                    or action.model_dump(exclude={"version", "updated_at", "consumed_at"})
                    != prior_action.model_dump(exclude={"version", "updated_at", "consumed_at"})
                ):
                    return None
                checks.append(Check(previous.key, previous.version))
        if row.entity_type == "mission":
            if command.payload.get(
                "type"
            ) == "RecordPatientReply" and _medication_barrier_projection(snap, request, row, now):
                continue
            if row.version > 1:
                from sanad.contact.scheduler import prime
                from sanad.domain import (
                    DRAFT_POLICY_2026_09,
                    Mission,
                    PatientReplied,
                    TransitionResult,
                    transition_mission,
                )

                original = next((m for m in snap.missions if m.id == row.id), None)
                if original:
                    reply = transition_mission(
                        original,
                        PatientReplied(event_id=command.command_id + ":reply:" + row.id),
                        now,
                        DRAFT_POLICY_2026_09,
                    )
                    if (
                        isinstance(reply, TransitionResult)
                        and isinstance(reply.aggregate, Mission)
                        and prime(reply.aggregate, now).model_dump(mode="json") == row.body
                    ):
                        continue
                if (
                    original
                    and command.payload.get("type") == "RecordPatientReply"
                    and original.kind in {"VISIT", "TASK"}
                ):
                    from sanad.domain import events as ev
                    from sanad.domain.entities import VisitDetails
                    from sanad.domain.predicates import PredicateResult
                    from sanad.store.records import InboundReceipt, to_record

                    reported = [
                        from_record(r, ClinicalFact)
                        for r in request.puts
                        if r.entity_type == "clinical_fact"
                    ]
                    report = next(
                        (
                            f.payload
                            for f in reported
                            if isinstance(f.payload, ReportFactPayload)
                            and f.payload.target_ref == to_record(original, snap.scope).ref
                        ),
                        None,
                    )
                    if report is not None:
                        if report.report_kind not in (
                            {"task_done"}
                            if original.kind == "TASK"
                            else {"visit_booking", "visit_attendance", "visit_report_pending"}
                        ):
                            return None
                        revised_mission = from_record(row, Mission)
                        at = from_record(receipt, InboundReceipt).received_at
                        if report.original_receipt_id:
                            source_row = store.get(
                                snap.scope, "inbound_receipt", report.original_receipt_id
                            )
                            if (
                                not source_row
                                or source_row.body.get("source_subject") != actor.subject
                            ):
                                return None
                            at = from_record(source_row, InboundReceipt).received_at
                            checks.append(Check(source_row.key, source_row.version))
                        base = original
                        if isinstance(original.details, VisitDetails):
                            # Window-only revisions preserve every clinical and lifecycle field.
                            if not isinstance(revised_mission.details, VisitDetails) or (
                                original.details.model_dump(exclude={"window_start", "window_end"})
                                != revised_mission.details.model_dump(
                                    exclude={"window_start", "window_end"}
                                )
                            ):
                                return None
                            base = original.model_copy(update={"details": revised_mission.details})
                            if (
                                revised_mission.state == original.state
                                and revised_mission
                                == Mission.model_validate(
                                    base.model_dump()
                                    | {"version": original.version + 1, "updated_at": now}
                                )
                            ):
                                continue
                            if not (
                                original.details.objective in {"arrange", "booking_reported"}
                                and report.report_kind == "visit_booking"
                                or original.details.objective == "attendance_reported"
                                and report.report_kind == "visit_attendance"
                            ):
                                return None
                        fulfillment = transition_mission(
                            base,
                            ev.ObjectiveFulfilled(
                                event_id=command.command_id,
                                fulfillment_event_id=command.command_id,
                                actor_kind="patient",
                                objective_received_at=at,
                                danger_flag=False,
                                predicate_result=PredicateResult(
                                    satisfied=True,
                                    evaluated_at=now,
                                    detail="Explicit patient report.",
                                ),
                            ),
                            now,
                            DRAFT_POLICY_2026_09,
                        )
                        if (
                            isinstance(fulfillment, TransitionResult)
                            and fulfillment.aggregate == revised_mission
                        ):
                            continue
            if row.version == 1 and (
                row.body.get("kind") != "QUESTION"
                or command.payload.get("type") != "CreateSupportTicket"
            ):
                return None
            if row.body.get("kind") == "MONITOR":
                from sanad.monitor.guard import permits as monitor_permits

                if monitor_permits(store, snap, request, row, now):
                    continue
            if row.version > 1 and (
                row.body.get("kind") != "MEDICATION"
                or row.body.get("state") != "fulfilled"
                or command.payload.get("type") != "RecordPatientReply"
            ):
                return None
    for row in request.intents:
        if (
            row.body.get("audience") == "doctor"
            and row.body.get("notification_purpose") != "DONE:FULFILLMENT"
        ):
            return None
        if (
            row.body.get("audience") == "patient"
            and row.body.get("notification_purpose") != "solicited_reply"
        ):
            return None
    return checks


def _start_clarification_revision(
    snap: "Snapshot", changed: dict[str, StoredRecord], now: datetime
) -> bool:
    """Only clarification metadata; the surrounding guard still owns authority."""
    from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY
    from sanad.concierge.reports import medication_missions

    if set(changed) != {"patient_profile"}:
        return False
    profile = from_record(changed["patient_profile"], PatientProfile)
    fields = {"pending_start_clarification", "version", "updated_at"}
    if (
        profile.version != snap.profile.version + 1
        or profile.updated_at != now
        or profile.model_dump(exclude=fields) != snap.profile.model_dump(exclude=fields)
    ):
        return False
    pending = profile.pending_start_clarification
    if pending is None:
        return snap.profile.pending_start_clarification is not None
    return (
        pending.asked_at == now
        and pending.expires_at == now + DRAFT_CONCIERGE_POLICY.start_clarification_window
        and any(
            m.id == pending.mission_ref.id and m.version == pending.mission_ref.version
            for m in medication_missions(snap, "", "START", barrier=True)
        )
    )


def _medication_barrier_projection(
    snap: "Snapshot", request: CommitRequest, row: StoredRecord, now: datetime
) -> bool:
    from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY
    from sanad.concierge.reports import is_start, medication_missions, recognize_barrier
    from sanad.domain import DRAFT_POLICY_2026_09, Mission, TransitionResult, transition_mission
    from sanad.domain.events import BarrierRecorded, BarrierResolved

    original = next(
        (m for m in medication_missions(snap, "", "START", barrier=True) if m.id == row.id), None
    )
    if original is None:
        return False
    for report in request.puts:
        if report.entity_type != "clinical_fact":
            continue
        fact = from_record(report, ClinicalFact)
        payload = fact.payload
        if not isinstance(payload, ReportFactPayload) or payload.target_ref != row.ref.model_copy(
            update={"version": original.version}
        ):
            continue
        event: BarrierRecorded | BarrierResolved | None = None
        if (
            payload.report_kind == "barrier"
            and payload.barrier_type
            and payload.barrier_type == recognize_barrier(payload.text)
        ):
            event = BarrierRecorded(
                event_id=request.command.command_id + ":barrier:" + original.id,
                barrier_type=payload.barrier_type,
                reason=payload.text,
                resume_at=now + DRAFT_CONCIERGE_POLICY.barrier_resume_after,
            )
        elif (
            original.state == "blocked"
            and payload.report_kind == "medication_start"
            and is_start(payload.text)
        ):
            event = BarrierResolved(
                event_id=request.command.command_id + ":barrier-resolved:" + original.id
            )
        if event is not None:
            result = transition_mission(original, event, now, DRAFT_POLICY_2026_09)
            if (
                isinstance(result, TransitionResult)
                and isinstance(result.aggregate, Mission)
                and result.aggregate.model_dump(mode="json") == row.body
            ):
                return True
    return False


def _medication_suppression(
    store: "StoreBase",
    snap: "Snapshot",
    request: CommitRequest,
    row: StoredRecord,
    now: datetime,
    checks: list["Check"],
) -> bool:
    from sanad.steward.apply import is_routine
    from sanad.store import keys
    from sanad.store._base import Check
    from sanad.store.records import OutboundIntent

    previous = store.get(snap.scope, "outbound_intent", row.id)
    if previous is None:
        return False
    old, new = from_record(previous, OutboundIntent), from_record(row, OutboundIntent)
    from pydantic import TypeAdapter, ValidationError

    from sanad.domain.events import SuppressRoutineIntents

    try:
        declared = TypeAdapter(tuple[SuppressRoutineIntents, ...]).validate_python(
            request.command.payload.get("medication_suppressions", [])
        )
    except ValidationError:
        return False
    scoped_refs = {
        ref
        for effect in declared
        if effect.reason == new.suppression_reason and effect.order_refs is not None
        for ref in effect.order_refs
    }
    fields = {"version", "updated_at", "status", "suppression_reason", "work_clock"}
    if (
        old.status != "queued"
        or not is_routine(old)
        or not old.order_refs
        or new.status != "suppressed"
        or not new.suppression_reason
        or new.work_clock is not None
        or new.version != old.version + 1
        or new.updated_at != now
        or old.model_dump(exclude=fields) != new.model_dump(exclude=fields)
    ):
        return False
    for ref in old.order_refs:
        if ref not in scoped_refs:
            continue
        order = store.get(snap.scope, "care_order", ref.id)
        if new.suppression_reason == "order_superseded" and (
            order is None or order.ref != ref or order.body.get("status") != "active"
        ):
            checks.extend(
                (
                    Check(previous.key, previous.version),
                    Check(order.key, order.version)
                    if order
                    else Check(keys.patient(snap.scope, "ORDER", ref.id), None),
                )
            )
            return True
        for mission in request.puts:
            if mission.entity_type != "mission" or not _medication_barrier_projection(
                snap, request, mission, now
            ):
                continue
            from sanad.domain import Mission

            blocked = from_record(mission, Mission)
            if ref in blocked.order_refs and blocked.barrier_reason == new.suppression_reason:
                checks.append(Check(previous.key, previous.version))
                return True
    return False
