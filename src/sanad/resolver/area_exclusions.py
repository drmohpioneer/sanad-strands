"""Clinical tokens may only exclude a search area; they cannot classify problems."""

import re
from functools import lru_cache

from sanad.concierge.barrier_evidence import NEGATIONS
from sanad.safety.labs import ALIASES, PANEL_ANALYTES
from sanad.safety.normalize import FRANCO_ALIASES, normalize
from sanad.safety.sentinel import CONCEPT_RULES, MUST_WAKE, NEEDS_SUPPORT, NEVER_WAKE
from sanad.scribe.names import dictionary

# Retained verbatim from the retired barrier seed, solely for search privacy.
SYMPTOMS = (
    "makes me feel unwell",
    "dizzy",
    "dizziness",
    "nausea",
    "nauseous",
    "بيتعبني",
    "دوخة",
    "غثيان",
)


@lru_cache(maxsize=1)
def patterns() -> tuple[tuple[str, bool], ...]:
    phrases = [
        *SYMPTOMS,
        *NEGATIONS,
        *NEVER_WAKE,
        *FRANCO_ALIASES,
        *FRANCO_ALIASES.values(),
        *ALIASES,
        *ALIASES.values(),
        *PANEL_ANALYTES,
        *(a for values in PANEL_ANALYTES.values() for a in values),
        *(p for _, values in MUST_WAKE for p in values),
        *NEEDS_SUPPORT,
        *(p for values in NEEDS_SUPPORT.values() for p in values),
        *(p for _, groups in CONCEPT_RULES for values in groups for p in values),
        *(
            p
            for e in dictionary()
            if e.kind == "drug"
            for p in (e.latin, e.generic, *e.arabic_spellings, *e.latin_spellings)
        ),
    ]
    return tuple(
        dict.fromkeys(
            (token, raw.endswith("*"))
            for phrase in phrases
            for raw in phrase.split()
            for token in normalize(raw.removesuffix("*")).split()
        )
    )


def excluded(text: str, mission_names: tuple[str, ...] = ()) -> bool:
    tokens = normalize(text).split()
    dynamic = tuple((t, False) for name in mission_names for t in normalize(name).split())
    return any(
        stem in token
        if wildcard and re.search(r"[ء-ي]", stem)
        else token.startswith(stem)
        if wildcard
        else token == stem
        for stem, wildcard in (*patterns(), *dynamic)
        for token in tokens
    )
