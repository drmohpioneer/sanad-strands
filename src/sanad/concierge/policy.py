"""One draft instance; these are operational limits, not clinical approval."""

from datetime import timedelta
from typing import Literal

from sanad.domain.boundaries import _BoundaryValue


class ConciergePolicy(_BoundaryValue):
    window: int = 6
    reply_max_chars: int = 700
    snooze_max: timedelta = timedelta(days=7)
    question_due: timedelta = timedelta(hours=48)
    question_dedupe: timedelta = timedelta(hours=24)
    education_top_k: int = 2
    start_clarification_window: timedelta = timedelta(hours=24)
    start_anchor_max_age: timedelta = timedelta(days=7)
    barrier_resume_after: timedelta = timedelta(days=1)
    # Contract 15 draft policy: OWNER_REVIEW_PENDING.
    visit_brief_offset: timedelta = timedelta(days=1)
    task_reopen_offset: timedelta = timedelta(days=3)
    question_list_ttl: timedelta = timedelta(hours=1)


DRAFT_CONCIERGE_POLICY = ConciergePolicy()

OWNER_REVIEW_PENDING = True

type BarrierType = Literal[
    "cost", "availability", "forgot", "confusion", "side_effect_experience", "other"
]
BARRIER_CATEGORIES: tuple[BarrierType, ...] = (
    "cost",
    "availability",
    "forgot",
    "confusion",
    "side_effect_experience",
    "other",
)
