"""One draft instance; these are operational limits, not clinical approval."""

from datetime import timedelta

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
    barrier_seed: str = "concierge/barriers.yaml"


DRAFT_CONCIERGE_POLICY = ConciergePolicy()

OWNER_REVIEW_PENDING = True
