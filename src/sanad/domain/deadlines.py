"""Pure UTC clock calculations, with explicit instants preserved without rounding."""

from datetime import UTC, datetime, time, timedelta
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, model_validator

from sanad.domain.boundaries import (
    IanaZone,
    NonblankStr,
    NonnegativeInt,
    TimingProposal,
    UtcInstant,
    _BoundaryValue,
)
from sanad.domain.entities import (
    TERMINAL_STATES,
    DoctorTimingPolicy,
    DueSource,
    Mission,
    MissionKind,
    TimingAnchor,
)

_INSTANT: TypeAdapter[datetime] = TypeAdapter(UtcInstant)
_ZONE: TypeAdapter[str] = TypeAdapter(IanaZone)
_GRACE: TypeAdapter[int] = TypeAdapter(NonnegativeInt)


def utc_instant(instant: datetime) -> datetime:
    """Validate function arguments just as strictly as model datetime fields."""
    return _INSTANT.validate_python(instant)


class ExplicitTiming(_BoundaryValue):
    instant: UtcInstant
    original_expression: NonblankStr
    timezone: IanaZone


class ResolvedTiming(_BoundaryValue):
    due_at: UtcInstant
    due_source: DueSource
    due_reason: NonblankStr
    timing_anchor: TimingAnchor
    original_time_expression: NonblankStr | None = None
    timezone: IanaZone
    grace_seconds: NonnegativeInt
    escalation_at: UtcInstant
    review_at: UtcInstant
    policy_version: NonblankStr

    @model_validator(mode="after")
    def validate_escalation(self) -> Self:
        if self.escalation_at != self.due_at + timedelta(seconds=self.grace_seconds):
            raise ValueError("escalation_at must equal due_at plus grace_seconds")
        return self


class NeedsClarification(_BoundaryValue):
    reason_code: NonblankStr
    message: NonblankStr


def resolve_timing(
    kind: MissionKind,
    anchor_at: datetime,
    policy: DoctorTimingPolicy,
    *,
    explicit: ExplicitTiming | None = None,
    proposal: TimingProposal | None = None,
    schedule_end: datetime | None = None,
    state_hint: Literal["proposed", "active"] = "active",
    grace_override: int | None = None,
) -> ResolvedTiming | NeedsClarification:
    anchor_at = utc_instant(anchor_at)
    if schedule_end is not None:
        schedule_end = utc_instant(schedule_end)
    if state_hint not in {"proposed", "active"}:
        raise ValueError("state_hint must be proposed or active")
    grace = (
        policy.default_grace_seconds
        if grace_override is None
        else _GRACE.validate_python(grace_override)
    )
    original = None
    zone = policy.timezone
    anchor = TimingAnchor(kind="confirmation", instant=anchor_at)
    rejection = ""
    if explicit is not None:
        if explicit.instant < anchor_at or explicit.instant - anchor_at > policy.explicit_horizon:
            return NeedsClarification(
                reason_code="explicit_out_of_bounds",
                message="Explicit time precedes the anchor or exceeds the policy horizon.",
            )
        due = explicit.instant
        source = DueSource.doctor
        reason = "Explicit doctor time: " + explicit.original_expression
        original = explicit.original_expression
        zone = explicit.timezone
    elif proposal is not None and (
        timedelta(days=policy.inferred_min_days)
        <= proposal.proposed_due_at - anchor_at
        <= timedelta(days=policy.inferred_max_days)
    ):
        due = proposal.proposed_due_at
        source = DueSource(proposal.source)
        reason = proposal.reason
        zone = proposal.timezone
        anchor = TimingAnchor(kind=proposal.anchor_kind, instant=proposal.anchor_time)
    else:
        if proposal is not None:
            rejection = (
                "Rejected proposal: offset outside inferred bounds "
                f"{policy.inferred_min_days}, {policy.inferred_max_days} days. "
            )
        default = policy.default_deadlines[kind]
        if default.relative_to == "schedule_end":
            if schedule_end is None:
                return NeedsClarification(
                    reason_code="schedule_end_required",
                    message=rejection + "The default deadline requires schedule_end.",
                )
            anchor = TimingAnchor(kind="schedule_end", instant=schedule_end)
        due = anchor.instant + default.offset
        local = datetime.combine(
            due.astimezone(ZoneInfo(policy.timezone)).date(),
            time.fromisoformat(policy.default_deadline_local_time),
        )
        try:
            due = local_to_utc(local, policy.timezone)
        except AmbiguousLocalTime:
            # Computed times choose the later instant, as in the contact ladder.
            zone_info = ZoneInfo(policy.timezone)
            due = max(local.replace(tzinfo=zone_info, fold=f).astimezone(UTC) for f in (0, 1))
        if due <= anchor_at:
            return NeedsClarification(
                reason_code="default_not_future",
                message=rejection + "The default deadline is not after the reference instant.",
            )
        source = DueSource.default
        reason = rejection + f"{kind.value} default under policy {policy.policy_version}."
    escalation = due + timedelta(seconds=grace)
    return ResolvedTiming(
        due_at=due,
        due_source=source,
        due_reason=reason,
        timing_anchor=anchor,
        original_time_expression=original,
        timezone=zone,
        grace_seconds=grace,
        escalation_at=escalation,
        review_at=min(escalation, anchor_at + policy.draft_review_interval)
        if state_hint == "proposed"
        else escalation,
        policy_version=policy.policy_version,
    )


class AmbiguousLocalTime(ValueError):
    """A local clock reading is nonexistent or maps to two different instants."""


def local_to_utc(local_naive: datetime, tz: str) -> datetime:
    if local_naive.tzinfo is not None:
        raise ValueError("local_to_utc requires a naive local clock reading")
    zone = ZoneInfo(_ZONE.validate_python(tz))
    candidates = {local_naive.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)}
    valid = {
        instant
        for instant in candidates
        if instant.astimezone(zone).replace(tzinfo=None) == local_naive
    }
    if len(valid) != 1:
        raise AmbiguousLocalTime("Local time is nonexistent or repeated; ask for an exact instant.")
    return valid.pop()


def format_local(instant: datetime, tz: str) -> str:
    return utc_instant(instant).astimezone(ZoneInfo(_ZONE.validate_python(tz))).isoformat()


def next_action_at(
    mission: Mission,
    now: datetime,
    *,
    contact_eligible: bool = True,
) -> datetime | None:
    now = utc_instant(now)
    if mission.state in TERMINAL_STATES:
        return None
    candidates = [mission.review_at]
    if contact_eligible and mission.next_contact_at is not None and mission.next_contact_at > now:
        candidates.append(mission.next_contact_at)
    if mission.handled_deadline_generation < mission.deadline_generation:
        candidates.append(mission.escalation_at)
    if mission.resume_at is not None:
        candidates.append(mission.resume_at)
    return min(candidates)


def medication_day3_prompt_at(anchor_time: datetime, policy: DoctorTimingPolicy) -> datetime:
    return utc_instant(anchor_time) + policy.medication_day3_offset


def followup_due_at(prompt_at: datetime, policy: DoctorTimingPolicy) -> datetime:
    return utc_instant(prompt_at) + policy.followup_response_window
