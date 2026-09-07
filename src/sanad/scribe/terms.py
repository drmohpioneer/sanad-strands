"""Align proposed terms to spoken fragments; never render a free model rewrite."""

import re
import unicodedata

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
