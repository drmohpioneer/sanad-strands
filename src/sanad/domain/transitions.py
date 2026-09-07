"""Total, pure transition functions; effects are committed by a later Steward/store."""

from datetime import datetime
from hashlib import sha256
from typing import Literal

from pydantic import ValidationError

import sanad.domain.events as ev
from sanad.domain.deadlines import (
    NeedsClarification,
    followup_due_at,
    medication_day3_prompt_at,
    next_action_at,
    resolve_timing,
    utc_instant,
)
from sanad.domain.entities import (
    TERMINAL_STATES,
    CoverageStatus,
    DeadlineHistory,
    DoctorTimingPolicy,
    FollowUpKind,
    FollowUpState,
    FollowUpTask,
    FulfillmentValidity,
    MedicationDetails,
    Mission,
    MissionKind,
    MissionState,
    QuestionDetails,
    ReviewAction,
    ReviewKind,
    ReviewObligation,
    ReviewState,
    Timeliness,
    TimingAnchor,
    WorkClock,
    review_source_key,
)
from sanad.domain.operations import AccountabilityWake
from sanad.domain.predicates import DoctorAnswerPredicate, PatientReportPredicate

STATE_PRESERVING_EVENTS: frozenset[type[ev.MissionEvent]] = frozenset(
    {
        ev.CorrectAcceptedEvidence,
        ev.LateInputRecorded,
        ev.ReviewAcknowledged,
        ev.ReviewResolved,
        ev.SafetyIncidentRaised,
        ev.ContactPreferenceChanged,
    }
)
_UNFINISHED: frozenset[type[ev.MissionEvent]] = frozenset(
    {
        ev.PatientBound,
        ev.PauseContact,
        ev.DeadlineReached,
        ev.ObjectiveFulfilled,
        ev.DoctorExtend,
        ev.DoctorCancel,
        ev.DoctorCloseUnfulfilled,
        ev.OrderSuperseded,
        ev.EvidenceAssociated,
    }
)
LEGAL_TRANSITIONS: dict[MissionState, frozenset[type[ev.MissionEvent]]] = {
    MissionState.proposed: STATE_PRESERVING_EVENTS
    | {
        ev.ConfirmMission,
        ev.DoctorCancel,
        ev.OrderSuperseded,
    },
    MissionState.awaiting_link: STATE_PRESERVING_EVENTS | _UNFINISHED,
    MissionState.open: STATE_PRESERVING_EVENTS
    | _UNFINISHED
    | {
        ev.ContactAccepted,
        ev.ContactScheduled,
        ev.PatientReplied,
        ev.BarrierRecorded,
        ev.ContactExhausted,
    },
    MissionState.waiting_patient: STATE_PRESERVING_EVENTS
    | _UNFINISHED
    | {
        ev.ContactAccepted,
        ev.ContactScheduled,
        ev.PatientReplied,
        ev.BarrierRecorded,
        ev.ContactExhausted,
    },
    MissionState.blocked: STATE_PRESERVING_EVENTS
    | _UNFINISHED
    | {
        ev.PatientReplied,
        ev.BarrierRecorded,
        ev.BarrierResolved,
        ev.ContactExhausted,
        ev.ResumeContact,
    },
    MissionState.unreachable: STATE_PRESERVING_EVENTS
    | _UNFINISHED
    | {
        ev.ContactAccepted,
        ev.PatientReplied,
        ev.BarrierRecorded,
        ev.ContactExhausted,
    },
    MissionState.overdue: STATE_PRESERVING_EVENTS
    | _UNFINISHED
    | {
        ev.ContactAccepted,
        ev.PatientReplied,
        ev.BarrierRecorded,
        ev.BarrierResolved,
        ev.ResumeContact,
    },
    MissionState.fulfilled: STATE_PRESERVING_EVENTS | {ev.DoctorReopen, ev.ValidateCorrection},
    MissionState.cancelled: STATE_PRESERVING_EVENTS | {ev.DoctorReopen},
    MissionState.closed_unfulfilled: STATE_PRESERVING_EVENTS | {ev.DoctorReopen},
    MissionState.superseded: STATE_PRESERVING_EVENTS,
}

LEGAL_FOLLOWUP_TRANSITIONS: dict[FollowUpState, frozenset[type[ev.FollowUpEvent]]] = {
    FollowUpState.awaiting_anchor: frozenset(
        {
            ev.AnchorConfirmed,
            ev.FollowUpDeadline,
            ev.SuppressFollowUpContact,
            ev.CancelFollowUp,
        }
    ),
    FollowUpState.scheduled: frozenset(
        {
            ev.AnchorConfirmed,
            ev.PromptAccepted,
            ev.PromptScheduled,
            ev.ResponseReceived,
            ev.FollowUpDeadline,
            ev.SuppressFollowUpContact,
            ev.CancelFollowUp,
        }
    ),
    FollowUpState.waiting_response: frozenset(
        {
            ev.ResponseReceived,
            ev.FollowUpDeadline,
            ev.SuppressFollowUpContact,
            ev.CancelFollowUp,
        }
    ),
    # A20's idempotent deadline is admitted here solely for the handled replay guard.
    FollowUpState.overdue: frozenset(
        {
            ev.ResponseReceived,
            ev.FollowUpDeadline,
            ev.CancelFollowUp,
            ev.SuppressFollowUpContact,
        }
    ),
    FollowUpState.contact_suppressed: frozenset(
        {ev.AnchorConfirmed, ev.ResponseReceived, ev.CancelFollowUp}
    ),
    FollowUpState.fulfilled: frozenset(),
    FollowUpState.cancelled: frozenset(),
}
_OPEN_REVIEW: frozenset[type[ev.ReviewEvent]] = frozenset(
    {
        ev.AcknowledgeReview,
        ev.ResolveReview,
        ev.BlockCoverage,
        ev.RestoreCoverage,
        ev.MaterialChange,
    }
)
LEGAL_REVIEW_TRANSITIONS: dict[ReviewState, frozenset[type[ev.ReviewEvent]]] = {
    ReviewState.open: _OPEN_REVIEW,
    ReviewState.acknowledged: _OPEN_REVIEW,
    ReviewState.resolved: frozenset(),
}
ALLOWED_REVIEW_ACTIONS: dict[ReviewKind, frozenset[ReviewAction]] = {
    ReviewKind.result_review: frozenset({ReviewAction.review}),
    ReviewKind.correction_disposition: frozenset({ReviewAction.review}),
    ReviewKind.incident_response: frozenset({ReviewAction.resolve_incident}),
    ReviewKind.unmet_objective: frozenset(
        {
            ReviewAction.extend,
            ReviewAction.close,
            ReviewAction.cancel,
            ReviewAction.review,
        }
    ),
    ReviewKind.question_answer: frozenset({ReviewAction.answer, ReviewAction.close}),
    ReviewKind.media_failure: frozenset({ReviewAction.dispose, ReviewAction.review}),
    ReviewKind.delivery_failure: frozenset({ReviewAction.dispose}),
    ReviewKind.binding_review: frozenset({ReviewAction.review, ReviewAction.dispose}),
    ReviewKind.coverage_review: frozenset({ReviewAction.restore_coverage, ReviewAction.review}),
    ReviewKind.followup_disposition: frozenset({ReviewAction.dispose, ReviewAction.review}),
    ReviewKind.evidence_association: frozenset({ReviewAction.associate, ReviewAction.dispose}),
    ReviewKind.intake_clarification: frozenset({ReviewAction.clarify, ReviewAction.dispose}),
}

type Outcome = ev.TransitionResult | ev.TransitionRejected
type Event = ev.MissionEvent | ev.FollowUpEvent | ev.ReviewEvent


def _reject(
    aggregate: ev.Aggregate,
    event: Event,
    code: str,
    message: str,
    effects: tuple[ev.Effect, ...] = (),
) -> ev.TransitionRejected:
    return ev.TransitionRejected(
        reason_code=code,
        message=message,
        state=aggregate.state,
        event_type=event.event_type,
        effects=effects,
    )


def _illegal(aggregate: ev.Aggregate, event: Event) -> ev.TransitionRejected:
    return _reject(
        aggregate,
        event,
        "illegal_transition",
        f"{event.event_type} is illegal from {aggregate.state.value}.",
    )


def _noop(aggregate: ev.Aggregate) -> ev.TransitionResult:
    return ev.TransitionResult(aggregate=aggregate, noop=True)


def _audit(event: Event, before: int, after: int) -> ev.RecordAudit:
    return ev.RecordAudit(
        event_type=event.event_type,
        event_id=event.event_id,
        before_version=before,
        after_version=after,
    )


def _intent(
    event: Event,
    version: int,
    purpose: Literal["DANGER", "DONE:FULFILLMENT", "DEADLINE", "routine_prompt"],
    **fields: object,
) -> ev.EmitIntent:
    return ev.EmitIntent.model_validate(
        dict(
            purpose=purpose,
            source_event_id=event.event_id,
            source_version=version,
            facts_ref=event.event_id,
        )
        | fields
    )


def _review(
    aggregate: Mission | FollowUpTask,
    event: Event,
    kind: ReviewKind,
    review_at: datetime,
    *,
    rejected: bool = False,
) -> ev.CreateReview:
    return ev.CreateReview(
        event_id=event.event_id,
        review_kind=kind,
        source_type=aggregate.entity_type,
        source_id=aggregate.id,
        source_version=aggregate.version + (0 if rejected else 1),
        review_at=review_at,
        owner_doctor_id=aggregate.doctor_id,
        patient_id=aggregate.patient_id,
        source_mission_id=aggregate.id
        if isinstance(aggregate, Mission)
        else aggregate.parent_mission_id,
    )


def _clock(
    aggregate: ev.Aggregate,
    at: datetime,
    lane: Literal["mission", "followup", "review"],
) -> WorkClock:
    previous = aggregate.work_clock
    return WorkClock(
        next_action_at=at,
        work_lane=lane,
        work_shard=previous.work_shard if previous else "0",
        work_generation=aggregate.last_work_generation + 1,
        attempt_count=previous.attempt_count if previous else 0,
        last_error_code=previous.last_error_code if previous else None,
    )


def _finish_mission(
    mission: Mission,
    event: ev.MissionEvent,
    now: datetime,
    policy: DoctorTimingPolicy,
    changes: dict[str, object],
    effects: list[ev.Effect],
) -> Outcome:
    data = mission.model_dump() | changes
    data.update(version=mission.version + 1, updated_at=now)
    if data["state"] in TERMINAL_STATES:
        data["work_clock"] = None
    else:
        interval = (
            policy.draft_review_interval
            if data["state"] == MissionState.proposed
            else (policy.overdue_review_interval)
        )
        review_at = data["review_at"]
        assert isinstance(review_at, datetime)
        if review_at <= now:
            data["review_at"] = now + interval
        resume_at = data["resume_at"]
        if isinstance(resume_at, datetime) and resume_at <= now:
            data["resume_at"] = None
        if data["state"] == MissionState.blocked and data["resume_at"] is None:
            return _reject(mission, event, "resume_required", "Resolve the expired pause first.")
        data["work_clock"] = _clock(mission, now + interval, "mission")
        data["last_work_generation"] = mission.last_work_generation + 1
    try:
        updated = Mission.model_validate(data)
        at = next_action_at(updated, now)
        if at is not None:
            if at <= now:
                if updated.state == MissionState.proposed:
                    return _reject(
                        mission,
                        event,
                        "timing_needs_clarification",
                        "Confirm with valid timing or cancel the expired proposal first.",
                    )
                return _reject(
                    mission,
                    event,
                    "deadline_requires_handling",
                    "Handle the outstanding deadline before this nonterminal transition.",
                )
            data["work_clock"] = _clock(mission, at, "mission")
            updated = Mission.model_validate(data)
    except ValidationError as error:
        return _reject(mission, event, "invalid_aggregate", str(error))
    return ev.TransitionResult(
        aggregate=updated,
        effects=(*effects, _audit(event, mission.version, updated.version)),
    )


def transition_mission(
    mission: Mission,
    event: ev.MissionEvent | AccountabilityWake,
    now: datetime,
    policy: DoctorTimingPolicy,
) -> Outcome:
    if isinstance(event, AccountabilityWake):
        return _accountability_wake(mission, event, now, policy)
    historical_contact = (
        isinstance(event, ev.ContactAccepted)
        and event.accepted_at is not None
        and (
            mission.state in TERMINAL_STATES | {MissionState.blocked}
            or (
                mission.last_patient_reply_at is not None
                and event.accepted_at <= mission.last_patient_reply_at
            )
        )
    )
    if not historical_contact and type(event) not in LEGAL_TRANSITIONS[mission.state]:
        return _illegal(mission, event)
    now = utc_instant(now)
    if now < mission.updated_at:
        return _reject(
            mission, event, "stale_time", "Transition time precedes the aggregate revision."
        )
    changes: dict[str, object] = {}
    effects: list[ev.Effect] = []
    active = MissionState.open if now < mission.escalation_at else MissionState.overdue
    if isinstance(event, ev.ConfirmMission):
        if mission.due_source == "doctor" and event.explicit is None:
            return _reject(
                mission,
                event,
                "explicit_timing_required",
                "Resupply the explicit doctor time on the confirmation card.",
            )
        timing = resolve_timing(
            mission.kind,
            mission.created_at if mission.kind == MissionKind.QUESTION else now,
            policy,
            explicit=event.explicit,
            proposal=event.proposal,
            schedule_end=event.schedule_end,
            grace_override=event.grace_seconds,
        )
        if isinstance(timing, NeedsClarification):
            return _reject(mission, event, "timing_needs_clarification", timing.message)
        changes.update(timing.model_dump())
        changes.update(
            state=MissionState.open if event.consent_active else MissionState.awaiting_link,
            confirmed_at=now,
            confirmed_by=event.actor_id,
        )
        if isinstance(mission.details, MedicationDetails) and mission.details.action == "START":
            effects.append(
                ev.CreateFollowUp(
                    event_id=event.event_id,
                    kind=FollowUpKind.MEDICATION_DAY3,
                    parent_mission_id=mission.id,
                    order_refs=mission.order_refs,
                    anchor_kind=event.anchor_kind,
                    anchor_time=event.anchor_time,
                    response_predicate=PatientReportPredicate(report_kind="medication_day3"),
                    confirmed_by=event.actor_id,
                    confirmed_at=now,
                    doctor_id=mission.doctor_id,
                    patient_id=mission.patient_id,
                )
            )
    elif isinstance(event, ev.PatientBound):
        if not event.consent_active:
            return _reject(
                mission, event, "consent_required", "Active consented binding is required."
            )
        if mission.state != MissionState.awaiting_link:
            return _noop(mission)
        changes["state"] = active
    elif isinstance(event, ev.DeadlineReached):
        if mission.handled_deadline_generation == mission.deadline_generation:
            return _noop(mission)
        if now < mission.escalation_at:
            return _reject(
                mission, event, "deadline_not_reached", "Escalation time has not arrived."
            )
        if mission.fulfillment_validity == FulfillmentValidity.valid:
            return _reject(mission, event, "objective_already_fulfilled", "The objective is valid.")
        review_at = now + policy.overdue_review_interval
        changes.update(
            state=MissionState.overdue,
            handled_deadline_generation=mission.deadline_generation,
            latest_deadline_notice_event_id=event.event_id,
            review_at=review_at,
        )
        effects.extend(
            [
                _review(mission, event, ReviewKind.unmet_objective, review_at),
                _intent(event, mission.version + 1, "DEADLINE"),
            ]
        )
    elif isinstance(event, ev.ObjectiveFulfilled):
        if not event.predicate_result.satisfied:
            return _reject(
                mission, event, "predicate_not_satisfied", "Objective evidence is incomplete."
            )
        if mission.kind == MissionKind.QUESTION and event.actor_kind != "doctor":
            return _reject(
                mission, event, "doctor_answer_required", "Only a doctor answer fulfills QUESTION."
            )
        if event.objective_received_at > now or event.predicate_result.evaluated_at > now:
            return _reject(
                mission,
                event,
                "future_evidence",
                "Receipt and evaluation must not be in the future.",
            )
        changes.update(
            state=MissionState.fulfilled,
            fulfillment_validity=FulfillmentValidity.valid,
            fulfillment_event_id=event.fulfillment_event_id,
            fulfilled_at=now,
            objective_received_at=event.objective_received_at,
            timeliness=Timeliness.on_time
            if event.objective_received_at <= mission.due_at
            else Timeliness.late,
            evidence_refs=event.evidence_refs,
            danger_history=mission.danger_history or event.danger_flag,
        )
        if (
            mission.kind in {MissionKind.TEST, MissionKind.MONITOR, MissionKind.SEND_RECORDS}
            or event.danger_flag
        ):
            effects.append(
                _review(
                    mission, event, ReviewKind.result_review, now + policy.result_review_interval
                )
            )
        effects.append(
            _intent(
                event, mission.version + 1, "DANGER" if event.danger_flag else "DONE:FULFILLMENT"
            )
        )
        if isinstance(mission.details, MedicationDetails) and mission.details.action == "START":
            effects.append(
                ev.AnchorFollowUp(
                    parent_mission_id=mission.id,
                    anchor_time=event.objective_received_at,
                    anchor_kind="reported_effective_start",
                )
            )
    elif isinstance(event, (ev.DoctorExtend, ev.DoctorReopen)):
        if event.timing.instant <= now:
            return _reject(
                mission,
                event,
                "timing_needs_clarification",
                "The new deadline must be strictly future.",
            )
        timing = resolve_timing(
            mission.kind,
            now,
            policy,
            explicit=event.timing,
            grace_override=event.grace_seconds,
        )
        if isinstance(timing, NeedsClarification):
            return _reject(mission, event, "timing_needs_clarification", timing.message)
        changes.update(timing.model_dump())
        changes.update(
            state=MissionState.open if event.consent_active else MissionState.awaiting_link,
            timing_anchor=TimingAnchor(
                kind="extension" if isinstance(event, ev.DoctorExtend) else "reopen", instant=now
            ),
            timing_history=(
                *mission.timing_history,
                DeadlineHistory.model_validate(
                    mission.model_dump(include=set(DeadlineHistory.model_fields))
                ),
            ),
            deadline_generation=mission.deadline_generation + 1,
            resume_at=None,
            next_contact_at=None,
            barrier_type=None,
            barrier_reason=None,
        )
        if isinstance(event, ev.DoctorExtend):
            effects.append(
                ev.ResolveReviewEffect(
                    review_kind=ReviewKind.unmet_objective,
                    source_type="mission",
                    source_id=mission.id,
                    reason=event.reason,
                )
            )
        else:
            if any(ref not in event.current_active_order_refs for ref in event.new_order_refs):
                return _reject(
                    mission, event, "superseded_order", "A reopened order is not currently active."
                )
            if (
                isinstance(mission.details, MedicationDetails)
                and mission.details.order_ref not in event.new_order_refs
            ):
                return _reject(
                    mission,
                    event,
                    "order_details_require_revision",
                    "Medication details reference the old order; an explicit revision is required.",
                )
            prior = mission.prior_fulfillment_event_ids
            if mission.fulfillment_event_id is not None:
                prior += (mission.fulfillment_event_id,)
            changes.update(
                confirmed_at=now,
                confirmed_by=event.actor_id,
                objective_predicate=event.new_objective_predicate,
                order_refs=event.new_order_refs,
                prior_fulfillment_event_ids=prior,
                fulfillment_event_id=None,
                fulfilled_at=None,
                objective_received_at=None,
                timeliness=Timeliness.undetermined,
                fulfillment_validity=FulfillmentValidity.not_fulfilled,
                cancellation_reason=None,
            )
    elif isinstance(event, ev.DoctorCancel):
        changes.update(state=MissionState.cancelled, cancellation_reason=event.reason)
        effects.append(ev.SuppressRoutineIntents(reason=event.reason))
        if mission.fulfillment_validity == FulfillmentValidity.invalidated_pending_review:
            effects.append(
                _review(
                    mission,
                    event,
                    ReviewKind.correction_disposition,
                    now + policy.result_review_interval,
                )
            )
    elif isinstance(event, ev.DoctorCloseUnfulfilled):
        if mission.danger_history or event.open_incident:
            return _reject(
                mission,
                event,
                "danger_history_requires_disposition",
                "Danger history or an open incident requires explicit disposition.",
            )
        changes["state"] = MissionState.closed_unfulfilled
    elif isinstance(event, ev.OrderSuperseded):
        changes["state"] = MissionState.superseded
        effects.append(
            ev.SuppressRoutineIntents(
                reason="Order superseded by a confirmed revision or cancellation."
            )
        )
    elif isinstance(event, ev.CorrectAcceptedEvidence):
        if mission.state == MissionState.fulfilled and not event.predicate_still_holds:
            changes["fulfillment_validity"] = FulfillmentValidity.invalidated_pending_review
        changes["evidence_refs"] = tuple(
            event.correcting_evidence_ref if ref == event.superseded_evidence_ref else ref
            for ref in mission.evidence_refs
        )
        effects.extend(
            [
                ev.SupersedeEvidence(
                    superseded_ref=event.superseded_evidence_ref,
                    correcting_ref=event.correcting_evidence_ref,
                ),
                ev.SuppressRoutineIntents(reason="Accepted evidence corrected."),
                _review(
                    mission,
                    event,
                    ReviewKind.correction_disposition,
                    now + policy.result_review_interval,
                ),
            ]
        )
    elif isinstance(event, ev.ValidateCorrection):
        if mission.fulfillment_validity == FulfillmentValidity.valid:
            return _noop(mission)
        changes["fulfillment_validity"] = (
            FulfillmentValidity.valid
            if event.predicate_result.satisfied
            else FulfillmentValidity.invalidated_pending_review
        )
    elif isinstance(event, ev.ContactScheduled):
        if event.emit and (
            event.next_contact_at > now or (event.expires_at and event.expires_at <= now)
        ):
            return _reject(mission, event, "contact_not_due", "Contact is outside its send window.")
        changes["next_contact_at"] = event.next_contact_at
        if event.emit:
            effects.append(
                _intent(
                    event,
                    mission.version + 1,
                    "routine_prompt",
                    audience="patient",
                    slot=event.slot_id,
                    template_id=event.template_id,
                    contact_kind=event.kind,
                    expires_at=event.expires_at,
                )
            )
    elif isinstance(event, ev.ContactAccepted):
        changes.update(
            state=mission.state
            if historical_contact
            else MissionState.waiting_patient
            if active == MissionState.open
            else active,
            contact_count=mission.contact_count + 1,
            unanswered_delivered_count=mission.unanswered_delivered_count
            + (
                event.kind == "chase"
                and (
                    mission.last_patient_reply_at is None
                    or (event.accepted_at or now) > mission.last_patient_reply_at
                )
            ),
            next_contact_at=mission.next_contact_at if historical_contact else None,
        )
        if event.kind == "chase":
            accepted = event.accepted_at or now
            if accepted > now:
                return _reject(
                    mission,
                    event,
                    "future_contact",
                    "Provider acceptance must not be in the future.",
                )
            changes.update(
                first_chase_accepted_at=mission.first_chase_accepted_at or accepted,
                last_chase_accepted_at=accepted,
            )
    elif isinstance(event, ev.PatientReplied):
        changes.update(state=active, unanswered_delivered_count=0, last_patient_reply_at=now)
    elif isinstance(event, (ev.BarrierRecorded, ev.PauseContact)):
        if not now < event.resume_at <= now + policy.pause_max_interval:
            return _reject(
                mission,
                event,
                "pause_out_of_bounds",
                "Resume time must be future and within the pause policy.",
            )
        state: MissionState = active if active == MissionState.overdue else MissionState.blocked
        if mission.state == MissionState.awaiting_link:
            state = MissionState.awaiting_link
        changes.update(
            state=state,
            barrier_reason=event.reason,
            resume_at=event.resume_at,
            next_contact_at=None,
        )
        if isinstance(event, ev.BarrierRecorded):
            changes["barrier_type"] = event.barrier_type
        effects.append(ev.SuppressRoutineIntents(reason=event.reason))
    elif isinstance(event, ev.BarrierResolved):
        changes.update(state=active, barrier_type=None, barrier_reason=None, resume_at=None)
    elif isinstance(event, ev.ResumeContact):
        reviews = tuple(
            _review(mission, event, kind, now + policy.overdue_review_interval, rejected=True)
            for allowed, kind in (
                (event.doctor_active, ReviewKind.coverage_review),
                (event.consent_active, ReviewKind.binding_review),
                (event.order_active, ReviewKind.unmet_objective),
            )
            if not allowed
        )
        if reviews:
            return _reject(
                mission,
                event,
                "contact_authority_required",
                "Current doctor, consent and order authority are required.",
                reviews,
            )
        changes.update(state=active, resume_at=None)
    elif isinstance(event, ev.ContactExhausted):
        if not event.exhausted:
            return _reject(
                mission,
                event,
                "contact_not_exhausted",
                "Delivered-contact exhaustion has not been established.",
            )
        changes.update(state=MissionState.unreachable, next_contact_at=None)
    elif isinstance(event, ev.LateInputRecorded):
        effects.extend(
            [
                ev.RetainObservation(observation_ref=event.observation_ref),
                _review(
                    mission,
                    event,
                    ReviewKind.evidence_association,
                    now + policy.result_review_interval,
                ),
            ]
        )
    elif isinstance(event, ev.EvidenceAssociated):
        if event.evidence_ref not in mission.evidence_refs:
            changes["evidence_refs"] = (*mission.evidence_refs, event.evidence_ref)
        effects.append(ev.RecordEvidenceAssociation(evidence_ref=event.evidence_ref))
    elif isinstance(event, ev.ReviewAcknowledged):
        effects.append(
            ev.ApplyReviewEvent(
                obligation_id=event.obligation_id,
                event=ev.AcknowledgeReview(event_id=event.event_id, actor_id=event.actor_id),
            )
        )
    elif isinstance(event, ev.ReviewResolved):
        effects.append(
            ev.ApplyReviewEvent(
                obligation_id=event.obligation_id,
                event=ev.ResolveReview(
                    event_id=event.event_id,
                    action=event.action,
                    expected_source_version=event.expected_source_version,
                    actor_id=event.actor_id,
                    reason=event.reason,
                ),
            )
        )
    elif isinstance(event, ev.SafetyIncidentRaised):
        changes["danger_history"] = True
    elif isinstance(event, ev.ContactPreferenceChanged):
        changes["next_contact_at"] = None
        effects.append(ev.SuppressRoutineIntents(reason=event.reason))
    else:
        return _illegal(mission, event)
    if historical_contact:
        updated = Mission.model_validate(
            mission.model_dump() | changes | {"version": mission.version + 1, "updated_at": now}
        )
        return ev.TransitionResult(
            aggregate=updated, effects=(_audit(event, mission.version, updated.version),)
        )
    return _finish_mission(mission, event, now, policy, changes, effects)


def _followup_next_action(
    task: FollowUpTask, now: datetime, policy: DoctorTimingPolicy
) -> datetime:
    candidates = [task.review_at if task.review_at > now else now + policy.overdue_review_interval]
    if task.state not in {FollowUpState.overdue, FollowUpState.contact_suppressed}:
        if task.due_at is not None and not task.deadline_handled:
            candidates.append(task.due_at)
        if (
            task.state == FollowUpState.scheduled
            and task.prompt_at is not None
            and task.prompt_at > now
        ):
            candidates.append(task.prompt_at)
    return min(candidates)


def transition_followup(
    task: FollowUpTask,
    event: ev.FollowUpEvent | AccountabilityWake,
    now: datetime,
    policy: DoctorTimingPolicy,
) -> Outcome:
    if isinstance(event, AccountabilityWake):
        return _accountability_wake(task, event, now, policy)
    if type(event) not in LEGAL_FOLLOWUP_TRANSITIONS[task.state]:
        return _illegal(task, event)
    now = utc_instant(now)
    if now < task.updated_at:
        return _reject(
            task, event, "stale_time", "Transition time precedes the aggregate revision."
        )
    changes: dict[str, object] = {}
    effects: list[ev.Effect] = []
    if isinstance(event, ev.AnchorConfirmed):
        if task.anchor_time is not None and not event.replace_existing:
            return _noop(task)
        if task.anchor_time == event.anchor_time and task.anchor_kind == event.anchor_kind:
            return _noop(task)
        if task.kind != FollowUpKind.MEDICATION_DAY3:
            return _reject(
                task,
                event,
                "explicit_prompt_required",
                "A clinical check-in requires an explicit prompt revision.",
            )
        prompt = medication_day3_prompt_at(event.anchor_time, policy)
        due = followup_due_at(prompt, policy)
        changes.update(
            state=task.state
            if task.state == FollowUpState.contact_suppressed
            else FollowUpState.scheduled,
            anchor_kind=event.anchor_kind,
            anchor_time=event.anchor_time,
            anchor_source_ref=event.anchor_source_ref,
            prompt_at=prompt,
            due_at=due,
            review_at=due,
            deadline_handled=False,
        )
    elif isinstance(event, ev.PromptScheduled):
        if (
            task.prompt_at is None
            or now < task.prompt_at
            or (event.expires_at and now >= event.expires_at)
        ):
            return _reject(task, event, "prompt_not_due", "Prompt is outside its send window.")
        effects.append(
            _intent(
                event,
                task.version + 1,
                "routine_prompt",
                audience="patient",
                slot=event.slot_id,
                template_id="patient_day3_prompt",
                contact_kind="scheduled",
                expires_at=event.expires_at,
            )
        )
    elif isinstance(event, ev.PromptAccepted):
        if task.prompt_at is None or now < task.prompt_at:
            return _reject(
                task, event, "prompt_not_due", "The confirmed prompt time has not arrived."
            )
        changes["state"] = FollowUpState.waiting_response
    elif isinstance(event, ev.ResponseReceived):
        if not event.predicate_result.satisfied:
            return _reject(
                task, event, "predicate_not_satisfied", "The response predicate is not satisfied."
            )
        if task.due_at is None:
            return _reject(
                task,
                event,
                "anchor_required",
                "An unknown response window requires an explicit anchor or disposition.",
            )
        if event.objective_received_at > now or event.predicate_result.evaluated_at > now:
            return _reject(
                task, event, "future_evidence", "Receipt and evaluation must not be in the future."
            )
        changes.update(
            state=FollowUpState.fulfilled,
            source_report_ids=(*task.source_report_ids, *event.source_report_ids),
            timeliness=Timeliness.on_time
            if event.objective_received_at <= task.due_at
            else Timeliness.late,
        )
        effects.append(
            _intent(event, task.version + 1, "DANGER" if event.danger_flag else "DONE:FULFILLMENT")
        )
    elif isinstance(event, ev.FollowUpDeadline):
        if task.deadline_handled:
            return _noop(task)
        if task.state == FollowUpState.overdue:
            return _illegal(task, event)
        deadline = task.due_at if task.due_at is not None else task.review_at
        if now < deadline:
            return _reject(
                task, event, "deadline_not_reached", "The follow-up deadline has not arrived."
            )
        changes.update(state=FollowUpState.overdue, deadline_handled=True)
        effects.extend(
            [
                _review(
                    task,
                    event,
                    ReviewKind.followup_disposition,
                    now + policy.overdue_review_interval,
                ),
                _intent(event, task.version + 1, "DEADLINE"),
            ]
        )
    elif isinstance(event, ev.SuppressFollowUpContact):
        changes.update(state=FollowUpState.contact_suppressed, suppression_reason=event.reason)
        effects.extend(
            [
                ev.SuppressRoutineIntents(reason=event.reason),
                _review(
                    task,
                    event,
                    ReviewKind.followup_disposition,
                    now + policy.result_review_interval,
                ),
            ]
        )
    elif isinstance(event, ev.CancelFollowUp):
        changes["state"] = FollowUpState.cancelled
        effects.append(ev.SuppressRoutineIntents(reason=event.reason))
    else:
        return _illegal(task, event)
    data = task.model_dump() | changes
    data.update(version=task.version + 1, updated_at=now)
    terminal = data["state"] in {FollowUpState.fulfilled, FollowUpState.cancelled}
    if terminal:
        data["work_clock"] = None
    else:
        data.update(
            work_clock=_clock(task, now + policy.overdue_review_interval, "followup"),
            last_work_generation=task.last_work_generation + 1,
        )
    try:
        updated = FollowUpTask.model_validate(data)
        if not terminal:
            at = _followup_next_action(updated, now, policy)
            if at <= now:
                return _reject(
                    task,
                    event,
                    "deadline_requires_handling",
                    "Handle the outstanding follow-up deadline before this transition.",
                )
            data["work_clock"] = _clock(task, at, "followup")
            updated = FollowUpTask.model_validate(data)
    except ValidationError as error:
        return _reject(task, event, "invalid_aggregate", str(error))
    return ev.TransitionResult(
        aggregate=updated,
        effects=(*effects, _audit(event, task.version, updated.version)),
    )


def transition_review(
    obligation: ReviewObligation,
    event: ev.ReviewEvent | AccountabilityWake,
    now: datetime,
    policy: DoctorTimingPolicy,
) -> Outcome:
    if isinstance(event, AccountabilityWake):
        return _accountability_wake(obligation, event, now, policy)
    if type(event) not in LEGAL_REVIEW_TRANSITIONS[obligation.state]:
        return _illegal(obligation, event)
    now = utc_instant(now)
    if now < obligation.updated_at:
        return _reject(
            obligation, event, "stale_time", "Transition time precedes the aggregate revision."
        )
    changes: dict[str, object] = {}
    if isinstance(event, ev.AcknowledgeReview):
        if obligation.state == ReviewState.acknowledged:
            return _noop(obligation)
        changes.update(
            state=ReviewState.acknowledged, acknowledged_by=event.actor_id, acknowledged_at=now
        )
    elif isinstance(event, ev.ResolveReview):
        if event.action not in ALLOWED_REVIEW_ACTIONS[obligation.review_kind]:
            return _reject(
                obligation,
                event,
                "review_action_not_allowed",
                f"{event.action} cannot resolve {obligation.review_kind}.",
            )
        if event.expected_source_version != obligation.source_version:
            return _reject(
                obligation,
                event,
                "source_version_mismatch",
                "Resolution must match the exact source version.",
            )
        changes.update(
            state=ReviewState.resolved,
            resolved_by=event.actor_id,
            resolved_at=now,
            resolved_reason=event.reason,
            resolved_action_event_id=event.event_id,
        )
    elif isinstance(event, ev.BlockCoverage):
        if obligation.coverage_status == CoverageStatus.blocked:
            return _noop(obligation)
        changes["coverage_status"] = CoverageStatus.blocked
    elif isinstance(event, ev.RestoreCoverage):
        if obligation.coverage_status == CoverageStatus.covered:
            return _noop(obligation)
        changes["coverage_status"] = CoverageStatus.covered
    elif isinstance(event, ev.MaterialChange):
        if event.version <= obligation.last_material_change_version:
            return _reject(
                obligation,
                event,
                "stale_material_change",
                "Material change must identify a newer version.",
            )
        changes.update(
            last_material_change_version=event.version,
            review_at=now + policy.material_change_review_interval,
        )
    else:
        return _illegal(obligation, event)
    data = obligation.model_dump() | changes
    data.update(version=obligation.version + 1, updated_at=now)
    if data["state"] == ReviewState.resolved:
        data["work_clock"] = None
    else:
        review_at = data["review_at"]
        assert isinstance(review_at, datetime)
        at = review_at if review_at > now else now + policy.overdue_review_interval
        data.update(
            work_clock=_clock(obligation, at, "review"),
            last_work_generation=obligation.last_work_generation + 1,
        )
    updated = ReviewObligation.model_validate(data)
    return ev.TransitionResult(
        aggregate=updated, effects=(_audit(event, obligation.version, updated.version),)
    )


def _accountability_wake(
    aggregate: ev.Aggregate, event: AccountabilityWake, now: datetime, policy: DoctorTimingPolicy
) -> Outcome:
    """Re-arm owed review without claiming a reply, prompt, resolution or material change.

    An expired proposal remains proposed; an expired pause stays blocked. Their
    original clinical dates survive, with a future administrative checkpoint.
    """
    now = utc_instant(now)
    clock = aggregate.work_clock
    if clock is None or clock.next_action_at > now:
        return _noop(aggregate)
    unhandled = (
        isinstance(aggregate, Mission)
        and aggregate.state != MissionState.proposed
        and aggregate.handled_deadline_generation < aggregate.deadline_generation
        and aggregate.escalation_at <= now
    ) or (
        isinstance(aggregate, FollowUpTask)
        and aggregate.state not in {FollowUpState.contact_suppressed, FollowUpState.overdue}
        and not aggregate.deadline_handled
        and (aggregate.due_at or aggregate.review_at) <= now
    )
    if now < aggregate.updated_at or unhandled:
        return ev.TransitionRejected(
            reason_code="deadline_requires_handling" if unhandled else "stale_time",
            message="Handle the outstanding deadline first." if unhandled else "Stale time.",
            state=aggregate.state,
            event_type=event.event_type,
        )
    interval = (
        policy.draft_review_interval
        if isinstance(aggregate, Mission) and aggregate.state == MissionState.proposed
        else policy.overdue_review_interval
    )
    candidates = [aggregate.review_at if aggregate.review_at > now else now + interval]
    changes: dict[str, object] = {}
    if isinstance(aggregate, Mission):
        changes["review_at"] = candidates[0]
        candidates += [
            at
            for at in (aggregate.next_contact_at, aggregate.resume_at)
            if at is not None and at > now
        ]
        if aggregate.handled_deadline_generation < aggregate.deadline_generation:
            if aggregate.escalation_at > now:
                candidates.append(aggregate.escalation_at)
    elif isinstance(aggregate, FollowUpTask):
        candidates = [_followup_next_action(aggregate, now, policy)]
    updated = type(aggregate).model_validate(
        aggregate.model_dump()
        | changes
        | {
            "version": aggregate.version + 1,
            "updated_at": now,
            "last_work_generation": aggregate.last_work_generation + 1,
            "work_clock": _clock(aggregate, min(candidates), clock.work_lane),
        }
    )
    return ev.TransitionResult(
        aggregate=updated,
        effects=(
            ev.RecordAudit(
                event_type=event.event_type,
                event_id=event.event_id,
                before_version=aggregate.version,
                after_version=updated.version,
            ),
        ),
    )


def _creation_rejected(event_type: str, message: str) -> ev.TransitionRejected:
    return ev.TransitionRejected(
        reason_code="timing_needs_clarification",
        message=message,
        state="creation",
        event_type=event_type,
    )


def create_proposed_mission(event: ev.ProposalCreated) -> Outcome:
    """The caller resolves draft timing with state_hint='proposed' before this factory."""
    at = min(event.timing.review_at, event.timing.escalation_at)
    if at <= event.created_at:
        return _creation_rejected(
            event.event_type, "A proposed mission requires future draft work."
        )
    mission = Mission(
        id=event.mission_id,
        doctor_id=event.doctor_id,
        patient_id=event.patient_id,
        created_at=event.created_at,
        updated_at=event.created_at,
        kind=event.kind,
        title=event.title,
        details=event.details,
        objective_predicate=event.objective_predicate,
        order_refs=event.order_refs,
        source_proposal_id=event.source_proposal_id,
        state=MissionState.proposed,
        last_work_generation=1,
        work_clock=WorkClock(next_action_at=at, work_lane="mission"),
        **event.timing.model_dump(),
    )
    return ev.TransitionResult(aggregate=mission, effects=(_audit(event, 0, mission.version),))


def create_support_ticket(
    event: ev.CreateSupportTicket,
    now: datetime,
    policy: DoctorTimingPolicy,
) -> Outcome:
    now = utc_instant(now)
    timing = resolve_timing(MissionKind.QUESTION, now, policy, grace_override=0)
    if isinstance(timing, NeedsClarification):
        return _creation_rejected(event.event_type, timing.message)
    data = timing.model_dump() | {"timing_anchor": TimingAnchor(kind="ticket_created", instant=now)}
    mission = Mission(
        id=event.mission_id,
        doctor_id=event.doctor_id,
        patient_id=event.patient_id,
        created_at=now,
        updated_at=now,
        kind=MissionKind.QUESTION,
        title=event.title,
        details=QuestionDetails(
            question_text=event.question_text, source_observation_ref=event.source_observation_ref
        ),
        objective_predicate=DoctorAnswerPredicate(),
        state=MissionState.open,
        confirmed_at=now,
        confirmed_by="system:support_ticket",
        last_work_generation=1,
        work_clock=WorkClock(next_action_at=timing.review_at, work_lane="mission"),
        **data,
    )
    review = ev.CreateReview(
        event_id=event.event_id,
        review_kind=ReviewKind.question_answer,
        source_type="mission",
        source_id=mission.id,
        source_version=mission.version,
        review_at=mission.due_at,
        owner_doctor_id=mission.doctor_id,
        patient_id=mission.patient_id,
        source_mission_id=mission.id,
    )
    return ev.TransitionResult(
        aggregate=mission, effects=(review, _audit(event, 0, mission.version))
    )


def create_followup(
    payload: ev.CreateFollowUp,
    now: datetime,
    policy: DoctorTimingPolicy,
) -> Outcome:
    now = utc_instant(now)
    prompt: datetime | None
    if payload.kind == FollowUpKind.CLINICAL_CHECKIN:
        if payload.prompt_at is None:
            return _creation_rejected(
                "CONFIRM_FOLLOWUP", "CLINICAL_CHECKIN requires an explicit prompt_at."
            )
        prompt = payload.prompt_at
    else:
        prompt = (
            medication_day3_prompt_at(payload.anchor_time, policy)
            if payload.anchor_time is not None
            else None
        )
        if payload.prompt_at is not None and payload.prompt_at != prompt:
            return _creation_rejected(
                "CONFIRM_FOLLOWUP",
                "MEDICATION_DAY3 prompt must match the confirmed anchor and policy.",
            )
    due = followup_due_at(prompt, policy) if prompt is not None else None
    review_at = due if due is not None else now + policy.draft_review_interval
    if due is not None and due <= now:
        return _creation_rejected(
            "CONFIRM_FOLLOWUP",
            "The confirmed response window is already past; obtain an explicit disposition.",
        )
    key = review_source_key(
        payload.doctor_id,
        payload.kind,
        payload.parent_mission_id,
        1,
        ReviewKind.followup_disposition,
    )
    identity = sha256((key + payload.event_id).encode()).hexdigest()
    at = min(review_at, prompt) if prompt is not None and prompt > now else review_at
    task = FollowUpTask(
        id="followup:" + identity,
        doctor_id=payload.doctor_id,
        patient_id=payload.patient_id,
        created_at=now,
        updated_at=now,
        kind=payload.kind,
        parent_mission_id=payload.parent_mission_id,
        order_refs=payload.order_refs,
        confirmed_by=payload.confirmed_by,
        confirmed_at=payload.confirmed_at,
        anchor_kind=payload.anchor_kind,
        anchor_time=payload.anchor_time,
        prompt_at=prompt,
        due_at=due,
        review_at=review_at,
        response_predicate=payload.response_predicate,
        state=FollowUpState.awaiting_anchor if prompt is None else FollowUpState.scheduled,
        last_work_generation=1,
        work_clock=WorkClock(next_action_at=at, work_lane="followup"),
    )
    audit = ev.RecordAudit(
        event_type="CONFIRM_FOLLOWUP", event_id=payload.event_id, before_version=0, after_version=1
    )
    return ev.TransitionResult(aggregate=task, effects=(audit,))


def create_review(
    payload: ev.CreateReview,
    now: datetime,
    policy: DoctorTimingPolicy,
) -> ev.TransitionResult:
    """Stable source identity; the store's create-or-get enforces creation replay."""
    now = utc_instant(now)
    key = review_source_key(
        payload.owner_doctor_id,
        payload.source_type,
        payload.source_id,
        payload.source_version,
        payload.review_kind,
    )
    at = payload.review_at if payload.review_at > now else now + policy.overdue_review_interval
    review = ReviewObligation(
        id="review:" + sha256(key.encode()).hexdigest(),
        created_at=now,
        updated_at=now,
        source_type=payload.source_type,
        source_id=payload.source_id,
        source_version=payload.source_version,
        review_kind=payload.review_kind,
        unique_source_key=key,
        owner_doctor_id=payload.owner_doctor_id,
        patient_id=payload.patient_id,
        source_mission_id=payload.source_mission_id,
        review_at=payload.review_at,
        last_material_change_version=payload.source_version,
        last_work_generation=1,
        work_clock=WorkClock(next_action_at=at, work_lane="review"),
    )
    return ev.TransitionResult(aggregate=review, effects=(_audit(payload, 0, review.version),))
