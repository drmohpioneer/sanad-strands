"""Released operational draft values; no clinical thresholds live here."""

from dataclasses import dataclass
from datetime import timedelta

OWNER_REVIEW_PENDING = True


@dataclass(frozen=True)
class EvidencePolicy:
    association_clarification: timedelta = timedelta(hours=24)
    max_candidates_per_receipt: int = 3
    evidence_max_pages_per_turn: int = 5
    identity_match_min_tokens: int = 1
    duplicate_window: None = None


POLICY = EvidencePolicy()
