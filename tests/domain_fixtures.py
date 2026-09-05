"""Synthetic, explicitly constructed aggregates for independent behavioral tests."""

from datetime import UTC, datetime, timedelta

from sanad.domain import (
    DRAFT_POLICY_2026_09,
    AcceptedFactRef,
    CreateFollowUp,
    CreateReview,
    EvidencePredicate,
    FollowUpKind,
    FollowUpState,
    FollowUpTask,
    FulfillmentValidity,
    Mission,
    MissionKind,
    MissionState,
    PatientReportPredicate,
    PredicateResult,
    ResolvedTiming,
    ReviewKind,
    ReviewObligation,
    ReviewState,
    TestDetails,
    TransitionResult,
    VersionRef,
    WorkClock,
    create_followup,
    create_review,
    resolve_timing,
)

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
POLICY = DRAFT_POLICY_2026_09
ORDER = VersionRef(entity_type="care_order", id="synthetic-order", version=1)
EVIDENCE = AcceptedFactRef(fact_kind="evidence", fact_id="synthetic-evidence", version=1)
CORRECTION = AcceptedFactRef(fact_kind="evidence", fact_id="synthetic-evidence", version=2)


def predicate_result(satisfied: bool = True, at: datetime = NOW) -> PredicateResult:
    return PredicateResult(
        satisfied=satisfied,
        missing=() if satisfied else ("synthetic missing analyte",),
        detail="Synthetic caller-evaluated predicate",
        evaluated_at=at,
    )


def mission(state: MissionState = MissionState.open, **changes: object) -> Mission:
    timing = resolve_timing(MissionKind.TEST, NOW, POLICY)
    assert isinstance(timing, ResolvedTiming)
    data: dict[str, object] = {
        "id": "synthetic-mission",
        "doctor_id": "synthetic-doctor",
        "patient_id": "synthetic-patient",
        "created_at": NOW - timedelta(days=30),
        "updated_at": NOW - timedelta(hours=1),
        "kind": MissionKind.TEST,
        "title": "Synthetic TEST",
        "details": TestDetails(analytes=("synthetic-analyte",), completeness="all"),
        "objective_predicate": EvidencePredicate(evaluator="test"),
        "order_refs": (ORDER,),
        "state": state,
        "confirmed_at": NOW - timedelta(days=1),
        "confirmed_by": "synthetic-doctor",
        "last_work_generation": 1,
        "work_clock": WorkClock(next_action_at=timing.review_at, work_lane="mission"),
        **timing.model_dump(),
    }
    if state == MissionState.proposed:
        data.update(confirmed_at=None, confirmed_by=None)
    if state == MissionState.blocked:
        data.update(
            resume_at=NOW + timedelta(days=1),
            barrier_reason="Synthetic barrier",
            barrier_type="access",
        )
    if state == MissionState.overdue:
        data.update(
            due_at=NOW - timedelta(days=1),
            escalation_at=NOW - timedelta(days=1),
            review_at=NOW + POLICY.overdue_review_interval,
            handled_deadline_generation=1,
        )
    if state in {
        MissionState.fulfilled,
        MissionState.cancelled,
        MissionState.closed_unfulfilled,
        MissionState.superseded,
    }:
        data["work_clock"] = None
    if state == MissionState.fulfilled:
        data.update(
            fulfilled_at=NOW - timedelta(hours=1),
            fulfillment_event_id="synthetic-prior-fulfillment",
            objective_received_at=NOW - timedelta(hours=2),
            timeliness="on_time",
            fulfillment_validity=FulfillmentValidity.valid,
            evidence_refs=(EVIDENCE,),
        )
    return Mission.model_validate(data | changes)


def followup_payload(**changes: object) -> CreateFollowUp:
    return CreateFollowUp.model_validate(
        {
            "event_id": "synthetic-create-followup",
            "kind": FollowUpKind.MEDICATION_DAY3,
            "parent_mission_id": "synthetic-mission",
            "order_refs": (ORDER,),
            "anchor_kind": "reported_effective_start",
            "anchor_time": NOW - POLICY.medication_day3_offset,
            "response_predicate": PatientReportPredicate(report_kind="medication_day3"),
            "confirmed_by": "synthetic-doctor",
            "confirmed_at": NOW - timedelta(days=4),
            "doctor_id": "synthetic-doctor",
            "patient_id": "synthetic-patient",
            **changes,
        }
    )


def followup(state: FollowUpState = FollowUpState.scheduled, **changes: object) -> FollowUpTask:
    result = create_followup(followup_payload(), NOW - timedelta(hours=1), POLICY)
    assert isinstance(result, TransitionResult)
    assert isinstance(result.aggregate, FollowUpTask)
    data = result.aggregate.model_dump() | {"state": state}
    if state == FollowUpState.awaiting_anchor:
        data.update(anchor_time=None, prompt_at=None, due_at=None)
    if state == FollowUpState.overdue:
        data.update(
            due_at=NOW - timedelta(minutes=1),
            prompt_at=NOW - timedelta(days=2),
            deadline_handled=True,
        )
    if state in {FollowUpState.fulfilled, FollowUpState.cancelled}:
        data["work_clock"] = None
    return FollowUpTask.model_validate(data | changes)


def review_payload(**changes: object) -> CreateReview:
    return CreateReview.model_validate(
        {
            "event_id": "synthetic-create-review",
            "review_kind": ReviewKind.result_review,
            "source_type": "mission",
            "source_id": "synthetic-mission",
            "source_version": 2,
            "review_at": NOW + POLICY.result_review_interval,
            "owner_doctor_id": "synthetic-doctor",
            "patient_id": "synthetic-patient",
            "source_mission_id": "synthetic-mission",
            **changes,
        }
    )


def review(state: ReviewState = ReviewState.open, **changes: object) -> ReviewObligation:
    result = create_review(review_payload(), NOW - timedelta(hours=1), POLICY)
    assert isinstance(result.aggregate, ReviewObligation)
    data = result.aggregate.model_dump() | {"state": state}
    if state == ReviewState.acknowledged:
        data.update(acknowledged_by="synthetic-doctor", acknowledged_at=NOW - timedelta(minutes=1))
    if state == ReviewState.resolved:
        data.update(
            work_clock=None,
            resolved_by="synthetic-doctor",
            resolved_at=NOW - timedelta(minutes=1),
            resolved_reason="Reviewed",
            resolved_action_event_id="synthetic-review-resolution",
        )
    return ReviewObligation.model_validate(data | changes)
