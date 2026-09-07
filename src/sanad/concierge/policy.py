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


DRAFT_CONCIERGE_POLICY = ConciergePolicy()
