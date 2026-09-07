"""Draft presentation policy; mission clocks use DoctorTimingPolicy."""

from datetime import timedelta
from typing import Literal

from sanad.domain.boundaries import _BoundaryValue
from sanad.media.limits import MAX_IMAGE_BYTES


class ScribePolicy(_BoundaryValue):
    review_status: Literal["OWNER_REVIEW_PENDING"] = "OWNER_REVIEW_PENDING"
    rxnorm_timeout: float = 3
    lookups_per_card: int = 6
    name_cache_ttl: timedelta = timedelta(days=30)
    hint_max_names: int = 400
    spoken_forms_max: int = 10
    clinical_en_max_chars: int = 120
    correction_window: timedelta = timedelta(minutes=30)
    intake_review_interval: timedelta = timedelta(hours=24)
    max_photo_bytes: int = MAX_IMAGE_BYTES
    shift_guard_enabled: bool = True
    proposal_ttl: timedelta
    max_candidates: int
    card_max_chars: int
    min_drug_chars: int
    default_language: Literal["ar"]
    qr_scale: int


DRAFT_SCRIBE_POLICY = ScribePolicy(
    proposal_ttl=timedelta(minutes=30),
    max_candidates=5,
    card_max_chars=3500,
    min_drug_chars=3,
    default_language="ar",
    qr_scale=6,
)
