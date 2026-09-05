from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from domain_fixtures import CORRECTION, EVIDENCE, NOW, POLICY, followup, mission, review
from pydantic import BaseModel, ValidationError
from test_transition_tables import FOLLOWUP_EVENTS, MISSION_EVENTS, REVIEW_EVENTS

import sanad.domain as domain
import sanad.domain.events as ev
from sanad.domain import (
    DoctorTimingPolicy,
    FollowUpState,
    FulfillmentValidity,
    Mission,
    MissionState,
    MonitorDetails,
    ResolvedTiming,
    ReviewObligation,
    ReviewState,
    SendRecordsDetails,
    TimingAnchor,
    VisitDetails,
    WorkClock,
    followup_due_at,
    format_local,
    medication_day3_prompt_at,
    next_action_at,
    resolve_timing,
    transition_followup,
    transition_mission,
    transition_review,
)
from sanad.domain import (
    TestDetails as LabDetails,
)

VALUES: tuple[BaseModel, ...] = (
    *(mission(state) for state in MissionState),
    *(followup(state) for state in FollowUpState),
    *(review(state) for state in ReviewState),
    *MISSION_EVENTS,
    *FOLLOWUP_EVENTS,
    *REVIEW_EVENTS,
    POLICY,
    *POLICY.default_deadlines.values(),
    LabDetails(
        analytes=("synthetic-analyte",),
        collection_window_start=NOW,
        collection_window_end=NOW + timedelta(days=1),
        completeness="all",
    ),
    MonitorDetails(
        metric="synthetic-metric",
        unit="synthetic-unit",
        slots=(NOW, NOW + timedelta(hours=1)),
        required_coverage=1,
    ),
    SendRecordsDetails(
        categories=("synthetic-history",),
        period_start=NOW - timedelta(days=365),
        period_end=NOW,
        required_count=1,
    ),
    VisitDetails(
        objective="attendance_reported", window_start=NOW, window_end=NOW + timedelta(days=1)
    ),
    WorkClock(next_action_at=NOW, work_lane="mission", last_error_code="synthetic-error"),
    ev.AnchorFollowUp(
        parent_mission_id="synthetic-mission",
        anchor_time=NOW,
        anchor_kind="reported_effective_start",
    ),
    ev.SupersedeEvidence(superseded_ref=EVIDENCE, correcting_ref=CORRECTION),
    ev.ResolveReviewEffect(
        review_kind=domain.ReviewKind.unmet_objective,
        source_type="mission",
        source_id="synthetic-mission",
        reason="Synthetic extension",
    ),
    ev.EmitIntent(
        purpose="DONE:CORRECTION",
        source_event_id="synthetic-correction",
        source_version=2,
        facts_ref="synthetic-correction-facts",
    ),
)


@pytest.mark.parametrize("value", VALUES, ids=lambda value: type(value).__name__)
def test_models_are_frozen_reject_unknown_fields_and_round_trip(value: BaseModel) -> None:
    model = type(value)
    assert model.model_validate_json(value.model_dump_json()) == value
    field = next(iter(model.model_fields))
    with pytest.raises(ValidationError, match="frozen"):
        setattr(value, field, getattr(value, field))
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate(value.model_dump() | {"unexpected": True})


def instant_paths(
    value: object, prefix: tuple[str | int, ...] = ()
) -> Iterator[tuple[str | int, ...]]:
    if isinstance(value, datetime):
        yield prefix
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from instant_paths(item, (*prefix, key))
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            yield from instant_paths(item, (*prefix, index))


def replace_path(value: object, path: tuple[str | int, ...], replacement: object) -> object:
    if not path:
        return replacement
    key, *remaining = path
    if isinstance(value, dict):
        return {
            k: replace_path(v, tuple(remaining), replacement) if k == key else v
            for k, v in value.items()
        }
    assert isinstance(value, (tuple, list))
    return tuple(
        replace_path(v, tuple(remaining), replacement) if i == key else v
        for i, v in enumerate(value)
    )


@pytest.mark.parametrize("value", VALUES, ids=lambda value: type(value).__name__)
def test_every_supplied_nested_datetime_rejects_naive_input(value: BaseModel) -> None:
    data = value.model_dump()
    for path in instant_paths(data):
        for naive in (NOW.replace(tzinfo=None), NOW.replace(tzinfo=None).isoformat()):
            with pytest.raises(ValidationError) as error:
                type(value).model_validate(replace_path(data, path, naive))
            assert any(item["type"] == "timezone_aware" for item in error.value.errors()), path


@pytest.mark.parametrize(
    "field",
    [
        "version",
        "last_work_generation",
        "grace_seconds",
        "deadline_generation",
        "handled_deadline_generation",
        "contact_count",
        "unanswered_delivered_count",
        "evidence_request_count",
    ],
)
@pytest.mark.parametrize("invalid", [True, "1", 1.5, -1])
def test_mission_counters_and_versions_require_strict_valid_integers(
    field: str, invalid: object
) -> None:
    with pytest.raises(ValidationError):
        Mission.model_validate(mission().model_dump() | {field: invalid})


@pytest.mark.parametrize(
    "changes",
    [
        {"patient_id": None},
        {"doctor_id": " "},
        {"title": " "},
        {"policy_version": " "},
        {"timezone": "Not/AZone"},
        {"escalation_at": NOW},
        {"state": "fulfilled", "work_clock": None},
        {"state": "cancelled", "fulfillment_validity": "valid", "work_clock": None},
        {"state": "closed_unfulfilled", "fulfillment_validity": "valid", "work_clock": None},
        {"state": "blocked", "resume_at": NOW},
        {"state": "blocked", "barrier_reason": "Synthetic"},
        {"work_clock": None},
        {"confirmed_at": None},
        {"kind": "MONITOR"},
        {"last_work_generation": 2},
        {"handled_deadline_generation": 2},
        {
            "evidence_refs": (
                {"fact_kind": "clinical_fact", "fact_id": "synthetic-fact", "version": 1},
            )
        },
    ],
)
def test_mission_invariants_reject_inconsistent_aggregate(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Mission.model_validate(mission().model_dump() | changes)


def test_terminal_mission_forbids_work_clock_but_preserves_invalidated_history() -> None:
    with pytest.raises(ValidationError, match="terminal"):
        mission(
            MissionState.fulfilled, work_clock=WorkClock(next_action_at=NOW, work_lane="mission")
        )
    for state in (MissionState.cancelled, MissionState.closed_unfulfilled):
        assert (
            mission(
                state, fulfillment_validity=FulfillmentValidity.invalidated_pending_review
            ).work_clock
            is None
        )


def test_details_are_discriminated_and_medication_question_predicates_cannot_be_swapped() -> None:
    value = mission()
    for details in (
        {"kind": "TEST", "payload": {}},
        {"kind": "TEST", "analytes": (), "completeness": "all"},
        {"kind": "invented"},
    ):
        with pytest.raises(ValidationError):
            Mission.model_validate(value.model_dump() | {"details": details})
    with pytest.raises(ValidationError, match="patient_report"):
        mission(
            kind="MEDICATION",
            details={"kind": "MEDICATION", "action": "START", "order_ref": value.order_refs[0]},
        )
    with pytest.raises(ValidationError, match="doctor_answer"):
        mission(
            kind="QUESTION",
            details={
                "kind": "QUESTION",
                "question_text": "Synthetic?",
                "source_observation_ref": {"observation_id": "synthetic-observation"},
            },
        )


def test_invalid_windows_empty_collections_and_nonpositive_requirements() -> None:
    for model, data in (
        (
            LabDetails,
            {
                "analytes": ("synthetic",),
                "completeness": "all",
                "collection_window_start": NOW,
                "collection_window_end": NOW - timedelta(days=1),
            },
        ),
        (
            MonitorDetails,
            {"metric": "synthetic", "unit": "synthetic", "slots": (), "required_coverage": 1},
        ),
        (
            MonitorDetails,
            {
                "metric": "synthetic",
                "unit": "synthetic",
                "slots": (NOW,),
                "required_coverage": True,
            },
        ),
        (SendRecordsDetails, {"categories": (), "required_count": 1}),
        (SendRecordsDetails, {"categories": ("synthetic",), "required_count": 0}),
        (
            SendRecordsDetails,
            {
                "categories": ("synthetic",),
                "required_count": 1,
                "period_start": NOW,
                "period_end": NOW - timedelta(days=1),
            },
        ),
        (
            VisitDetails,
            {"objective": "arrange", "window_start": NOW, "window_end": NOW - timedelta(days=1)},
        ),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(data)


def test_followup_timing_and_clock_requirements_and_review_resolution_metadata() -> None:
    for state in (FollowUpState.scheduled, FollowUpState.waiting_response, FollowUpState.fulfilled):
        with pytest.raises(ValidationError):
            followup(state, prompt_at=None, due_at=None)
    for state in (FollowUpState.scheduled, FollowUpState.contact_suppressed):
        with pytest.raises(ValidationError, match="unfinished"):
            followup(state, work_clock=None)
    with pytest.raises(ValidationError, match="resolution metadata"):
        review(ReviewState.resolved, resolved_by=None)
    with pytest.raises(ValidationError, match="unique_source_key"):
        review(unique_source_key="synthetic-wrong-key")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ReviewObligation.model_validate(review().model_dump() | {"next_action_at": NOW})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ReviewObligation.model_validate(
            review().model_dump() | {"doctor_id": "synthetic-other-doctor"}
        )


def test_policy_validation_rejects_missing_kinds_reversed_bounds_and_nonpositive_intervals() -> (
    None
):
    cases: tuple[dict[str, object], ...] = (
        {"default_deadlines": {}},
        {"inferred_min_days": 200},
        {"default_grace_seconds": True},
        {"draft_review_interval": timedelta()},
        {"overdue_review_interval": timedelta(seconds=-1)},
        {"explicit_horizon": timedelta()},
        {"timezone": "Not/AZone"},
    )
    for changes in cases:
        with pytest.raises(ValidationError):
            DoctorTimingPolicy.model_validate(POLICY.model_dump() | changes)


def test_function_datetime_arguments_validate_timezone_including_optional_schedule() -> None:
    naive = NOW.replace(tzinfo=None)
    calls = (
        lambda: resolve_timing(domain.MissionKind.TEST, naive, POLICY),
        lambda: resolve_timing(domain.MissionKind.MONITOR, NOW, POLICY, schedule_end=naive),
        lambda: next_action_at(mission(), naive),
        lambda: format_local(naive, "Africa/Cairo"),
        lambda: medication_day3_prompt_at(naive, POLICY),
        lambda: followup_due_at(naive, POLICY),
        lambda: transition_mission(
            mission(), ev.PatientReplied(event_id="synthetic-reply"), naive, POLICY
        ),
        lambda: transition_followup(
            followup(), ev.PromptAccepted(event_id="synthetic-prompt"), naive, POLICY
        ),
        lambda: transition_review(
            review(), ev.BlockCoverage(event_id="synthetic-block"), naive, POLICY
        ),
    )
    for call in calls:
        with pytest.raises(ValidationError, match="timezone_aware"):
            call()


def test_public_surface_exports_contract_types_without_competing_aggregate_names() -> None:
    for name in (
        "Mission",
        "FollowUpTask",
        "ReviewObligation",
        "MissionEvent",
        "FollowUpEvent",
        "ReviewEvent",
        "Effect",
        "ObjectivePredicate",
        "PredicateResult",
        "ExplicitTiming",
        "ResolvedTiming",
        "NeedsClarification",
        "create_proposed_mission",
        "create_support_ticket",
        "create_followup",
        "create_review",
        "transition_mission",
        "transition_followup",
        "transition_review",
        "LEGAL_TRANSITIONS",
        "TERMINAL_STATES",
        "DRAFT_POLICY_2026_09",
        "review_source_key",
    ):
        assert name in domain.__all__ and getattr(domain, name) is not None
    assert "next_action_at" not in ReviewObligation.model_fields
    assert "question_due_hours" not in DoctorTimingPolicy.model_fields


def test_optional_instants_normalize_to_utc_and_escalation_invariant_round_trips() -> None:
    from datetime import timezone

    local = NOW.astimezone(timezone(timedelta(hours=3)))
    anchor = TimingAnchor(kind="doctor_reference_time", instant=local)
    assert anchor.instant.tzinfo is NOW.tzinfo and anchor.instant == NOW
    timing = domain.resolve_timing(domain.MissionKind.TEST, NOW, POLICY)
    assert isinstance(timing, ResolvedTiming)
    with pytest.raises(ValidationError, match="escalation_at"):
        ResolvedTiming.model_validate(timing.model_dump() | {"escalation_at": NOW})
