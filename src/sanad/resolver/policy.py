"""Released operational defaults; no clinical approval is implied."""

from datetime import timedelta

from sanad.domain.boundaries import _BoundaryValue

OWNER_REVIEW_PENDING = True


class ResolverPolicy(_BoundaryValue):
    search_radius_m: int = 5000
    max_results: int = 10
    shown_results: int = 3
    question_budget: int = 1
    search_budget: int = 2
    attempt_ttl: timedelta = timedelta(days=7)
    amenities: tuple[str, ...] = ("pharmacy", "doctors", "clinic", "hospital", "laboratory")


POLICY = ResolverPolicy()
