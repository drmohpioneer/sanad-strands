"""Draft spelling resolution, never authority to substitute or prescribe."""

import json
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sanad.domain.boundaries import _BoundaryValue
from sanad.media.numbers import numbers_in

if TYPE_CHECKING:
    from sanad.scribe.extract import DictationCandidate, ProposalIssue

_ARABIC = re.compile(r"[\u0621-\u064a]")
logger = logging.getLogger(__name__)
_DOSE_UNIT = r"(?:mcg|mg|ml|g|مج|مجم|مليجرام|ميكروجرام|مل)"
_DOSE_TOKEN = rf"(?:\d+(?:[.٫]\d+)?(?:{_DOSE_UNIT})?|{_DOSE_UNIT})"
_DOSE_SEPARATOR = r"[\s,،/+;؛:()\-–×]"
_DOSE_SUFFIX = re.compile(
    rf"(?<!\w){_DOSE_TOKEN}(?:{_DOSE_SEPARATOR}+{_DOSE_TOKEN})*{_DOSE_SEPARATOR}*$", re.I
)


def split_drug_dose(drug: str) -> tuple[str, str]:
    """Separate a trailing quantity without interpreting or supplying its strength."""
    match = _DOSE_SUFFIX.search(drug)
    if match:
        name = drug[: match.start()].rstrip(" \t\n,،/+;؛:()-–×")
        if name:
            return name, match[0].strip()
    return drug, ""


def normalize(text: str) -> str:
    text = "".join(c for c in text if not unicodedata.combining(c) and c != "ـ")
    text = text.translate(str.maketrans("أإآٱىئؤة", "ااااييوه"))
    return " ".join(text.casefold().split())


@dataclass(frozen=True)
class NameEntry:
    kind: Literal["drug", "term"]
    latin: str
    generic: str
    arabic_spellings: tuple[str, ...]
    fixed_combination_strengths: tuple[str, ...] = ()
    latin_spellings: tuple[str, ...] = ()


@lru_cache(maxsize=1)
def dictionary() -> tuple[NameEntry, ...]:
    # JSON is a YAML subset; no new runtime dependency or remote dictionary.
    data = json.loads(Path(__file__).with_name("names.yaml").read_text())
    return tuple(
        NameEntry(
            kind=e["kind"],
            latin=e["latin"],
            generic=e["generic"],
            arabic_spellings=tuple(e["arabic_spellings"]),
            fixed_combination_strengths=tuple(e.get("fixed_combination_strengths", ())),
            latin_spellings=tuple(e.get("latin_spellings", ())),
        )
        for e in data["entries"]
    )


def known_names(learned: tuple[str, ...] = ()) -> str:
    """Vocabulary hints contain canonical names only, never strengths or instructions."""
    from sanad.scribe.policy import DRAFT_SCRIBE_POLICY

    names = dict.fromkeys((*learned, *(e.latin for e in dictionary())))
    return ", ".join(tuple(names)[: DRAFT_SCRIBE_POLICY.hint_max_names])


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        row = [i]
        for j, right in enumerate(b, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = row
    return previous[-1]


def entry_for(text: str, kind: Literal["drug", "term"] = "drug") -> NameEntry | None:
    key = normalize(text)
    entries = [e for e in dictionary() if e.kind == kind]
    exact = [
        e
        for e in entries
        if key in {normalize(s) for s in (e.latin, *e.arabic_spellings, *e.latin_spellings)}
    ]
    if len(exact) == 1:
        return exact[0]
    if exact or not key:
        return None
    arabic = bool(_ARABIC.search(key))
    if not arabic and not re.search(r"[a-z]", key):
        return None
    quantities = numbers_in(key)
    distances = []
    for entry in entries:
        spellings = entry.arabic_spellings if arabic else (entry.latin, *entry.latin_spellings)
        distance = min(
            (
                edit_distance(key, spelling)
                for s in spellings
                if abs(len(key) - len(spelling := normalize(s))) <= 2
                and numbers_in(spelling) == quantities
            ),
            default=3,
        )
        distances.append((distance, entry))
    minimum = min(d for d, _ in distances)
    closest = [e for d, e in distances if d == minimum]
    return closest[0] if minimum <= 2 and len(closest) == 1 else None


def latin_in_source(name: str, source: str) -> bool:
    return bool(
        re.fullmatch(r"[A-Za-z][A-Za-z0-9 /+-]*", name)
        and re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", source, re.I)
    )


@dataclass(frozen=True)
class Resolution:
    latin: str | None
    entry: NameEntry | None = None
    conflict: bool = False
    source: Literal["memory", "rxnorm", "seed"] = "seed"


class NameReading(_BoundaryValue):
    item: str
    kind: Literal["drug", "test", "finding"]
    spoken: str
    latin: str
    generic: str = ""
    strengths: tuple[str, ...] = ()
    verified: bool
    learnable: bool = True
    source: Literal["memory", "rxnorm", "seed"] = "seed"


def resolve(
    spoken: str,
    source: str,
    proposed: str | None = None,
    generic: str | None = None,
) -> Resolution:
    resolved = entry_for(spoken)
    suggested = entry_for(proposed) if proposed else None
    if resolved:
        conflict = bool(
            (suggested and normalize(suggested.generic) != normalize(resolved.generic))
            or (generic and normalize(generic) != normalize(resolved.generic))
            or (proposed and suggested is None and not latin_in_source(proposed, source))
        )
        return Resolution(None if conflict else resolved.latin, resolved, conflict)
    if latin_in_source(spoken, source):
        if proposed and normalize(proposed) != normalize(spoken):
            return Resolution(None, conflict=True)
        return Resolution(spoken)
    if suggested:
        conflict = bool(generic and normalize(generic) != normalize(suggested.generic))
        return Resolution(None if conflict else suggested.latin, suggested, conflict)
    if proposed and latin_in_source(proposed, source):
        if suggested and generic and normalize(suggested.generic) != normalize(generic):
            return Resolution(None, suggested, conflict=True)
        return Resolution(proposed, suggested)
    return Resolution(None)


def latin_terms(text: str) -> str:
    """Replace only recognizable terms; retain surrounding words and all quantities."""
    complete = entry_for(text, "term")
    if complete is not None:
        return complete.latin
    normalized = normalize(text)
    spellings = sorted(
        (
            (normalize(s), e.latin)
            for e in dictionary()
            if e.kind == "term"
            for s in (e.latin, *e.arabic_spellings, *e.latin_spellings)
        ),
        key=lambda p: -len(p[0]),
    )
    # Token scanning avoids replacing a component inside a longer already-resolved name.
    pattern = re.compile(
        r"(?<!\w)(و?)(" + "|".join(re.escape(s) for s, _ in spellings) + r")(?!\w)"
    )
    mapping = dict(spellings)

    def fuzzy_latin(fragment: str) -> str:
        # Short acronyms resolve exactly above; do not turn prose such as "in"
        # into an unrelated acronym such as INR through a one-letter edit.
        return re.sub(
            r"(?<!\w)[a-z][a-z0-9-]{3,}(?!\w)",
            lambda m: entry.latin if (entry := entry_for(m[0], "term")) else m[0],
            fragment,
        )

    parts: list[str] = []
    cursor = 0
    for match in pattern.finditer(normalized):
        parts.extend(
            (
                fuzzy_latin(normalized[cursor : match.start()]),
                (", " if match[1] else "") + mapping[match[2]],
            )
        )
        cursor = match.end()
    return "".join((*parts, fuzzy_latin(normalized[cursor:])))


def prepare_names(
    candidate: "DictationCandidate",
    source: str,
    *,
    resolve_name: Callable[[str, str, str | None, str | None], Resolution] | None = None,
    readings: list[NameReading] | None = None,
    verified_names: dict[str, NameReading] | None = None,
) -> tuple["DictationCandidate", tuple["ProposalIssue", ...]]:
    from sanad.scribe.extract import ProposalIssue, extracted_numbers

    issues, orders = [], []
    spoken_names = []
    for i, order in enumerate(candidate.orders):
        spoken, fragment = split_drug_dose(order.drug)
        spoken_names.append(normalize(spoken))
        if fragment:
            # Cleaning cannot hide unsupported digits when an explicit dose already exists.
            unsupported = tuple(n for n in numbers_in(fragment) if n not in numbers_in(source))
            if unsupported:
                issues.append(
                    ProposalIssue(item=f"order:{i}", code="unsupported_number", numbers=unsupported)
                )
            order = order.model_copy(
                update={
                    "drug": spoken,
                    "dose": order.dose if (order.dose or "").strip() else fragment,
                }
            )
        previous = (verified_names or {}).get(f"order:{i}")
        if previous and previous.verified:
            seed = entry_for(previous.latin)
            resolution = Resolution(
                previous.latin,
                NameEntry(
                    "drug",
                    previous.latin,
                    previous.generic,
                    (previous.spoken,),
                    seed.fixed_combination_strengths
                    if seed
                    else tuple(s for s in previous.strengths if "/" in s),
                ),
                source=previous.source,
            )
        else:
            resolution = (resolve_name or resolve)(
                order.drug, source, order.name_latin, order.generic
            )
        if resolution.latin is None:
            issues.append(
                ProposalIssue(
                    item=f"order:{i}",
                    code="drug_unclear",
                    blocked=resolution.conflict
                    or not (
                        resolve_name
                        and order.name_latin
                        and re.fullmatch(r"[A-Za-z][A-Za-z0-9 /+.-]{0,119}", order.name_latin)
                    ),
                    question=f'سمعت "{order.drug}"، اسم الدوا بالإنجليزي إيه؟',
                )
            )
        else:
            order = order.model_copy(update={"drug": resolution.latin})
        if (
            resolve_name
            and resolution.latin is None
            and not resolution.conflict
            and order.name_latin
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9 /+.-]{0,119}", order.name_latin)
        ):
            order = order.model_copy(update={"drug": order.name_latin})
        entry, dose = resolution.entry, order.dose or ""
        if entry and entry.fixed_combination_strengths:
            quantities = numbers_in(dose)
            match = next(
                (s for s in entry.fixed_combination_strengths if quantities == tuple(s.split("/"))),
                None,
            )
            if match and set(quantities) <= set(numbers_in(source)):
                unit = " mg" if re.search(r"\bmg\b|مج|مجم|مليجرام", dose) else ""
                order = order.model_copy(update={"dose": match + unit})
            elif dose:
                suggestion = (
                    "5/160/12.5"
                    if (
                        # Both compressed prefixes are digit subsequences of 5160.
                        quantities in {("560", "12.5"), ("516", "12.5")}
                        or normalize(dose) == normalize("خمسة مية وستين اتناشر ونص")
                    )
                    and "5/160/12.5" in entry.fixed_combination_strengths
                    else None
                )
                question = f'سمعت "{dose}" لـ {entry.latin}، '
                question += f"قصدك {suggestion}؟" if suggestion else "الجرعة كاملة إيه؟"
                issues.append(
                    ProposalIssue(item=f"order:{i}", code="dose_unclear", question=question)
                )
        elif re.search(r"\d\s*(?:على|/)\s*\d", dose):
            issues.append(
                ProposalIssue(
                    item=f"order:{i}",
                    code="dose_unclear",
                    question=f'سمعت "{dose}" لـ {order.drug}، الجرعة المقصودة إيه؟',
                )
            )
        orders.append(order)
        if readings is not None:
            readings.append(
                NameReading(
                    item=f"order:{i}",
                    kind="drug",
                    spoken=previous.spoken if previous else spoken[:120],
                    latin=order.drug,
                    generic=resolution.entry.generic if resolution.entry else order.generic or "",
                    strengths=(order.dose,) if order.dose else (),
                    verified=resolution.latin is not None,
                    source=resolution.source,
                )
            )
    missions = tuple(
        m.model_copy(update={"text": latin_terms(m.text)})
        if m.kind == "TEST" and resolve_name is None
        else m
        for m in candidate.missions
    )
    kept_facts = tuple(
        f
        for f in candidate.facts
        if not (
            f.category == "medication_history"
            and any(name and name in normalize(f.text) for name in spoken_names)
        )
    )
    dropped = len(candidate.facts) - len(kept_facts)
    if dropped:
        logger.info("scribe_medication_history_dropped count=%d", dropped)
    facts = tuple(
        f.model_copy(update={"text": latin_terms(f.text)})
        if resolve_name is None
        and f.category == "history"
        and re.search(r"ECG|Echo|إيكو|ايكو|الأكو|رسم قلب", f.text, re.I)
        else f
        for f in kept_facts
    )
    prepared = candidate.model_copy(
        update={"orders": tuple(orders), "missions": missions, "facts": facts}
    )
    # Removed material cannot conceal a source number or an unsupported model digit.
    # Numbers already placed in surviving fields must not become duplicate questions.
    represented = set(extracted_numbers(prepared))
    prepared._dropped_numbers = tuple(
        dict.fromkeys(
            (
                *candidate._dropped_numbers,
                *(n for n in extracted_numbers(candidate) if n not in represented),
            )
        )
    )
    return prepared, tuple(issues)
