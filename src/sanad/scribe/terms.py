"""Align proposed terms to spoken fragments; never render a free model rewrite."""

import re
import unicodedata
from collections.abc import Callable

from sanad.scribe.extract import ClinicalKind, FactCandidate, FactTerm
from sanad.scribe.lookup import DrugLookupService
from sanad.scribe.names import NameReading, dictionary, edit_distance, entry_for, normalize
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY

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


def _phonetic(english: str) -> str:
    """Bounded mechanical English-to-Arabic spelling, with no medical semantics."""
    value = normalize(english)
    for left, right in (
        ("sh", "ش"),
        ("ch", "تش"),
        ("ph", "ف"),
        ("th", "ث"),
        ("kh", "خ"),
        ("gh", "غ"),
        ("oo", "و"),
        ("ee", "ي"),
    ):
        value = value.replace(left, right)
    letters: dict[str, str | int | None] = {
        "a": "ا",
        "b": "ب",
        "c": "ك",
        "d": "د",
        "e": "ي",
        "f": "ف",
        "g": "ج",
        "h": "ه",
        "i": "ي",
        "j": "ج",
        "k": "ك",
        "l": "ل",
        "m": "م",
        "n": "ن",
        "o": "و",
        "p": "ب",
        "q": "ك",
        "r": "ر",
        "s": "س",
        "t": "ت",
        "u": "و",
        "v": "ف",
        "w": "و",
        "x": "كس",
        "y": "ي",
        "z": "ز",
    }
    return value.translate(str.maketrans(letters))


def verified_term(spoken: str, english: str, service: DrugLookupService) -> bool:
    from sanad.scribe.clinical import valid_english

    if (
        not valid_english(english, spoken)
        or ("%" in english and not re.search(r"[%٪]", spoken))
        or service.contains_identity(spoken)
        or service.contains_identity(english)
    ):
        return False
    raw, proposed = vocabulary_term(spoken), vocabulary_term(english)
    if not raw or not proposed:
        return False
    remembered = service.vocabulary.find(raw, "finding")
    if remembered:
        return _key(remembered.latin) == _key(proposed)
    seed = entry_for(raw, "term")
    if seed:
        return _key(seed.latin) == _key(proposed)
    # A known English term still needs its spoken identity. Mere membership in
    # the vocabulary must never verify LVH for an unrelated spoken finding.
    target = next(
        (e for e in dictionary() if e.kind == "term" and _key(e.latin) == _key(proposed)), None
    )
    if target and any(_key(alias) == _key(raw) for alias in target.arabic_spellings):
        return True
    left = normalize(raw)
    right = (
        normalize(_phonetic(proposed))
        if re.search(r"[\u0621-\u064a]", raw)
        else normalize(proposed)
    )
    return abs(len(left) - len(right)) <= 2 and edit_distance(left, right) <= 2


def _chunks(text: str) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip(" ,،;؛:\n")
    while remaining:
        end = min(len(remaining), DRAFT_SCRIBE_POLICY.clinical_en_max_chars)
        if end < len(remaining):
            end = remaining.rfind(" ", 0, end + 1) or end
            if end < 1:
                end = DRAFT_SCRIBE_POLICY.clinical_en_max_chars
        chunks.append(remaining[:end])
        remaining = remaining[end:].lstrip()
    return chunks


def aligned_facts(
    fact: FactCandidate,
    patient_name: str | None,
    service: DrugLookupService,
    verify_drugs: Callable[[str, str], bool],
    source: str,
) -> tuple[tuple[FactCandidate, tuple[NameReading, ...]], ...]:
    """Produce source-ordered lines and code-owned display readings, including gaps."""
    from sanad.scribe.clinical import normalize_units, valid_english

    text = fact.text
    if marker := _MARKER.search(text):
        # The marker and its explicitly extracted demographics are not history.
        text = text[: marker.start()] + text[marker.end() :]
        if patient_name:
            text = text.replace(patient_name, "")
        text = re.sub(r"(?:عنده\s+)?\d+\s+سنة", "", text).strip(" ،,:")
        if not text:
            return ()
    normalized, offsets = _normalized(text)
    source_key, _ = _normalized(source)
    used: list[tuple[int, int, FactTerm]] = []
    for term in fact.terms:
        key, _ = _normalized(term.spoken)
        if not key or not offsets:
            continue
        for match in re.finditer(re.escape(key), normalized):
            start, end = offsets[match.start()], offsets[match.end() - 1] + 1
            if not any(start < b and a < end for a, b, _ in used):
                used.append((start, end, term))
                break
    used.sort(key=lambda x: x[0])
    # Overlapping claims cannot replace or reorder the same spoken material twice.
    fragments: list[tuple[str, str | None, ClinicalKind, bool]] = []

    def gap(raw: str, kind: ClinicalKind) -> None:
        for chunk in _chunks(raw):
            remembered = service.vocabulary.find(vocabulary_term(chunk), "finding")
            # Only the Arabic fallback actually displayed at confirmation can be
            # reused unchanged. Never borrow a rejected, invisible translation.
            accepted = bool(remembered and _key(remembered.latin) == _key(vocabulary_term(chunk)))
            fragments.append((chunk, None, kind, accepted))

    cursor = 0
    for start, end, term in used:
        kind = term.kind or fact.clinical_kind
        gap(text[cursor:start], kind if cursor == 0 else fragments[-1][2])
        spoken = text[start:end]
        english = normalize_units(term.english or "")
        spoken_key, _ = _normalized(spoken)
        displayable = (
            spoken_key in source_key
            and valid_english(english, spoken)
            and ("%" not in english or bool(re.search(r"[%٪]", spoken)))
            and not service.contains_identity(spoken)
            and not service.contains_identity(english)
            and verify_drugs(english, spoken)
        )
        if displayable:
            # The doctor can verify an anchored translation on the displayed card.
            # A vocabulary miss changes its marker, never hides that proposal.
            fragments.append((spoken, english, kind, verified_term(spoken, english, service)))
        else:
            gap(spoken, kind)
        cursor = end
    gap(text[cursor:], fragments[-1][2] if fragments else fact.clinical_kind)
    if not fragments:
        return ()
    groups: list[list[tuple[str, str | None, ClinicalKind, bool]]] = []
    for fragment in fragments:
        if not groups or groups[-1][-1][2] != fragment[2]:
            groups.append([])
        groups[-1].append(fragment)
    result = []
    for group in groups:
        if (
            len(", ".join(en for _, en, _, _ in group if en))
            > DRAFT_SCRIBE_POLICY.clinical_en_max_chars
        ):
            group = [(raw, None, kind, False) for raw, _, kind, _ in group]
        spoken_text = text if len(groups) == 1 else " ".join(p[0] for p in group)
        kind = group[0][2]
        pairs = tuple(FactTerm(spoken=raw, english=en, kind=kind) for raw, en, _, _ in group)
        readings = tuple(
            NameReading(
                item="",
                kind="finding",
                spoken=raw,
                latin=en or raw,
                verified=accepted,
                learnable=not service.contains_identity(raw),
            )
            for raw, en, _, accepted in group
        )
        result.append(
            (
                fact.model_copy(
                    update={
                        "text": spoken_text,
                        "clinical_en": None,
                        "clinical_kind": kind,
                        "terms": pairs,
                    }
                ),
                readings,
            )
        )
    return tuple(result)
