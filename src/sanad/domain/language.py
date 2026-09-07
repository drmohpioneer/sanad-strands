"""Contest language policy from Decision 023; Arabic remains an explicit option."""

from typing import Literal

type Language = Literal["ar", "en"]

OWNER_REVIEW_PENDING = True
default_language: Language = "en"
