"""Merge two independent typed readings without letting either choose the truth."""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from sanad.media.numbers import numbers_in
from sanad.scribe.extract import (
    DictationCandidate,
    FactCandidate,
    MissionCandidate,
    OrderCandidate,
    PatientCandidate,
    ProposalIssue,
    extracted_numbers,
)
from sanad.scribe.names import compound_question, heard_dose, normalize, split_drug_dose
from sanad.scribe.resolver import Context, NameKind, resolve_fragments, resolve_name

type Item = OrderCandidate | FactCandidate | MissionCandidate | str
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeResult:
    candidate: DictationCandidate
    single_source: tuple[str, ...]
    issues: tuple[ProposalIssue, ...]


def _without_numbers(text: str) -> str:
    return normalize(re.sub(r"\d+(?:\.\d+)?[%٪]?", "", text)).strip(" ,،%")


def _content_key(text: str) -> tuple[str, ...]:
    """Compare amounts and words, retaining decimals, units, ranges and repeats."""
    return tuple(
        numbers_in(token)[0] if token[0].isdigit() else token
        for token in re.findall(
            r"\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?|[^\W\d_]+|[%٪]", normalize(text)
        )
    )


def _patient_key(value: object) -> object:
    if isinstance(value, str):
        return _content_key(value)
    if isinstance(value, tuple):
        return tuple(_patient_key(v) for v in value)
    return value


def _source_format(item: Item, source: str) -> Item:
    if isinstance(item, OrderCandidate):
        heard = heard_dose(item.drug, source)
        dose = item.dose or split_drug_dose(item.drug)[1]
        if heard and _content_key(heard) == _content_key(dose):
            return item.model_copy(update={"dose": heard})
    return item


def _preferred(first: Item, second: Item, source: str) -> Item:
    """A supported reading wins display; both readings still contribute numeric blocks."""
    if isinstance(first, OrderCandidate) and isinstance(second, OrderCandidate):
        supported = set(numbers_in(source))
        return min(
            (first, second),
            key=lambda o: (
                len(set(numbers_in(o.drug + " " + (o.dose or ""))) - supported),
                not bool(o.dose or split_drug_dose(o.drug)[1]),
            ),
        )
    return first


def _name(
    item: OrderCandidate | FactCandidate | MissionCandidate | str, source: str, ctx: Context
) -> str:
    if isinstance(item, OrderCandidate):
        spoken = split_drug_dose(item.drug)[0]
        resolved = resolve_name(spoken, "drug", source, item.name_latin, ctx)
        return normalize(resolved.latin or spoken)
    if isinstance(item, str):
        return _without_numbers(item)
    kind: NameKind = "test" if isinstance(item, MissionCandidate) else "finding"
    terms = resolve_fragments(item.text, kind, source, ctx)
    names = tuple(_without_numbers(r.latin or r.spoken) for r in terms)
    return ", ".join(names)


def _fields(
    item: OrderCandidate | FactCandidate | MissionCandidate | str, source: str, ctx: Context
) -> dict[str, object]:
    if isinstance(item, str):
        return {"text": _content_key(item)}
    if isinstance(item, OrderCandidate):
        _, suffix = split_drug_dose(item.drug)
        fields: dict[str, object] = {
            field: _content_key(str(getattr(item, field) or (suffix if field == "dose" else "")))
            for field in (
                "action",
                "dose",
                "frequency",
                "route",
                "timing",
                "duration",
                "effective_expression",
                "checkin_expression",
                "previous_drug",
                "previous_dose",
            )
        }
        if item.frequency and normalize(item.frequency) not in normalize(source):
            fields["frequency"] = ()
        return fields
    if isinstance(item, MissionCandidate):
        return {
            "text": (frozenset(_name(item, source, ctx).split(", ")), numbers_in(item.text)),
            "timing_expression": _content_key(item.timing_expression or ""),
        }
    return {"text": (_name(item, source, ctx), numbers_in(item.text)), "category": item.category}


def merge_candidates(
    first: DictationCandidate | None,
    second: DictationCandidate | None,
    source: str,
    ctx: Context | None = None,
) -> MergeResult | None:
    """None is a failed extraction. No successful reading is discarded by a peer's failure."""
    ctx = ctx or Context()
    if first is None and second is None:
        return None
    surviving = first or second
    assert surviving is not None
    if first is None:
        first, second = second, None
    assert first is not None
    from sanad.scribe.changes import combine_changes

    first = combine_changes(first, source, ctx)
    second = combine_changes(second, source, ctx) if second else None
    surviving = first
    single: list[str] = []
    issues: list[ProposalIssue] = []
    values: dict[str, object] = {}
    for family, attr in (
        ("order", "orders"),
        ("fact", "facts"),
        ("mission", "missions"),
        ("alert", "alerts"),
    ):
        left = (
            [_source_format(item, source) for item in getattr(first, attr)]
            if first is not None
            else []
        )
        right = (
            [_source_format(item, source) for item in getattr(second, attr)]
            if second is not None
            else []
        )
        used: set[int] = set()
        combined: list[tuple[Item, bool, tuple[ProposalIssue, ...]]] = []
        if family == "fact":
            values[attr] = tuple(left)
            for i, item in enumerate(left):
                if not any(
                    _fields(item, source, ctx) == _fields(other, source, ctx) for other in right
                ):
                    single.append(f"{family}:{i}")
            if family == "fact" and right:
                logger.info("scribe_secondary_facts_dropped count=%d", len(right))
            continue
        for item in left:
            key = _name(item, source, ctx)
            matches = [
                j
                for j, other in enumerate(right)
                if j not in used and _name(other, source, ctx) == key
            ]
            if not matches and isinstance(item, MissionCandidate):
                matches = [
                    j
                    for j, other in enumerate(right)
                    if j not in used
                    and isinstance(other, MissionCandidate)
                    and other.kind == item.kind
                ]
                if len(matches) != 1:
                    matches = []
            if not matches:
                combined.append((item, True, ()))
                continue
            j = matches[0]
            used.add(j)
            other = right[j]
            a, b = _fields(item, source, ctx), _fields(other, source, ctx)
            chosen = _preferred(item, other, source)
            differing = tuple(
                _conflict(item, other, field, chosen, source, ctx)
                for field in a
                if a[field] != b[field]
                and not (
                    field
                    in {"timing", "effective_expression", "checkin_expression", "timing_expression"}
                    and (not a[field] or not b[field])
                )
                and (
                    family != "mission"
                    or (
                        isinstance(item, MissionCandidate)
                        and item.kind == "TEST"
                        and field == "text"
                    )
                )
            )
            combined.append((chosen, False, differing))
        if family != "mission":
            combined.extend((item, True, ()) for j, item in enumerate(right) if j not in used)

        def position(row: tuple[Item, bool, tuple[ProposalIssue, ...]]) -> int:
            item = row[0]
            raw = (
                item
                if isinstance(item, str)
                else item.drug
                if isinstance(item, OrderCandidate)
                else item.text
            )
            at = normalize(source).find(normalize(raw))
            if at < 0 and isinstance(item, OrderCandidate):
                at = normalize(source).find(normalize(split_drug_dose(item.drug)[0]))
            return at if at >= 0 else len(source)

        combined.sort(key=position)
        values[attr] = tuple(item for item, _, _ in combined)
        for i, (_item, one, differing) in enumerate(combined):
            target = f"{family}:{i}"
            if one:
                single.append(target)
            issues.extend(issue.model_copy(update={"item": target}) for issue in differing)
    patient = surviving.patient.model_dump()
    if first and second:
        for field in ("name_as_spoken", "identifiers", "age", "sex"):
            left_value, right_value = getattr(first.patient, field), getattr(second.patient, field)
            # Absence is a single reading, never evidence of a different patient.
            patient[field] = left_value or right_value
            if left_value and right_value and _patient_key(left_value) != _patient_key(right_value):
                issues.append(
                    ProposalIssue(
                        item="patient",
                        field=field,
                        code="extraction_conflict",
                        question='سمعت بيانات المريض بقراءتين مختلفتين؛ تؤكد "'
                        + str(getattr(first.patient, field) or getattr(second.patient, field))
                        + '"؟',
                    )
                )
    values["patient"] = PatientCandidate.model_validate(patient)
    values["ambiguities"] = first.ambiguities
    result = surviving.model_copy(update=values)
    result._dropped_numbers = tuple(
        dict.fromkeys(
            (
                *first._dropped_numbers,
                *(
                    (
                        n
                        for n in extracted_numbers(
                            DictationCandidate(orders=second.orders, patient=second.patient)
                        )
                        if n not in extracted_numbers(result)
                    )
                    if second
                    else ()
                ),
                *(
                    n
                    for c in (first,)
                    if c
                    for n in extracted_numbers(c)
                    if n not in extracted_numbers(result)
                ),
            )
        )
    )
    result._single_source = tuple(single)
    result._malformed_items = first._malformed_items
    result._merge_issues = tuple(issues)
    result = fold_facts(result, source, ctx)
    return MergeResult(result, result._single_source, result._merge_issues)


def fold_facts(
    candidate: DictationCandidate, source: str, ctx: Context | None = None
) -> DictationCandidate:
    """Fold subsets and overlapping same-number ECG/echo readings in primary order."""
    ctx = ctx or Context()
    groups: list[tuple[FactCandidate, list[int]]] = []
    for index, fact in enumerate(candidate.facts):
        tokens = set(_content_key(_name(fact, source, ctx))) | set(numbers_in(fact.text))
        cue = _fact_cue(tokens)
        for pos, (other, origins) in enumerate(groups):
            other_tokens = set(_content_key(_name(other, source, ctx))) | set(
                numbers_in(other.text)
            )
            overlap = bool(
                cue
                and cue == _fact_cue(other_tokens)
                and set(numbers_in(fact.text)) & set(numbers_in(other.text))
            )
            if tokens <= other_tokens or other_tokens <= tokens or overlap:
                kept = (
                    fact
                    if (other_tokens < tokens or (overlap and len(fact.text) > len(other.text)))
                    else other
                )
                groups[pos] = (kept, [*origins, index])
                break
        else:
            groups.append((fact, [index]))
    if len(groups) == len(candidate.facts):
        return candidate
    result = candidate.model_copy(update={"facts": tuple(f for f, _ in groups)})
    remap_metadata(
        result,
        {
            f"fact:{old}": (f"fact:{new}",)
            for new, (_, origins) in enumerate(groups)
            for old in origins
        },
    )
    result._dropped_numbers = tuple(
        dict.fromkeys(
            (
                *candidate._dropped_numbers,
                *(n for n in extracted_numbers(candidate) if n not in extracted_numbers(result)),
            )
        )
    )
    logger.info("scribe_overlapping_facts_dropped count=%d", len(candidate.facts) - len(groups))
    return result


def _fact_cue(tokens: set[str]) -> str:
    if tokens & {"echo", "ef", "ejection", "ايكو", "فانكشن"}:
        return "echo"
    if tokens & {"ecg", "inversion", "انفرجين"}:
        return "ecg"
    return ""


def _conflict(
    first: Item, second: Item, field: str, chosen: Item, source: str, ctx: Context
) -> ProposalIssue:
    alternatives = tuple(
        dict.fromkeys(
            str(item if isinstance(item, str) else getattr(item, field) or "غير مذكور")
            for item in (first, second)
        )
    )
    quote = '" أو "'.join(alternatives)
    label = ""
    question = ""
    if isinstance(chosen, OrderCandidate):
        spoken = split_drug_dose(chosen.drug)[0]
        resolved = resolve_name(spoken, "drug", source, chosen.name_latin, ctx)
        label = f" لـ {resolved.latin or spoken}"
        if field == "dose" and resolved.entry and resolved.entry.fixed_combination_strengths:
            dose = chosen.dose or split_drug_dose(chosen.drug)[1]
            if numbers_in(dose) in {("560", "12.5"), ("516", "12.5")}:
                question = compound_question(dose, resolved.entry)
    return ProposalIssue(
        item="pending",
        field=field,
        code="extraction_conflict",
        question=question or f'سمعت "{quote}"{label}؛ تؤكد {_FIELD_LABELS.get(field, "البند")}؟',
        numbers=tuple(dict.fromkeys(numbers_in(quote))),
    )


def remap_metadata(candidate: DictationCandidate, targets: Mapping[str, tuple[str, ...]]) -> None:
    """Carry code-owned metadata through deterministic item removal/conversion."""
    candidate._single_source = tuple(
        dict.fromkeys(
            target
            for item in candidate._single_source
            for target in targets.get(item, (item,))
            if target != "all"
        )
    )
    candidate._merge_issues = tuple(
        dict.fromkeys(
            issue.model_copy(update={"item": target})
            for issue in candidate._merge_issues
            for target in targets.get(issue.item, (issue.item,))
        )
    )


_FIELD_LABELS = {
    "dose": "الجرعة",
    "action": "بداية ولا استمرار ولا إيقاف ولا تغيير",
    "frequency": "عدد المرات",
    "text": "البند",
    "timing_expression": "الموعد",
}
