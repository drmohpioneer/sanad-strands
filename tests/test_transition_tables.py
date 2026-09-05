"""Handwritten contract matrices. No expected outcome comes from legality constants.

Each column follows the contract's event enumeration. 'unchanged' means an
audited revision preserving execution state; 'noop' means identical object and
version, with no effects. Fixtures satisfy guards unless the row specifies replay.
"""

from datetime import timedelta
from typing import get_args

import pytest
from domain_fixtures import (
    CORRECTION,
    EVIDENCE,
    NOW,
    ORDER,
    POLICY,
    followup,
    followup_payload,
    mission,
    predicate_result,
    review,
    review_payload,
)

import sanad.domain.events as ev
from sanad.domain import (
    ExplicitTiming,
    FollowUpState,
    MissionKind,
    MissionState,
    ObservationRef,
    ResolvedTiming,
    ReviewAction,
    ReviewState,
    resolve_timing,
    transition_followup,
    transition_mission,
    transition_review,
)

_TIMING = resolve_timing(MissionKind.TEST, NOW, POLICY, state_hint="proposed")
assert isinstance(_TIMING, ResolvedTiming)
_EXPLICIT = ExplicitTiming(
    instant=NOW + timedelta(days=5),
    original_expression="Synthetic five days",
    timezone="Africa/Cairo",
)
_MISSION = mission()

MISSION_EVENTS: tuple[ev.MissionEvent, ...] = (
    ev.ProposalCreated(
        event_id="proposal",
        mission_id="synthetic-new",
        doctor_id="synthetic-doctor",
        patient_id="synthetic-patient",
        created_at=NOW,
        kind=MissionKind.TEST,
        title="Synthetic test",
        details=_MISSION.details,
        objective_predicate=_MISSION.objective_predicate,
        timing=_TIMING,
    ),
    ev.ConfirmMission(event_id="confirm", consent_active=True, actor_id="synthetic-doctor"),
    ev.CreateSupportTicket(
        event_id="ticket",
        mission_id="synthetic-question",
        doctor_id="synthetic-doctor",
        patient_id="synthetic-patient",
        title="Synthetic question",
        question_text="Synthetic question?",
        source_observation_ref=ObservationRef(observation_id="synthetic-observation"),
    ),
    ev.PatientBound(event_id="bound", consent_active=True),
    ev.ContactAccepted(event_id="contact"),
    ev.PatientReplied(event_id="reply"),
    ev.BarrierRecorded(
        event_id="barrier",
        barrier_type="access",
        reason="Synthetic barrier",
        resume_at=NOW + timedelta(days=1),
    ),
    ev.BarrierResolved(event_id="barrier-resolved"),
    ev.ContactExhausted(event_id="exhausted", exhausted=True),
    ev.PauseContact(event_id="pause", reason="Synthetic pause", resume_at=NOW + timedelta(days=1)),
    ev.ResumeContact(event_id="resume", consent_active=True, order_active=True, doctor_active=True),
    ev.DeadlineReached(event_id="deadline"),
    ev.ObjectiveFulfilled(
        event_id="fulfilled",
        predicate_result=predicate_result(),
        objective_received_at=NOW,
        evidence_refs=(EVIDENCE,),
        fulfillment_event_id="synthetic-fulfillment",
        danger_flag=False,
        actor_kind="doctor",
    ),
    ev.DoctorExtend(
        event_id="extend",
        actor_id="synthetic-doctor",
        timing=_EXPLICIT,
        reason="Synthetic extension",
        consent_active=True,
    ),
    ev.DoctorCancel(
        event_id="cancel", actor_id="synthetic-doctor", reason="Synthetic cancellation"
    ),
    ev.DoctorCloseUnfulfilled(
        event_id="close",
        actor_id="synthetic-doctor",
        reason="Synthetic disposition",
        open_incident=False,
    ),
    ev.DoctorReopen(
        event_id="reopen",
        actor_id="synthetic-doctor",
        timing=_EXPLICIT,
        new_objective_predicate=_MISSION.objective_predicate,
        new_order_refs=(ORDER,),
        current_active_order_refs=(ORDER,),
        reason="Synthetic reopen",
        consent_active=True,
    ),
    ev.OrderSuperseded(event_id="superseded", successor_order_ref=None),
    ev.CorrectAcceptedEvidence(
        event_id="correction",
        superseded_evidence_ref=EVIDENCE,
        correcting_evidence_ref=CORRECTION,
        predicate_still_holds=False,
    ),
    ev.ValidateCorrection(event_id="validate", predicate_result=predicate_result()),
    ev.LateInputRecorded(
        event_id="late-input",
        observation_ref=ObservationRef(observation_id="synthetic-late-observation"),
    ),
    ev.EvidenceAssociated(event_id="associate", evidence_ref=EVIDENCE),
    ev.ReviewAcknowledged(
        event_id="review-ack", obligation_id="synthetic-review", actor_id="synthetic-doctor"
    ),
    ev.ReviewResolved(
        event_id="review-resolve",
        obligation_id="synthetic-review",
        action=ReviewAction.review,
        expected_source_version=2,
        actor_id="synthetic-doctor",
        reason="Synthetic review",
    ),
    ev.SafetyIncidentRaised(event_id="incident"),
    ev.ContactPreferenceChanged(event_id="preference", reason="Synthetic preference"),
)

# 26 literal cells per row, transcribed from the domain table and A02–A03/A04.
MISSION_TABLE = {
    "proposed": (
        "reject",
        "open",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "cancelled",
        "reject",
        "reject",
        "superseded",
        "unchanged",
        "reject",
        "unchanged",
        "reject",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
    ),
    "awaiting_link": (
        "reject",
        "reject",
        "reject",
        "open",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "awaiting_link",
        "reject",
        "overdue",
        "fulfilled",
        "open",
        "cancelled",
        "closed_unfulfilled",
        "reject",
        "superseded",
        "unchanged",
        "reject",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
    ),
    "open": (
        "reject",
        "reject",
        "reject",
        "noop",
        "waiting_patient",
        "open",
        "blocked",
        "reject",
        "unreachable",
        "blocked",
        "reject",
        "overdue",
        "fulfilled",
        "open",
        "cancelled",
        "closed_unfulfilled",
        "reject",
        "superseded",
        "unchanged",
        "reject",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
    ),
    "waiting_patient": (
        "reject",
        "reject",
        "reject",
        "noop",
        "waiting_patient",
        "open",
        "blocked",
        "reject",
        "unreachable",
        "blocked",
        "reject",
        "overdue",
        "fulfilled",
        "open",
        "cancelled",
        "closed_unfulfilled",
        "reject",
        "superseded",
        "unchanged",
        "reject",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
    ),
    "blocked": (
        "reject",
        "reject",
        "reject",
        "noop",
        "reject",
        "open",
        "blocked",
        "open",
        "unreachable",
        "blocked",
        "open",
        "overdue",
        "fulfilled",
        "open",
        "cancelled",
        "closed_unfulfilled",
        "reject",
        "superseded",
        "unchanged",
        "reject",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
    ),
    "unreachable": (
        "reject",
        "reject",
        "reject",
        "noop",
        "waiting_patient",
        "open",
        "blocked",
        "reject",
        "unreachable",
        "blocked",
        "reject",
        "overdue",
        "fulfilled",
        "open",
        "cancelled",
        "closed_unfulfilled",
        "reject",
        "superseded",
        "unchanged",
        "reject",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
    ),
    "overdue": (
        "reject",
        "reject",
        "reject",
        "noop",
        "overdue",
        "overdue",
        "overdue",
        "overdue",
        "reject",
        "overdue",
        "overdue",
        "noop",
        "fulfilled",
        "open",
        "cancelled",
        "closed_unfulfilled",
        "reject",
        "superseded",
        "unchanged",
        "reject",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
    ),
    "fulfilled": (
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "open",
        "reject",
        "unchanged",
        "noop",
        "unchanged",
        "reject",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
    ),
    "cancelled": (
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "open",
        "reject",
        "unchanged",
        "reject",
        "unchanged",
        "reject",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
    ),
    "closed_unfulfilled": (
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "open",
        "reject",
        "unchanged",
        "reject",
        "unchanged",
        "reject",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
    ),
    "superseded": (
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "unchanged",
        "reject",
        "unchanged",
        "reject",
        "unchanged",
        "unchanged",
        "unchanged",
        "unchanged",
    ),
}

FOLLOWUP_EVENTS: tuple[ev.FollowUpEvent, ...] = (
    ev.ConfirmFollowUp.model_validate(followup_payload().model_dump()),
    ev.AnchorConfirmed(
        event_id="anchor",
        anchor_kind="reported_effective_start",
        anchor_time=NOW,
        replace_existing=True,
    ),
    ev.PromptAccepted(event_id="prompt"),
    ev.ResponseReceived(
        event_id="response",
        predicate_result=predicate_result(),
        objective_received_at=NOW,
        danger_flag=False,
    ),
    ev.FollowUpDeadline(event_id="followup-deadline"),
    ev.SuppressFollowUpContact(event_id="suppress", reason="Synthetic opt-out"),
    ev.CancelFollowUp(event_id="followup-cancel", reason="Synthetic cancellation"),
)
FOLLOWUP_TABLE = {
    "awaiting_anchor": (
        "reject",
        "scheduled",
        "reject",
        "reject",
        "overdue",
        "contact_suppressed",
        "cancelled",
    ),
    "scheduled": (
        "reject",
        "scheduled",
        "waiting_response",
        "fulfilled",
        "overdue",
        "contact_suppressed",
        "cancelled",
    ),
    "waiting_response": (
        "reject",
        "reject",
        "reject",
        "fulfilled",
        "overdue",
        "contact_suppressed",
        "cancelled",
    ),
    "fulfilled": ("reject", "reject", "reject", "reject", "reject", "reject", "reject"),
    "overdue": (
        "reject",
        "reject",
        "reject",
        "fulfilled",
        "noop",
        "contact_suppressed",
        "cancelled",
    ),
    "contact_suppressed": (
        "reject",
        "reject",
        "reject",
        "fulfilled",
        "reject",
        "reject",
        "cancelled",
    ),
    "cancelled": ("reject", "reject", "reject", "reject", "reject", "reject", "reject"),
}
REVIEW_EVENTS: tuple[ev.ReviewEvent, ...] = (
    review_payload(),
    ev.AcknowledgeReview(event_id="acknowledge", actor_id="synthetic-doctor"),
    ev.ResolveReview(
        event_id="resolve",
        action=ReviewAction.review,
        expected_source_version=2,
        actor_id="synthetic-doctor",
        reason="Synthetic review",
    ),
    ev.BlockCoverage(event_id="block"),
    ev.RestoreCoverage(event_id="restore"),
    ev.MaterialChange(event_id="material", version=3),
)
REVIEW_TABLE = {
    "open": ("reject", "acknowledged", "resolved", "unchanged", "noop", "unchanged"),
    "acknowledged": ("reject", "noop", "resolved", "unchanged", "noop", "unchanged"),
    "resolved": ("reject", "reject", "reject", "reject", "reject", "reject"),
}


def test_handwritten_tables_cover_every_state_and_event_once() -> None:
    assert set(MISSION_TABLE) == {state.value for state in MissionState}
    assert set(FOLLOWUP_TABLE) == {state.value for state in FollowUpState}
    assert set(REVIEW_TABLE) == {state.value for state in ReviewState}
    assert len(MISSION_EVENTS) == len({type(e) for e in MISSION_EVENTS}) == 26
    assert len(FOLLOWUP_EVENTS) == len({type(e) for e in FOLLOWUP_EVENTS}) == 7
    assert len(REVIEW_EVENTS) == len({type(e) for e in REVIEW_EVENTS}) == 6
    assert {type(e) for e in MISSION_EVENTS} == set(
        get_args(get_args(ev.MissionEvent.__value__)[0])
    )
    assert {type(e) for e in FOLLOWUP_EVENTS} == set(
        get_args(get_args(ev.FollowUpEvent.__value__)[0])
    )
    assert {type(e) for e in REVIEW_EVENTS} == set(get_args(get_args(ev.ReviewEvent.__value__)[0]))
    assert all(len(row) == 26 for row in MISSION_TABLE.values())
    assert all(len(row) == 7 for row in FOLLOWUP_TABLE.values())
    assert all(len(row) == 6 for row in REVIEW_TABLE.values())
    assert sum(map(len, MISSION_TABLE.values())) == 286
    assert sum(map(len, FOLLOWUP_TABLE.values())) == 49
    assert sum(map(len, REVIEW_TABLE.values())) == 18


def assert_outcome(
    original: ev.Aggregate,
    event_type: str,
    result: ev.TransitionResult | ev.TransitionRejected,
    expected: str,
) -> None:
    if expected == "reject":
        assert isinstance(result, ev.TransitionRejected), result
        assert result.reason_code == "illegal_transition"
        assert result.state == original.state
        assert result.event_type == event_type
        assert original.state in result.message and event_type in result.message
        assert not result.effects
        return
    assert isinstance(result, ev.TransitionResult), result
    assert ev.TransitionResult.model_validate_json(result.model_dump_json()) == result
    if expected == "noop":
        assert result.noop and result.aggregate is original and result.effects == ()
    else:
        assert not result.noop
        assert result.aggregate.state == (original.state if expected == "unchanged" else expected)
        assert result.aggregate.version == original.version + 1
        audit = result.effects[-1]
        assert isinstance(audit, ev.RecordAudit)
        assert audit == ev.RecordAudit(
            event_type=event_type,
            event_id=audit.event_id,
            before_version=original.version,
            after_version=original.version + 1,
        )


@pytest.mark.parametrize(
    ("state", "column", "expected"),
    [
        (state, column, expected)
        for state, row in MISSION_TABLE.items()
        for column, expected in enumerate(row)
    ],
)
def test_mission_table(state: str, column: int, expected: str) -> None:
    original = mission(MissionState(state))
    event = MISSION_EVENTS[column]
    at = max(NOW, original.escalation_at) if isinstance(event, ev.DeadlineReached) else NOW
    result = transition_mission(original, event, at, POLICY)
    assert_outcome(original, event.event_type, result, expected)
    if isinstance(result, ev.TransitionResult) and not result.noop and result.aggregate.work_clock:
        assert result.aggregate.work_clock.next_action_at > at
        assert result.aggregate.last_work_generation == original.last_work_generation + 1


@pytest.mark.parametrize(
    ("state", "column", "expected"),
    [
        (state, column, expected)
        for state, row in FOLLOWUP_TABLE.items()
        for column, expected in enumerate(row)
    ],
)
def test_followup_table(state: str, column: int, expected: str) -> None:
    original = followup(FollowUpState(state))
    event = FOLLOWUP_EVENTS[column]
    at = (
        max(NOW, original.due_at or original.review_at)
        if isinstance(event, ev.FollowUpDeadline)
        else NOW
    )
    result = transition_followup(original, event, at, POLICY)
    assert_outcome(original, event.event_type, result, expected)
    if isinstance(result, ev.TransitionResult) and not result.noop and result.aggregate.work_clock:
        assert result.aggregate.work_clock.next_action_at > at


@pytest.mark.parametrize(
    ("state", "column", "expected"),
    [
        (state, column, expected)
        for state, row in REVIEW_TABLE.items()
        for column, expected in enumerate(row)
    ],
)
def test_review_table(state: str, column: int, expected: str) -> None:
    original = review(ReviewState(state))
    event = REVIEW_EVENTS[column]
    result = transition_review(original, event, NOW, POLICY)
    assert_outcome(original, event.event_type, result, expected)
