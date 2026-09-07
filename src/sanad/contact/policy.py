"""Released implementation numbers; clinical/wording owner review remains pending."""

from datetime import timedelta
from typing import Annotated

from pydantic import Field

from sanad.domain.boundaries import PositiveVersion, _BoundaryValue
from sanad.domain.entities import DRAFT_POLICY_2026_09
from sanad.domain.operations import PositiveDuration

OWNER_REVIEW_PENDING = True


class ContactPolicy(_BoundaryValue):
    per_mission_chase_limit: PositiveVersion
    daily_chase_limit: PositiveVersion
    chase_min_gap: PositiveDuration
    chase_local_hour: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]
    scheduled_prompt_window: PositiveDuration
    day3_prompt_window: PositiveDuration
    unreachable_after: PositiveVersion
    bundle_interval: PositiveDuration
    bundle_max_lines: PositiveVersion


DRAFT_CONTACT_POLICY = ContactPolicy(
    per_mission_chase_limit=3,
    daily_chase_limit=1,
    chase_min_gap=timedelta(hours=24),
    chase_local_hour="10:00",
    scheduled_prompt_window=timedelta(hours=2),
    day3_prompt_window=DRAFT_POLICY_2026_09.followup_response_window,
    unreachable_after=3,
    bundle_interval=timedelta(days=7),
    bundle_max_lines=20,
)
