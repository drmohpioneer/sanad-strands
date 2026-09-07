"""Verify spoken test identities and source-aligned fact terms."""

import logging
import re
from functools import partial

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
    dictionary,
    entry_for,
    latin_in_source,
    normalize,
    split_drug_dose,
)
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY

TERM_QUESTION = "المصطلحات اللي عليها (؟) اتكتبت من كلامك؛ لو حاجة غلط عدّلها، وإلا اضغط ✅"
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
    entries = (*service.vocabulary.entries(), *(e for e in dictionary() if e.kind == "drug"))
    result: dict[str, DrugMention] = {}
    for entry in entries:
        for spelling in (entry.latin, *entry.arabic_spellings, *entry.latin_spellings):
            if _contains(text, spelling):
                result.setdefault(
                    entry.generic,
                    DrugMention(spoken=spelling, name_latin=entry.latin, generic=entry.generic),
                )
    return tuple(result.values())


def _drug_gate(
    english: str,
    spoken: str,
    source: str,
    supplied: tuple[DrugMention, ...],
    service: DrugLookupService,
) -> bool:
    # Independently recognize known drug vocabulary; declarations cannot hide it.
    mentions = (*_drug_mentions(english, service), *supplied)
    for mention in mentions:
        canonical = mention.name_latin or mention.spoken
        if not _contains(english, canonical):
            continue
        spoken_mentions = (*_drug_mentions(spoken, service), *supplied)
        supported = next(
            (
                m
                for m in spoken_mentions
                if _contains(spoken, m.spoken)
                and _contains(source, m.spoken)
                and (
                    (
                        mention.generic
                        and m.generic
                        and generic_key(mention.generic) == generic_key(m.generic)
                    )
                    or normalize(m.name_latin or m.spoken) == normalize(canonical)
                )
            ),
            None,
        )
        if supported is None:
            return False
        resolved = service.resolve(supported.spoken, source, canonical, mention.generic)
        if resolved.latin is None or resolved.conflict:
            return False
    return True


def test_names(
    spoken: str,
    source: str,
    service: DrugLookupService,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Resolve only spoken terms. Model English is never an analyte authority."""
    entries = (*service.vocabulary.entries("term"), *(e for e in dictionary() if e.kind == "term"))
    aliases: dict[str, str] = {}
    for entry in entries:
        for name in (entry.latin, *entry.arabic_spellings, *entry.latin_spellings):
            aliases.setdefault(normalize(name), entry.latin)
    pattern = re.compile(
        r"(?<!\w)(?:و|وال|ال)?("
        + "|".join(re.escape(s) for s in sorted(aliases, key=len, reverse=True))
        + r")(?!\w)",
        re.I,
    )
    parts = re.split(r"[,،;؛]|\s+and\s+|\s+و\s*|\s*&\s*", spoken, flags=re.I)
    names: list[tuple[str, str]] = []

    def add(raw: str, latin: str) -> bool:
        if not _contains(source, raw) or service.contains_identity(raw):
            return False
        names.extend((raw, component.strip()) for component in latin.split(","))
        return True

    def fragment(raw: str) -> bool:
        raw = raw.strip(" ,،:؛;")
        raw = re.sub(
            r"^(?:(?:تحليل|تحاليل|فحص|اعمل|عايز|محتاج|tests?|labs?)(?:\s+|$))+", "", raw, flags=re.I
        )
        if not raw:
            return True
        learned = service.vocabulary.find(raw, "test")
        term = entry_for(raw, "term") if len(normalize(raw)) >= 4 else None
        latin = learned.latin if learned else term.latin if term else raw
        return bool((learned or term or latin_in_source(raw, source)) and add(raw, latin))

    for part in parts:
        part = part.strip()
        if not part:
            continue
        normalized = normalize(part)
        matches = list(pattern.finditer(normalized))
        if not matches:
            if not fragment(part):
                return "", ()
            continue
        cursor = 0
        for match in matches:
            if not fragment(normalized[cursor : match.start()]) or not add(
                match[0], aliases[match[1]]
            ):
                return "", ()
            cursor = match.end()
        if not fragment(normalized[cursor:]):
            return "", ()
    return ", ".join(dict.fromkeys(latin for _, latin in names)), tuple(names)


def prepare_clinical(
    candidate: DictationCandidate,
    source: str,
    service: DrugLookupService,
    readings: list[NameReading],
) -> tuple[DictationCandidate, tuple[ProposalIssue, ...]]:
    facts: list[FactCandidate] = []
    missions: list[MissionCandidate] = []
    orders, issues = list(candidate.orders), []
    for fact in candidate.facts:
        mentions = (*fact.drug_mentions, *_drug_mentions(fact.text, service))
        current = bool(re.search(r"واخد|بياخد|ماشي على|ماشى على|taking\b|on\s+", fact.text, re.I))
        if current and mentions:
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
                continue
        from sanad.scribe.terms import aligned_facts

        for prepared, fragments in aligned_facts(
            fact,
            candidate.patient.name_as_spoken,
            service,
            partial(_drug_gate, source=source, supplied=fact.drug_mentions, service=service),
            source,
        ):
            item = f"fact:{len(facts)}"
            facts.append(prepared)
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
    for i, mission in enumerate(candidate.missions):
        english = normalize_units(mission.clinical_en or "")
        if mission.kind != "TEST":
            missions.append(mission.model_copy(update={"clinical_en": None}))
            continue
        proposed_numbers_supported = set(numbers_in(english)) <= set(numbers_in(source))
        pairs: tuple[tuple[str, str], ...] = ()
        if mission.kind == "TEST":
            spoken = mission.text
            if mission.timing_expression:
                spoken = spoken.replace(mission.timing_expression, "").strip()
            english, pairs = test_names(spoken, source, service)
        valid = (
            proposed_numbers_supported
            and valid_english(english, source)
            and _drug_gate(english, mission.text, source, (), service)
        )
        if not valid:
            issues.append(
                ProposalIssue(
                    item=f"mission:{i}",
                    code="clinical_unclear",
                    blocked=False,
                    question=f'الصياغة الإنجليزية لـ "{mission.text}" محتاجة مراجعة؛ المقصود إيه؟',
                )
            )
        missions.append(mission.model_copy(update={"clinical_en": english if valid else None}))
        if valid and mission.kind == "TEST":
            for spoken, latin in pairs:
                entry = entry_for(latin, "term")
                spellings = (
                    (entry.latin, *entry.arabic_spellings, *entry.latin_spellings) if entry else ()
                )
                source_spelling = next(
                    (s for s in spellings if _contains(mission.text, s)),
                    None,
                )
                if source_spelling is None:
                    # A compound's components must not learn the same whole alias:
                    # the next memory read would collapse it to one analyte. Retain
                    # each component's literal bounded-fuzzy spelling when present.
                    source_spelling = next(
                        (
                            part
                            for part in spoken.split()
                            if entry
                            and len(normalize(part)) >= 4
                            and entry_for(part, "term") == entry
                        ),
                        spoken,
                    )
                readings.append(
                    NameReading(
                        item=f"mission:{i}",
                        kind="test",
                        spoken=source_spelling[:120],
                        latin=latin,
                        generic=entry.generic if entry else "",
                        verified=entry is not None,
                    )
                )
    facts = _drop_bare_facts(facts, readings, issues)
    result = candidate.model_copy(
        update={"facts": tuple(facts), "missions": tuple(missions), "orders": tuple(orders)}
    )
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
