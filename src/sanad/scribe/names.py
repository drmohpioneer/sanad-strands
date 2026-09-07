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


def heard_dose(drug: str, source: str) -> str:
    """Read only a literal numeric suffix after this spoken drug, without inference."""
    spoken = normalize(split_drug_dose(drug)[0])
    match = re.search(
        r"(?<!\w)"
        + re.escape(spoken)
        + r"(?!\w)\s+"
        + rf"(\d+(?:[.٫]\d+)?(?:[\s/,،]+\d+(?:[.٫]\d+)?)*(?:\s*{_DOSE_UNIT})?)",
        normalize(source),
        re.I,
    )
    return match[1].strip() if match else ""


def compound_question(dose: str, entry: "NameEntry") -> str:
    suggestion = (
        "5/160/12.5"
        if (
            numbers_in(dose) in {("560", "12.5"), ("516", "12.5")}
            or normalize(dose) == normalize("خمسة مية وستين اتناشر ونص")
        )
        and "5/160/12.5" in entry.fixed_combination_strengths
        else None
    )
    return f'سمعت "{dose}" لـ {entry.latin}، ' + (
        f"قصدك {suggestion}؟" if suggestion else "الجرعة كاملة إيه؟"
    )


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
    from sanad.scribe.resolver import hint_names

    return hint_names(learned=learned)


def edit_distance(a: str, b: str) -> int:
    from sanad.scribe.resolver import edit_distance as distance

    return distance(a, b)


def entry_for(text: str, kind: Literal["drug", "term"] = "drug") -> NameEntry | None:
    from sanad.scribe.resolver import entry_for as find_entry

    return find_entry(text, kind)


def latin_in_source(name: str, source: str) -> bool:
    from sanad.scribe.resolver import latin_in_source as anchored

    return anchored(name, source)


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
    spoken: str, source: str, proposed: str | None = None, generic: str | None = None
) -> Resolution:
    from sanad.scribe.resolver import Context, resolve_name

    return resolve_name(spoken, "drug", source, proposed, Context(generic=generic)).legacy()


def latin_terms(text: str) -> str:
    from sanad.scribe.resolver import latin_terms as render_terms

    return render_terms(text)


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
            from sanad.scribe.clinical import TERM_QUESTION

            issues.append(
                ProposalIssue(
                    item=f"order:{i}",
                    code="drug_unclear",
                    blocked=resolution.conflict,
                    question=TERM_QUESTION,
                )
            )
        else:
            order = order.model_copy(update={"drug": resolution.latin})
        if order.frequency and normalize(order.frequency) not in normalize(source):
            # Retain unsupported digits for the unchanged numeric guard. A word
            # or a source-supported but unspoken shorthand is never an instruction.
            if set(numbers_in(order.frequency)) <= set(numbers_in(source)):
                order = order.model_copy(update={"frequency": None})
        entry, dose = resolution.entry, order.dose or ""
        if entry and entry.fixed_combination_strengths:
            quantities = numbers_in(dose)
            match = next(
                (s for s in entry.fixed_combination_strengths if quantities == tuple(s.split("/"))),
                None,
            )
            explicit_components = (
                re.fullmatch(r"\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)+(?:\s*mg)?", dose, re.I)
                and len(dose.split("/")) == len(entry.generic.split("/"))
                and dose.casefold() in source.casefold()
            )
            if (match or explicit_components) and set(quantities) <= set(numbers_in(source)):
                unit = " mg" if re.search(r"\bmg\b|مج|مجم|مليجرام", dose) else ""
                order = order.model_copy(update={"dose": match + unit if match else dose})
            elif dose:
                issues.append(
                    ProposalIssue(
                        item=f"order:{i}",
                        code="dose_unclear",
                        question=compound_question(dose, entry),
                    )
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
                    learnable=resolution.latin is not None,
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
    if dropped:
        from sanad.scribe.merge import remap_metadata

        targets = {
            f"fact:{i}": (f"fact:{kept_facts.index(fact)}",) if fact in kept_facts else ("all",)
            for i, fact in enumerate(candidate.facts)
        }
        remap_metadata(prepared, targets)
        issues = [q.model_copy(update={"item": targets.get(q.item, (q.item,))[0]}) for q in issues]
        if readings is not None:
            readings[:] = [
                n.model_copy(update={"item": targets.get(n.item, (n.item,))[0]})
                for n in readings
                if targets.get(n.item) != ("all",)
            ]
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
