"""Explicit operational policy and clock-only accountability events for slice 03."""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field

from sanad.domain.boundaries import (
    NonblankStr,
    NonnegativeInt,
    PositiveVersion,
    UtcInstant,
    _BoundaryValue,
)

type PositiveDuration = Annotated[timedelta, Field(gt=timedelta())]


class OperationalClock(_BoundaryValue):
    next_action_at: UtcInstant
    work_lane: Literal["ingress", "delivery", "account", "operational", "claim"]
    work_shard: NonblankStr = "0"
    work_generation: PositiveVersion = 1
    attempt_count: NonnegativeInt = 0
    last_error_code: NonblankStr | None = None


def transition_operational_clock(
    clock: OperationalClock, at: datetime, *, attempt_delta: int = 0, error: str | None = None
) -> OperationalClock:
    return OperationalClock.model_validate(
        clock.model_dump()
        | {
            "next_action_at": at,
            "work_generation": clock.work_generation + 1,
            "attempt_count": clock.attempt_count + attempt_delta,
            "last_error_code": error,
        }
    )


class OperationsPolicy(_BoundaryValue):
    retry_schedule: Annotated[tuple[PositiveDuration, ...], Field(min_length=1)] = (
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=15),
        timedelta(hours=1),
        timedelta(hours=4),
    )
    max_inbound_attempts: PositiveVersion = 5
    max_delivery_attempts: PositiveVersion = 5
    lease_ttl: PositiveDuration = timedelta(minutes=5)
    claim_ttl: PositiveDuration = timedelta(minutes=10)
    danger_uncertain_resend_limit: Literal[1] = 1

    def retry_backoff(self, attempt_count: int) -> timedelta:
        if type(attempt_count) is not int or attempt_count < 1:
            raise ValueError("attempt_count must be positive")
        return self.retry_schedule[min(attempt_count, len(self.retry_schedule)) - 1]


class AccountabilityWake(_BoundaryValue):
    """Internal bookkeeping, separate from the accepted clinical event vocabulary."""

    event_id: NonblankStr
    event_type: Literal["ACCOUNTABILITY_WAKE"] = "ACCOUNTABILITY_WAKE"
