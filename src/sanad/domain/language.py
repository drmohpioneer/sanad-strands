"""Contest language policy from Decision 023; Arabic remains an explicit option."""

import os
from typing import Literal

type Language = Literal["ar", "en"]
type Audience = Literal["doctor", "patient"]

OWNER_REVIEW_PENDING = True
default_language: Language = "en"


def contest_english() -> bool:
    """Decision 023: the judged build speaks English whatever a record stores.

    An account created before the default changed still stores ``ar``, and that
    stored preference must not be able to hand a judge an Arabic surface. The
    stored value is never rewritten; only its visible effect is suspended, so
    switching this off restores every doctor's own choice.
    """
    return os.environ.get("SANAD_CONTEST_ENGLISH", "1") != "0"


def effective(language: str, *, audience: Audience = "doctor") -> Language:
    """The language a rendered string is actually produced in."""
    # Preferences belong to the recipient, never implicitly to their doctor.
    # Decision 023 currently overrides each audience identically.
    if audience not in {"doctor", "patient"}:
        raise ValueError("language_audience")
    if contest_english():
        return "en"
    return "ar" if language == "ar" else "en"
