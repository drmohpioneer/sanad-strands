"""Align proposed terms to spoken fragments; never render a free model rewrite."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import TYPE_CHECKING

from sanad.media.numbers import numbers_in

if TYPE_CHECKING:
    from sanad.scribe.extract import DictationCandidate

from sanad.scribe.extract import ClinicalKind, FactCandidate, FactTerm
from sanad.scribe.lookup import DrugLookupService
from sanad.scribe.names import NameReading, normalize

_MARKER = re.compile(r"مريض\s+(?:جديد|تاني|تانى)")
_QUANTITY = re.compile(r"[0-9]+(?:\.[0-9]+)?\s*[%٪]?|[%٪]")


def vocabulary_term(text: str) -> str:
    """Values stay on the clinical card, never in a reusable finding alias."""
    return " ".join(_QUANTITY.sub("", text).strip(" ,،/%٪").split())


def _key(text: str) -> str:
    return normalize(re.sub(r"^(?:ECG|Echo|Complaint|History|Dx):\s*", "", text, flags=re.I))


def _normalized(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    offsets: list[int] = []
    digits = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٪", "01234567890123456789.%")
    for i, char in enumerate(text):
        if unicodedata.combining(char) or char == "ـ":
            continue
        value = " " if char.isspace() else normalize(char.translate(digits))
        if value == " " and (not chars or chars[-1] == " "):
            continue
        for letter in value:
            chars.append(letter)
            offsets.append(i)
    if chars and chars[-1] == " ":
        chars.pop()
        offsets.pop()
    return "".join(chars), offsets


def verified_term(spoken: str, english: str, service: DrugLookupService) -> bool:
    from sanad.scribe.resolver import context, resolve_name

    resolved = resolve_name(spoken, "finding", spoken, english, context(service))
    return resolved.latin == english and resolved.tier != "unresolved"


def fact_kind(category: str, first: str) -> ClinicalKind:
    """Presentation prefixes belong to code, never a model-selected field."""
    if category == "condition":
        return "Dx"
    if category == "complaint":
        return "Complaint"
    if category != "finding":
        return "History"
    name = normalize(first)
    if name.startswith(("ecg", "t wave", "st ", "lvh", "rbbb", "lbbb", "sinus", "af")):
        return "ECG"
    if name.startswith(("echo", "ef", "segmental", "hypokinesia")):
        return "Echo"
    return "Finding"


def aligned_facts(
    fact: FactCandidate,
    patient_name: str | None,
    service: DrugLookupService,
    source: str,
) -> tuple[tuple[FactCandidate, tuple[NameReading, ...]], ...]:
    from sanad.scribe.resolver import Resolved, context, resolve_fragments, resolve_name

    text = fact.text
    if marker := _MARKER.search(text):
        text = text[: marker.start()] + text[marker.end() :]
        if patient_name:
            text = text.replace(patient_name, "")
        text = re.sub(r"(?:عنده\s+)?\d+\s+سنة", "", text).strip(" ،,:")
    if not text:
        return ()
    ctx = context(service)
    # A model-created source fragment cannot verify its own English reading.
    source_key, _ = _normalized(source)
    text_key, _ = _normalized(text)
    anchored = text_key in source_key
    if text.isascii():
        # English punctuation, including T-wave, does not change the spoken words.
        def tokens(value: str) -> str:
            return " ".join(re.findall(r"[a-z0-9%]+", value.casefold()))

        anchored = bool(tokens(text)) and tokens(text) in tokens(source)
    fragments = (
        list(resolve_fragments(text, "finding", source, ctx))
        if anchored
        else [Resolved(None, None, "unresolved", text)]
    )
    if (
        fact.name_latin
        and anchored
        and not any(r.tier in {"memory", "clinic", "seed", "lookup"} for r in fragments)
    ):
        proposed = resolve_name(text, "finding", source, fact.name_latin, ctx)
        if proposed.latin or len(fragments) == 1:
            fragments = [proposed]
    first = next((r.latin for r in fragments if r.latin), "")
    kind = fact_kind(fact.category, first)
    # The old seed contains one presentation prefix. It is a code prefix, not
    # part of the term, and must not be duplicated beside the new fixed prefix.
    readings = tuple(
        NameReading(
            item="",
            kind="finding",
            spoken=r.spoken,
            latin=re.sub(r"^(?:ECG|Echo|Complaint|History|Dx):\s*", "", r.latin or r.spoken),
            generic=r.generic or "",
            verified=r.latin is not None,
            learnable=anchored and not service.contains_identity(r.spoken),
            source=r.legacy().source,
        )
        for r in fragments
    )
    pairs = tuple(
        FactTerm(spoken=r.spoken, english=r.latin if r.verified else None, kind=kind)
        for r in readings
    )
    return (
        (
            fact.model_copy(
                update={"text": text, "terms": pairs, "clinical_en": None, "clinical_kind": kind}
            ),
            readings,
        ),
    )


INSTRUCTION_VERBS_EN = (
    "request order check measure start stop hold continue send give take do repeat"
)
INSTRUCTION_VERBS_AR = (
    "اطلب اعمل اعمله يعمل حلل يحلل ابدأ ابدا يبدأ وقف اوقف يوقف كمل يكمل قيس يقيس ابعت يبعت خد ياخد"
)
INSTRUCTION_WORDS = (
    "for now today daily and or to of in on with at from until then "
    "his her him their it a an the once twice times day days week weeks month months "
    "morning evening night one two three four five six seven eight nine ten "
    "و او في على علي من ل مع هو هي "
    "مرة مرتين مرات يوم يومين ايام أيام اسبوع أسبوع الصبح بالليل النهارده دلوقتي"
)
INSTRUCTION_EXCLUSIONS = (
    "diabetes diabetic sugar blood sugar سكر thyroid liver kidney renal gout inflammation "
    "count salts protein glycated lipid lipids cholesterol ضغط الضغط ضغطه ضغط الدم "
    "high low raised elevated uncontrolled controlled known pregnancy pregnant ca cancer "
    "gas gases clotting coagulation زمان قديم من زمان"
)


def instruction_tokens(text: str) -> frozenset[str]:
    from sanad.scribe.grounding import _TOKEN, normalize

    return frozenset(_TOKEN.findall(normalize(text)))


@lru_cache(maxsize=1)
def instruction_vocabulary() -> frozenset[str]:
    from sanad.safety.labs import ALIASES, PANEL_ANALYTES
    from sanad.scribe.names import dictionary

    phrases = [
        *ALIASES.keys(),
        *ALIASES.values(),
        *PANEL_ANALYTES.keys(),
        *(v for values in PANEL_ANALYTES.values() for v in values),
        "bp blood pressure glucose blood glucose weight pulse heart rate",
        INSTRUCTION_VERBS_EN,
        INSTRUCTION_VERBS_AR,
        INSTRUCTION_WORDS,
    ]
    for entry in dictionary():
        if entry.kind == "drug":
            phrases.extend(
                (entry.latin, entry.generic, *entry.arabic_spellings, *entry.latin_spellings)
            )
    return instruction_tokens(" ".join(phrases)) - instruction_tokens(INSTRUCTION_EXCLUSIONS)


def instruction_content(text: str, candidate: DictationCandidate) -> bool:
    """Closed token vocabulary, with condition exclusions winning over every source."""
    drugs = " ".join(
        value
        for order in candidate.orders
        for value in (order.drug, order.name_latin, order.generic)
        if value
    )
    tokens = instruction_tokens(text)
    excluded = instruction_tokens(INSTRUCTION_EXCLUSIONS)
    vocabulary = (instruction_vocabulary() | instruction_tokens(drugs)) - excluded
    return (
        bool(tokens)
        and not (tokens & excluded)
        and all(token in vocabulary or token.isdecimal() for token in tokens)
    )


def instruction_verb(text: str) -> bool:
    return bool(
        instruction_tokens(text)
        & instruction_tokens(INSTRUCTION_VERBS_EN + " " + INSTRUCTION_VERBS_AR)
    )


def drop_instruction_fact(fact: FactCandidate, candidate: DictationCandidate, source: str) -> bool:
    return (
        fact.category in {"condition", "history", "medication_history"}
        and not (set(numbers_in(fact.text)) - set(numbers_in(source)))
        and instruction_content(fact.text, candidate)
        and (
            instruction_verb(fact.text)
            or bool(instruction_tokens(fact.text) - instruction_tokens(source))
        )
    )
