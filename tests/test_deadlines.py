from datetime import UTC, datetime, timedelta
from zoneinfo import available_timezones

import pytest
from domain_fixtures import NOW, POLICY, mission
from pydantic import ValidationError

import sanad.domain.boundaries as boundaries
from sanad.domain import (
    AmbiguousLocalTime,
    DefaultDeadline,
    DoctorTimingPolicy,
    DueSource,
    ExplicitTiming,
    MissionKind,
    MissionState,
    NeedsClarification,
    ObservationRef,
    ResolvedTiming,
    TimingProposal,
    followup_due_at,
    format_local,
    local_to_utc,
    medication_day3_prompt_at,
    next_action_at,
    resolve_timing,
)


def proposal(offset: timedelta = timedelta(days=6), **changes: object) -> TimingProposal:
    return TimingProposal.model_validate(
        {
            "proposed_due_at": NOW + offset,
            "source": "scribe",
            "reason": "Synthetic timing reason",
            "timezone": "Africa/Cairo",
            "anchor_time": NOW,
            "anchor_kind": "observation_received",
            "policy_version": "synthetic-proposal-policy",
            "source_observation_ref": ObservationRef(observation_id="synthetic-observation"),
            **changes,
        }
    )


def explicit(at: datetime) -> ExplicitTiming:
    return ExplicitTiming(
        instant=at, original_expression="Synthetic explicit instruction", timezone="Africa/Cairo"
    )


@pytest.mark.parametrize(
    ("kind", "offset"),
    [
        (MissionKind.TEST, timedelta(days=14)),
        (MissionKind.MONITOR, timedelta(days=11)),
        (MissionKind.MEDICATION, timedelta(days=3)),
        (MissionKind.SEND_RECORDS, timedelta(days=3)),
        (MissionKind.VISIT, timedelta(days=30)),
        (MissionKind.QUESTION, timedelta(hours=48)),
        (MissionKind.TASK, timedelta(days=7)),
    ],
)
def test_every_default_and_zero_grace(kind: MissionKind, offset: timedelta) -> None:
    result = resolve_timing(kind, NOW, POLICY, schedule_end=NOW + timedelta(days=10))
    assert isinstance(result, ResolvedTiming)
    assert result.due_at == NOW + offset - timedelta(hours=5)
    assert result.due_source == DueSource.default
    assert result.grace_seconds == 0
    assert result.escalation_at == result.review_at == result.due_at
    assert POLICY.policy_version in result.due_reason
    assert result.policy_version == POLICY.policy_version
    assert result.timezone == "Africa/Cairo"
    assert result.timing_anchor.kind == (
        "schedule_end" if kind == MissionKind.MONITOR else "confirmation"
    )


@pytest.mark.parametrize("kind", list(MissionKind))
def test_explicit_and_valid_proposal_take_precedence_for_every_kind(kind: MissionKind) -> None:
    instant = NOW + timedelta(hours=4, seconds=17, microseconds=321)
    result = resolve_timing(kind, NOW, POLICY, explicit=explicit(instant), proposal=proposal())
    assert isinstance(result, ResolvedTiming)
    assert result.due_at == instant
    assert result.due_source == DueSource.doctor
    assert result.original_time_expression == "Synthetic explicit instruction"
    assert result.grace_seconds == 0
    assert result.due_at.tzinfo is UTC
    inferred = resolve_timing(kind, NOW, POLICY, proposal=proposal())
    assert isinstance(inferred, ResolvedTiming)
    assert inferred.due_at == NOW + timedelta(days=6)
    assert inferred.due_source == DueSource.scribe
    assert inferred.due_reason == "Synthetic timing reason"
    assert inferred.timing_anchor.kind == "observation_received"


def test_four_hours_across_cairo_dst_keeps_exact_instant_and_local_offset() -> None:
    anchor = datetime(2026, 10, 29, 20, 30, 17, tzinfo=UTC)
    assert format_local(anchor, "Africa/Cairo") == "2026-10-29T23:30:17+03:00"
    due = anchor + timedelta(hours=4)
    result = resolve_timing(MissionKind.TEST, anchor, POLICY, explicit=explicit(due))
    assert isinstance(result, ResolvedTiming)
    assert result.due_at == datetime(2026, 10, 30, 0, 30, 17, tzinfo=UTC)
    assert result.due_at - anchor == timedelta(hours=4)
    assert format_local(result.due_at, "Africa/Cairo") == "2026-10-30T02:30:17+02:00"
    assert ResolvedTiming.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("local", [datetime(2026, 4, 24, 0, 30), datetime(2026, 10, 29, 23, 30)])
def test_cairo_nonexistent_and_repeated_local_times_require_clarification(local: datetime) -> None:
    for fold in (0, 1):
        with pytest.raises(AmbiguousLocalTime):
            local_to_utc(local.replace(fold=fold), "Africa/Cairo")


def test_normal_local_time_and_invalid_local_helper_input() -> None:
    assert local_to_utc(datetime(2026, 9, 6, 15), "Africa/Cairo") == NOW
    with pytest.raises(ValueError, match="naive"):
        local_to_utc(NOW, "Africa/Cairo")
    with pytest.raises(ValidationError):
        format_local(NOW, "Invalid/Zone")


@pytest.mark.parametrize(
    "offset", [timedelta(days=-1), timedelta(days=400), timedelta(days=365, seconds=1)]
)
def test_invalid_explicit_time_is_never_replaced_by_proposal_or_default(offset: timedelta) -> None:
    result = resolve_timing(
        MissionKind.TEST, NOW, POLICY, explicit=explicit(NOW + offset), proposal=proposal()
    )
    assert isinstance(result, NeedsClarification)
    assert result.reason_code == "explicit_out_of_bounds"


@pytest.mark.parametrize("offset", [timedelta(), timedelta(days=365)])
def test_explicit_resolver_boundaries_are_inclusive(offset: timedelta) -> None:
    result = resolve_timing(MissionKind.TEST, NOW, POLICY, explicit=explicit(NOW + offset))
    assert isinstance(result, ResolvedTiming)
    assert result.due_at == NOW + offset


@pytest.mark.parametrize("days", [1, 180])
def test_inferred_boundary_instants_are_accepted(days: int) -> None:
    result = resolve_timing(MissionKind.TEST, NOW, POLICY, proposal=proposal(timedelta(days=days)))
    assert isinstance(result, ResolvedTiming)
    assert result.due_source == DueSource.scribe


@pytest.mark.parametrize(
    "offset", [timedelta(hours=23, minutes=59), timedelta(days=180, seconds=1), timedelta(days=400)]
)
def test_rejected_proposal_falls_back_with_visible_reason(offset: timedelta) -> None:
    result = resolve_timing(MissionKind.TEST, NOW, POLICY, proposal=proposal(offset))
    assert isinstance(result, ResolvedTiming)
    assert result.due_at == datetime(2026, 9, 20, 7, tzinfo=UTC)
    assert result.due_source == DueSource.default
    assert "Rejected proposal" in result.due_reason and "1–180" in result.due_reason
    assert POLICY.policy_version in result.due_reason


def test_monitor_default_requires_a_future_schedule_end_but_proposal_does_not() -> None:
    missing = resolve_timing(MissionKind.MONITOR, NOW, POLICY)
    assert (
        isinstance(missing, NeedsClarification) and missing.reason_code == "schedule_end_required"
    )
    past = resolve_timing(MissionKind.MONITOR, NOW, POLICY, schedule_end=NOW - timedelta(days=1))
    assert isinstance(past, NeedsClarification) and past.reason_code == "default_not_future"
    invalid = resolve_timing(
        MissionKind.MONITOR, NOW, POLICY, proposal=proposal(timedelta(days=400))
    )
    assert isinstance(invalid, NeedsClarification) and "Rejected proposal" in invalid.message


def test_draft_review_uses_minimum_of_escalation_and_draft_interval() -> None:
    draft = resolve_timing(MissionKind.TEST, NOW, POLICY, state_hint="proposed")
    assert isinstance(draft, ResolvedTiming)
    assert draft.review_at == NOW + timedelta(days=2)
    urgent = resolve_timing(
        MissionKind.TEST,
        NOW,
        POLICY,
        state_hint="proposed",
        explicit=explicit(NOW + timedelta(hours=4)),
    )
    assert isinstance(urgent, ResolvedTiming)
    assert urgent.review_at == urgent.escalation_at == NOW + timedelta(hours=4)


def test_custom_policy_drives_defaults_horizon_inference_grace_and_followup_clocks() -> None:
    policy = DoctorTimingPolicy.model_validate(
        POLICY.model_dump()
        | {
            "policy_version": "synthetic-alternate",
            "default_grace_seconds": 23,
            "inferred_min_days": 2,
            "inferred_max_days": 4,
            "explicit_horizon": timedelta(days=10),
            "draft_review_interval": timedelta(hours=3),
            "medication_day3_offset": timedelta(hours=70),
            "followup_response_window": timedelta(hours=19),
            "default_deadlines": {
                **POLICY.default_deadlines,
                MissionKind.TEST: DefaultDeadline(offset=timedelta(hours=39), relative_to="anchor"),
            },
        }
    )
    result = resolve_timing(
        MissionKind.TEST, NOW, policy, proposal=proposal(), state_hint="proposed"
    )
    assert isinstance(result, ResolvedTiming)
    assert result.due_at == datetime(2026, 9, 8, 7, tzinfo=UTC)
    assert result.escalation_at == result.due_at + timedelta(seconds=23)
    assert result.review_at == NOW + timedelta(hours=3)
    overridden = resolve_timing(
        MissionKind.TEST,
        NOW,
        policy,
        explicit=explicit(NOW + timedelta(hours=4)),
        grace_override=11,
    )
    assert isinstance(overridden, ResolvedTiming) and overridden.grace_seconds == 11
    assert isinstance(
        resolve_timing(MissionKind.TEST, NOW, policy, explicit=explicit(NOW + timedelta(days=11))),
        NeedsClarification,
    )
    prompt = medication_day3_prompt_at(NOW, policy)
    assert prompt == NOW + timedelta(hours=70)
    assert followup_due_at(prompt, policy) == prompt + timedelta(hours=19)


@pytest.mark.parametrize("grace", [-1, True, "1"])
def test_grace_override_requires_nonnegative_strict_int(grace: object) -> None:
    with pytest.raises(ValidationError):
        resolve_timing(MissionKind.TEST, NOW, POLICY, grace_override=grace)  # type: ignore[arg-type]


def test_next_action_minimum_preserves_deadline_review_and_resume_work() -> None:
    value = mission(
        next_contact_at=NOW + timedelta(hours=1),
        resume_at=NOW + timedelta(hours=2),
        review_at=NOW + timedelta(hours=3),
    )
    assert next_action_at(value, NOW) == NOW + timedelta(hours=1)
    assert next_action_at(value, NOW, contact_eligible=False) == NOW + timedelta(hours=2)
    without_resume = mission(next_contact_at=NOW, review_at=NOW + timedelta(hours=3))
    assert next_action_at(without_resume, NOW) == NOW + timedelta(hours=3)
    early_deadline = mission(
        due_at=NOW + timedelta(hours=1), escalation_at=NOW + timedelta(hours=1)
    )
    assert next_action_at(early_deadline, NOW, contact_eligible=False) == NOW + timedelta(hours=1)
    handled = mission(MissionState.overdue, next_contact_at=NOW - timedelta(hours=1))
    assert next_action_at(handled, NOW) == handled.review_at
    assert next_action_at(mission(MissionState.fulfilled), NOW) is None
    pending = mission(due_at=NOW - timedelta(hours=1), escalation_at=NOW - timedelta(hours=1))
    assert next_action_at(pending, NOW, contact_eligible=False) == pending.escalation_at


def test_timezone_set_is_cached_without_weakening_invalid_zone_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = available_timezones
    calls = 0

    def measured() -> set[str]:
        nonlocal calls
        calls += 1
        return real()

    boundaries._available_timezones.cache_clear()
    monkeypatch.setattr(boundaries, "available_timezones", measured)
    try:
        for _ in range(3):
            proposal()
        with pytest.raises(ValidationError, match="IANA"):
            proposal(timezone="Not/AZone")
        assert calls == 1
    finally:
        boundaries._available_timezones.cache_clear()
