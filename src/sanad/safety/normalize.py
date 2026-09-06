"""Source repository: ../sanad (frozen Google Sanad).
Source commit: b65f569.
Source path: app/core/sentinel.py.
Copy date: 2026-09-06.
Slice 04 modifications:
- Extract the normalizer without changing its implementation or tables.

Unknown is never normal; a missing protocol is not a safe result.
Screening covers readable text and captions only, never hidden content of
unprocessed media; callers own the media_failure route. A verdict is not a
diagnosis. Original adult cardiology cohort; Sanad v2 re-approval pending.

The extracted legacy Arabic/Franco/English comparison normalizer."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
# Arabic is written with optional diacritics and several spellings of the same
# letter; Franco-Arabic writes sounds as digits (3 = ع, 7 = ح, 2 = ء). We strip
# the first, unify the second and keep the digits, then reduce everything else
# to single spaces. The table entries go through the same function at import, so
# input and table are always compared in the same alphabet.
_DIACRITICS = re.compile(r"[ً-ٰٟـ]")
_LETTER_VARIANTS = str.maketrans(
    {"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ي": "ي", "ة": "ه",
     "ؤ": "و", "ئ": "ي", "ک": "ك", "ی": "ي", "ﻻ": "لا"}
)
_NON_TEXT = re.compile(r"[^0-9a-zء-ي ]+")

# Franco-Arabic has no spelling authority: the same word is written six ways by
# six people, and a phrase table can only ever hold one of them. This is the
# alias table the red team's bypasses needed - "nafasy", "nfsy" and "nafsi" are
# one word, "sadry" and "sdry" are one word. It is applied word by word inside
# normalize(), so the table entries and the patient's message are folded the
# same way and a variant can never be "not in the table".
FRANCO_ALIASES: dict[str, str] = {
    "nafasy": "nafsi", "nafsy": "nafsi", "nfsy": "nafsi", "nafasi": "nafsi",
    "nafs": "nafsi", "nfs": "nafsi", "anfas": "nafsi", "nafas": "nafsi",
    "sadry": "sadri", "sdry": "sadri", "sadre": "sadri", "sader": "sadri",
    "msh": "mesh", "mish": "mesh", "mosh": "mesh",
    "2ader": "2ader", "2adr": "2ader", "ader": "2ader",
    "a5od": "akhod", "akhud": "akhod", "akhd": "akhod",
    "wg3ny": "wag3ny", "wage3ny": "wag3ny", "waga3ny": "wag3ny",
    "2alb": "2alby", "alby": "2alby",
    "eid": "idi", "edi": "idi", "eidi": "idi", "dera3y": "dera3",
    "we2e3": "we2e3", "wa2a3": "we2e3", "wo2e3": "we2e3",
    "shafayfy": "shafayef", "shafayef": "shafayef", "shafayfi": "shafayef",
}


def normalize(text: Optional[str]) -> str:
    """Text -> comparison form, space-padded so matches land on word edges.

    Diacritics go, the several spellings of one Arabic letter are unified, the
    Franco spellings of one word are folded onto one of them (FRANCO_ALIASES),
    and everything else becomes single spaces.
    """
    s = unicodedata.normalize("NFKC", text or "")
    s = _DIACRITICS.sub("", s)
    s = s.translate(_LETTER_VARIANTS).lower()
    s = _NON_TEXT.sub(" ", s)
    words = [FRANCO_ALIASES.get(w, w) for w in s.split()]
    return " " + " ".join(words) + " "


