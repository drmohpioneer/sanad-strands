"""Verify spoken test identities and source-aligned fact terms."""

import logging
import re
from typing import TYPE_CHECKING

from sanad.scribe.change_binding import SourcePartition

if TYPE_CHECKING:
    from sanad.scribe.proposal import Proposal

from sanad.domain.language import default_language
from sanad.media.numbers import numbers_in
from sanad.scribe.extract import (
    DictationCandidate,
    DrugMention,
    FactCandidate,
    MissionCandidate,
    OrderCandidate,
    ProposalIssue,
)
from sanad.scribe.lookup import DrugLookupService, generic_key
from sanad.scribe.names import (
    NameReading,
    entry_for,
    normalize,
    split_drug_dose,
)
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY

TERM_QUESTION = "الأسماء اللي بالعربي اتكتبت زي ما سمعتها؛ لو عايز تكتبها بالإنجليزي عدّلها"
logger = logging.getLogger(__name__)


def _drop_bare_facts(
    facts: list[FactCandidate], readings: list[NameReading], issues: list[ProposalIssue]
) -> list[FactCandidate]:
    """Remove repeated labels only after their other occurrence passed term alignment."""
    dropped: set[int] = set()
    for i, fact in enumerate(facts):
        bare = normalize(fact.text.strip(" ,،;؛:."))
        if not bare.isalpha() or fact.lab or fact.drug_mentions:
            continue
        for j, other in enumerate(facts):
            if i == j or j in dropped:
                continue
            # Equal bare facts retain their first occurrence, never erase each other.
            if normalize(other.text.strip(" ,،;؛:.")) == bare and j > i:
                continue
            if any(
                n.item == f"fact:{j}"
                and bare in {normalize(n.spoken), normalize(n.latin)}
                and any(normalize(t.spoken) == normalize(n.spoken) for t in other.terms)
                for n in readings
            ):
                dropped.add(i)
                break
    if not dropped:
        return facts
    remap = {
        f"fact:{old}": f"fact:{new}"
        for new, old in enumerate(i for i in range(len(facts)) if i not in dropped)
    }
    removed = {f"fact:{i}" for i in dropped}
    readings[:] = [
        n.model_copy(update={"item": remap.get(n.item, n.item)})
        for n in readings
        if n.item not in removed
    ]
    issues[:] = [
        issue.model_copy(update={"item": remap.get(issue.item, issue.item)})
        for issue in issues
        if issue.item not in removed
    ]
    logger.info("scribe_bare_fact_dropped count=%d", len(dropped))
    return [fact for i, fact in enumerate(facts) if i not in dropped]


def normalize_units(text: str) -> str:
    text = re.sub(r"%(?:\s*%)+", "%", text)
    return re.sub(
        r"\b(mmol/L|mEq/L|mg/dL|g/dL|mmHg|mcg|mg|ml|bpm|kg|cm|mm|g|L)(?:\s+\1\b)+",
        r"\1",
        text,
        flags=re.I,
    )


_QUALIFIERS = frozenset(
    "grade class stage mild moderate severe acute chronic stable unstable significant "
    "normal abnormal reduced preserved improved worsening new recurrent null none".split()
)


def supported_qualifiers(english: str, source: str) -> bool:
    """A bounded lexical guard, not an inference about severity or clinical meaning."""
    tokens = set(re.findall(r"\b[A-Za-z]+\b", english.casefold()))
    source_tokens = set(re.findall(r"\b[A-Za-z]+\b", source.casefold()))
    # Includes both uppercase and lowercase Roman grades; do not convert them to digits.
    roman = {t for t in tokens if re.fullmatch(r"[ivxlcdm]+", t)}
    return (tokens & _QUALIFIERS | roman) <= source_tokens


def valid_english(text: str | None, source: str) -> bool:
    return bool(
        text
        and len(text) <= DRAFT_SCRIBE_POLICY.clinical_en_max_chars
        and re.fullmatch(r"[A-Za-z0-9 %/,]+(?:\.[0-9]+[A-Za-z0-9 %/,]*)*", text)
        and set(numbers_in(text)) <= set(numbers_in(source))
    )


def _contains(text: str, name: str) -> bool:
    return bool(re.search(r"(?<!\w)و?" + re.escape(normalize(name)) + r"(?!\w)", normalize(text)))


def _drug_mentions(text: str, service: DrugLookupService) -> tuple[DrugMention, ...]:
    from sanad.scribe.resolver import drug_mentions

    return drug_mentions(text, service)


def test_names(
    spoken: str, source: str, service: DrugLookupService
) -> tuple[str, tuple[tuple[str, str], ...]]:
    from sanad.scribe.resolver import context, resolve_tests

    names, _ = resolve_tests(spoken, source, context(service))
    pairs = tuple(
        (r.spoken, component.strip())
        for r in names
        for component in (r.latin or r.spoken).split(",")
    )
    return ", ".join(dict.fromkeys(latin for _, latin in pairs)), pairs


def prepare_clinical(
    candidate: DictationCandidate,
    source: str,
    service: DrugLookupService,
    readings: list[NameReading],
    *,
    language: str = default_language,
    clarified_tests: frozenset[str] = frozenset(),
    partition: SourcePartition | None = None,
    previous: "Proposal | None" = None,
) -> tuple[DictationCandidate, tuple[ProposalIssue, ...]]:
    from sanad.scribe.alerts import prepare_alerts

    candidate = prepare_alerts(candidate, source)
    facts: list[FactCandidate] = []
    missions: list[MissionCandidate] = []
    orders, issues = list(candidate.orders), []
    origins: dict[str, list[FactCandidate]] = {}
    converted: dict[str, tuple[str, ...]] = {}
    for original_index, fact in enumerate(candidate.facts):
        original_item = f"fact:{original_index}"
        origins[original_item] = []
        mentions = (*fact.drug_mentions, *_drug_mentions(fact.text, service))
        current = bool(re.search(r"واخد|بياخد|ماشي على|ماشى على|taking\b|on\s+", fact.text, re.I))
        if current and mentions:
            first_order = len(orders)
            for mention in dict.fromkeys(mentions):
                if not _contains(fact.text, mention.spoken) or not _contains(
                    source, mention.spoken
                ):
                    continue
                if any(
                    normalize(split_drug_dose(o.drug)[0]) == normalize(mention.spoken)
                    or normalize(o.name_latin or o.drug)
                    == normalize(mention.name_latin or mention.spoken)
                    or (
                        (known := entry_for(split_drug_dose(o.drug)[0])) is not None
                        and generic_key(known.generic) == generic_key(mention.generic or "")
                    )
                    for o in orders
                ):
                    continue
                index = len(orders)
                orders.append(
                    OrderCandidate(
                        action="continue",
                        drug=mention.spoken,
                        name_latin=mention.name_latin,
                        generic=mention.generic,
                    )
                )
                issues.append(
                    ProposalIssue(
                        item=f"order:{index}",
                        code="fact_medication",
                        blocked=False,
                        question=f'سمعت "{mention.spoken}" كدوا حالي؛ أسجله كاستمرار؟',
                    )
                )
            if any(
                _contains(fact.text, m.spoken) and _contains(source, m.spoken) for m in mentions
            ):
                converted[original_item] = tuple(
                    f"order:{i}" for i in range(first_order, len(orders))
                ) or ("all",)
                continue
        from sanad.scribe.terms import aligned_facts

        for prepared, fragments in aligned_facts(
            fact,
            candidate.patient.name_as_spoken,
            service,
            source,
        ):
            item = f"fact:{len(facts)}"
            if (
                language == "en"
                and fragments
                and all(n.verified and n.latin.isascii() for n in fragments)
            ):
                prepared = prepared.model_copy(
                    update={"clinical_en": ", ".join(n.latin for n in fragments)}
                )
            facts.append(prepared)
            origins[original_item].append(prepared)
            readings.extend(n.model_copy(update={"item": item}) for n in fragments)
            if any(not fragment.verified for fragment in fragments):
                issues.append(
                    ProposalIssue(
                        item=item,
                        code="clinical_unclear",
                        blocked=False,
                        question=TERM_QUESTION,
                    )
                )
    from sanad.scribe.resolver import context, resolve_tests

    for i, mission in enumerate(candidate.missions):
        missions.append(mission.model_copy(update={"clinical_en": None}))
        if mission.kind == "TASK":
            from sanad.scribe.monitoring import task_instruction, task_request

            if task_request(mission.text):
                missions[-1] = missions[-1].model_copy(
                    update={"clinical_en": task_instruction(mission.text)}
                )
        if mission.kind != "TEST":
            continue
        spoken = mission.text
        if mission.timing_expression:
            spoken = spoken.replace(mission.timing_expression, "").strip()
        resolved_tests, unresolved = resolve_tests(
            spoken, source, context(service), clarified=f"mission:{i}" in clarified_tests
        )
        if resolved_tests and all(r.latin for r in resolved_tests):
            missions[-1] = missions[-1].model_copy(
                update={
                    "clinical_en": ", ".join(
                        dict.fromkeys(
                            part.strip()
                            for r in resolved_tests
                            for part in (r.latin or "").split(",")
                        )
                    )
                }
            )
        for fragment in unresolved:
            issues.append(
                ProposalIssue(
                    item=f"mission:{i}",
                    field="analyte",
                    code="clinical_unclear",
                    question=(
                        f'I heard "{fragment}" for a test; which test did you mean?'
                        if language == "en" and fragment
                        else "Which test did you mean?"
                        if language == "en"
                        else f'سمعت "{fragment}" كتحليل، قصدك إيه؟'
                        if fragment
                        else "قصدك تحليل إيه؟"
                    ),
                )
            )
        for resolved in resolved_tests:
            for latin in (resolved.latin or resolved.spoken).split(","):
                latin = latin.strip()
                entry = entry_for(latin, "term") if resolved.latin else None
                alias = resolved.spoken
                if entry and "," in (resolved.latin or ""):
                    alias = next(
                        (
                            s
                            for s in (*entry.arabic_spellings, *entry.latin_spellings, entry.latin)
                            if _contains(spoken, s)
                        ),
                        alias,
                    )
                readings.append(
                    NameReading(
                        item=f"mission:{i}",
                        kind="test",
                        spoken=alias[:120],
                        latin=latin,
                        generic=entry.generic if entry else "",
                        verified=resolved.latin is not None,
                        learnable=resolved.latin is not None
                        and not service.contains_identity(alias),
                        source=resolved.legacy().source,
                    )
                )
                if resolved.latin is None:
                    issues.append(
                        ProposalIssue(
                            item=f"mission:{i}",
                            code="clinical_unclear",
                            blocked=False,
                            question=TERM_QUESTION,
                        )
                    )
    facts = _drop_bare_facts(facts, readings, issues)
    result = candidate.model_copy(
        update={"facts": tuple(facts), "missions": tuple(missions), "orders": tuple(orders)}
    )
    from sanad.scribe.merge import remap_metadata

    remap_metadata(
        result,
        {
            item: converted.get(item)
            or tuple(f"fact:{i}" for i, fact in enumerate(facts) if fact in prepared)
            or ("all",)
            for item, prepared in origins.items()
        },
    )
    from sanad.scribe.changes import drop_bare_continues

    result, targets = drop_bare_continues(
        result, source, context(service), partition=partition, previous=previous
    )
    issues = [
        issue.model_copy(update={"item": target})
        for issue in issues
        for target in targets.get(issue.item, (issue.item,))
    ]
    # Preserve numbers from a converted medication fact for the existing coverage guard.
    from sanad.scribe.extract import extracted_numbers

    represented = set(extracted_numbers(result))
    result._dropped_numbers = tuple(
        dict.fromkeys(
            (
                *candidate._dropped_numbers,
                *(
                    n
                    for n in extracted_numbers(candidate)
                    if n not in represented and n in numbers_in(source)
                ),
            )
        )
    )
    return result, tuple(issues)
