"""Code-owned claim evidence for dictation cards. No provider calls or clinical inference."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from sanad.domain.boundaries import _BoundaryValue
from sanad.media.numbers import numbers_in
from sanad.scribe.change_binding import (
    bind_change,
    complete_quantity,
    mentions,
    partition_for,
    untouched,
)
from sanad.scribe.names import normalize as normalize

if TYPE_CHECKING:
    from sanad.scribe.extract import DictationCandidate, OrderCandidate, ProposalIssue
    from sanad.scribe.proposal import Proposal
    from sanad.scribe.resolver import Context

type Origin = Literal[
    "transcript_span",
    "vocabulary_alias",
    "authorized_correction",
    "stored_prior_order",
    "code_computed",
]


class FieldEvidence(_BoundaryValue):
    item: str
    field: str
    origin: Origin
    source_ref: str
    offsets: tuple[tuple[int, int], ...] = ()
    transformation: str
    value: str = Field(repr=False)
    correction_version: int | None = None
    correction_id: str | None = None


@dataclass(frozen=True)
class Claim:
    item: str
    field: str
    value: str


_TOKEN = re.compile(r"\d+(?:\.\d+)?|[^\W\d_]+|[%/+-]", re.UNICODE)
_CONNECTORS = {"و", "في", "فى", "in", "and", "of", "the"}
_UNITS = {"مج": "mg", "مجم": "mg", "مليجرام": "mg", "ميكروجرام": "mcg"}
_BOUNDARY = re.compile(r"(?<!\d)\.(?!\d)|[;؛\n]|[.!?](?=\s+[A-Z])")
_ACTION = re.compile(
    r"(?<!\w)(?:و)?(?:"
    r"(?P<start>start(?:ed)?|add(?:ed)?|ابدا|ضفت|زودته|هيبدا)|"
    r"(?P<stop>stop(?:ped)?|hold|discontinue|وقف(?:ت)?|بطل)|"
    r"(?P<change>increase|decrease|upgrade|change|switch|زود(?:ت)?|قلل(?:ت)?|غير(?:ت)?)|"
    r"(?P<continue>continue|taking|on|ماشي علي|ماشى علي|بياخد|واخد|خلي|استمر|كمل)"
    r")(?!\w)",
    re.I,
)
_NEGATIVE = re.compile(
    r"\b(?:no|not|never|without|denies|avoid|cannot|won['’]t|"
    r"(?:do|does|did|is|are|was|were|can|should|must)n['’]?t)\b|"
    r"(?<!\w)(?:لا|مش|بدون|مافيش|مفيش)(?!\w)",
    re.I,
)
_OTHER = re.compile(
    r"\b(?:mother|father|sister|brother|wife|husband|family)\b|(?<!\w)(?:امه|ابوه|والدته|والده|اخوه|اخته)(?!\w)",
    re.I,
)
_PAST = re.compile(
    r"\b(?:previously|formerly|used to|history of|was|were|had been)\b|"
    r"(?<!\w)(?:كان|زمان|قبل كده)(?!\w)",
    re.I,
)


@lru_cache(maxsize=64)
def _tokens(text: str) -> tuple[tuple[str, int, int], ...]:
    from sanad.scribe.terms import _normalized

    normalized, offsets = _normalized(text)
    return tuple(
        (_UNITS.get(m[0], m[0]), offsets[m.start()], offsets[m.end() - 1] + 1)
        for m in _TOKEN.finditer(normalized)
        if m[0] not in _CONNECTORS
    )


def grounded(value: str, source: str) -> tuple[tuple[int, int], ...]:
    """Locate complete words with original offsets; normalization never changes polarity."""
    needle = tuple(t[0] for t in _tokens(value))
    haystack = _tokens(source)
    if not needle:
        return ()
    return tuple(
        (haystack[i][1], haystack[i + len(needle) - 1][2])
        for i in range(len(haystack) - len(needle) + 1)
        if all(
            _same_word(a, b[0]) for a, b in zip(needle, haystack[i : i + len(needle)], strict=True)
        )
    )


def _same_word(left: str, right: str) -> bool:
    if left == right:
        return True
    if not re.search(r"[\u0621-\u064a]", left + right):
        return False

    def forms(word: str) -> set[str]:
        return {
            word,
            *(
                word[len(p) :]
                for p in ("وال", "ال", "و")
                if word.startswith(p) and len(word) > len(p) + 1
            ),
        }

    return bool(forms(left) & forms(right))


def clause(source: str, start: int, end: int) -> tuple[int, int]:
    left, right = 0, len(source)
    for boundary in _BOUNDARY.finditer(source):
        if boundary.end() <= start:
            left = boundary.end()
        elif boundary.start() >= end:
            right = boundary.start()
            break
    return left, right


def _claim_context(value: str, source: str, span: tuple[int, int]) -> bool:
    start, end = span
    left, _ = clause(source, start, end)
    prefix = normalize(source[left:start])
    # A comma starts a new assertion, except a family/negation subject governing a list.
    prefix = re.split(r"[,،]", prefix)[-1]
    text = normalize(value)
    return not (
        (_NEGATIVE.search(prefix) and not _NEGATIVE.search(text))
        or (_OTHER.search(prefix) and not _OTHER.search(text))
    )


def _aliases(name: str, kind: str, ctx: Context) -> tuple[str, ...]:
    from sanad.scribe.names import dictionary

    aliases = [name]
    for entry in dictionary():
        if entry.kind == ("drug" if kind == "drug" else "term") and normalize(
            entry.latin
        ) == normalize(name):
            aliases.extend((*entry.arabic_spellings, *entry.latin_spellings))
    if ctx.vocabulary:
        for rows in ctx.vocabulary.rows:
            for row in rows:
                if row.kind == kind and normalize(row.latin) == normalize(name):
                    aliases.extend(row.spoken_forms)
    return tuple(dict.fromkeys(aliases))


def name_spans(name: str, source: str, kind: str, ctx: Context) -> tuple[tuple[int, int], ...]:
    """Drug brands use their own aliases, never a shared generic's other brands."""
    spans = {span for alias in _aliases(name, kind, ctx) for span in grounded(alias, source)}
    if kind == "drug":
        # A shorter brand is not an occurrence inside a longer explicitly named brand.
        from sanad.scribe.names import dictionary

        longer = [
            e.latin
            for e in dictionary()
            if e.kind == "drug" and normalize(e.latin).startswith(normalize(name) + " ")
        ]
        spans = {
            s
            for s in spans
            if not any(t[0] == s[0] and t[1] > s[1] for n in longer for t in grounded(n, source))
        }
    return tuple(sorted(spans))


def _action_at(source: str, span: tuple[int, int]) -> tuple[str, tuple[int, int], bool] | None:
    from sanad.scribe.terms import _normalized

    left, right = clause(source, *span)
    normalized, offsets = _normalized(source[left:right])
    matches = [m for m in _ACTION.finditer(normalized) if left + offsets[m.start()] <= span[0]]
    if not matches:
        return None
    action = matches[-1]
    begin = left + offsets[action.start()]
    end = left + offsets[action.end() - 1] + 1
    prefix = normalized[max(0, action.start() - 25) : action.start()]
    prefix = re.split(r"[,،]|\band\b", prefix)[-1]
    between = normalize(source[end : span[0]])
    negative = bool(_NEGATIVE.search(prefix) or _NEGATIVE.search(between) or _OTHER.search(prefix))
    historical = bool(_PAST.search(prefix))
    return str(action.lastgroup), (begin, end), negative or historical


def _cited_action(
    order: OrderCandidate, source: str, drug_span: tuple[int, int]
) -> tuple[tuple[int, int], bool] | None:
    left, right = clause(source, *drug_span)
    spans = [
        span
        for span in grounded(order.action_quote or "", source)
        if (left <= span[0] and span[1] <= right)
        or (span[0] < drug_span[1] and drug_span[0] < span[1])
    ]
    if not spans:
        return None
    citation = min(
        spans,
        key=lambda span: (max(0, drug_span[0] - span[1], span[0] - drug_span[1]), span[0]),
    )
    earlier = min(citation[0], drug_span[0])
    prefix = normalize(source[left:earlier])[-25:]
    prefix = re.split(r"[,،]|\band\b", prefix)[-1]
    between = (
        source[citation[1] : drug_span[0]]
        if citation[1] <= drug_span[0]
        else (source[drug_span[1] : citation[0]] if drug_span[1] <= citation[0] else "")
    )
    window = " ".join((prefix, normalize(source[citation[0] : citation[1]]), normalize(between)))
    return citation, any(pattern.search(window) for pattern in (_NEGATIVE, _OTHER, _PAST))


def _instruction_span(
    order: OrderCandidate, source: str, ctx: Context, spoken: str
) -> tuple[int, int] | None:
    spans = name_spans(order.drug, source, "drug", ctx) or grounded(spoken, source)
    accepted = [
        span
        for span in spans
        if (
            bool((cited := _cited_action(order, source, span)) and not cited[1])
            if order.action_quote is not None
            else bool(
                (action := _action_at(source, span)) and not action[2] and action[0] == order.action
            )
        )
    ]
    # The last explicit correction controls the current instruction for that name.
    if accepted:
        latest = max(spans)
        later = _action_at(source, latest)
        # Commas share a clause: a stale citation can verify at both mentions.
        # Still reject an explicit later correction after an earlier drug span.
        if (
            later
            and (later[2] or later[0] != order.action)
            and (
                order.action_quote is None
                or latest > accepted[-1]
                or any(span[1] <= later[1][0] for span in spans if span < latest)
            )
        ):
            return None
        if order.action == "change":
            _, end = clause(source, *accepted[-1])
            tail = source[accepted[-1][1] : end]
            if re.match(r"\s*(?:\d[\d/ .]*\s*)?\bto\s+(?:be\s+)?[A-Za-z]", tail, re.I):
                # A named replacement is the target; the left-hand drug is history.
                return None
        return accepted[-1]
    return None


def _dose_span(
    value: str,
    source: str,
    drug: tuple[int, int],
    all_drugs: tuple[tuple[int, int], ...],
    *,
    dose: bool = False,
) -> tuple[int, int] | None:
    left, right = clause(source, *drug)
    # A new instruction or another drug ends this drug's local value slot.
    next_drug = min((a for a, _ in all_drugs if a >= drug[1]), default=right)
    end = min(right, next_drug)
    for span in reversed(grounded(value, source)):
        if dose and numbers_in(value) and not complete_quantity(value, source, span):
            continue
        if drug[1] <= span[0] and span[1] <= end:
            between = normalize(source[drug[1] : span[0]])
            between = re.split(r"[,،]\s*(?:actually|sorry|rather)|(?:اقصد|قصدي)", between)[-1]
            if re.search(r"\band\b|[,،;؛]|(?<!\w)و(?=\w)", between):
                continue
            if re.search(
                r"\b(?:age|aged|years?|ef|pressure|glucose|creatinine)\b|عمر|سنه", between
            ):
                continue
            # An explicitly superseded dose cannot borrow support from earlier words.
            following = normalize(source[span[1] : end])
            if re.search(
                r"\b(?:actually|instead|rather|sorry|now|to)\b|اقصد|قصدي|بدل", following
            ) and numbers_in(following):
                continue
            return span
        if left <= span[0] and span[1] <= drug[0]:
            between = normalize(source[span[1] : drug[0]])
            if re.fullmatch(r"\s*(?:of|من)?\s*", between):
                return span
    # Sharing requires the explicit distributive word; mere co-occurrence is insufficient.
    for match in re.finditer(r"\beach\b|لكل", source[left:right], re.I):
        at = left + match.start()
        for span in grounded(value, source[left:at]):
            global_span = (left + span[0], left + span[1])
            if not source[global_span[1] : at].strip(" ,،") and (
                not dose or complete_quantity(value, source, global_span)
            ):
                return global_span
    return None


def inventory(proposal: Proposal) -> tuple[Claim, ...]:
    """Every clinical field has an explicit disposition, including future schema additions."""
    c = proposal.candidate
    claims: list[Claim] = []
    excluded = {
        "action_quote",
        "name_latin",
        "generic",
        "clinical_en",
        "clinical_kind",
        "terms",
        "drug_mentions",
        "lab",
    }
    for family, values in (("order", c.orders), ("fact", c.facts), ("mission", c.missions)):
        for i, value in enumerate(values):
            for field, raw in value.model_dump().items():
                if field not in excluded and isinstance(raw, str) and raw:
                    claims.append(Claim(f"{family}:{i}", field, raw))
    for field, value in c.patient.model_dump().items():
        for i, part in enumerate(value if isinstance(value, tuple) else (value,)):
            if isinstance(part, str) and part:
                claims.append(
                    Claim("patient", f"{field}:{i}" if field == "identifiers" else field, part)
                )
    claims.extend(Claim(f"alert:{i}", "text", a) for i, a in enumerate(c.alerts))
    claims.extend(Claim(n.item, f"name:{i}", n.latin) for i, n in enumerate(proposal.names))
    for i, mission in enumerate(c.missions):
        if mission.kind == "TASK" and mission.clinical_en:
            claims.append(Claim(f"mission:{i}", "compiled_text", mission.clinical_en))
        if mission.kind == "MONITOR":
            from sanad.scribe.monitoring import card_line

            for language in ("en", "ar"):
                claims.append(
                    Claim(
                        f"mission:{i}",
                        "schedule:" + language,
                        card_line(mission.text, proposal.created_at, proposal.timezone, language),
                    )
                )
    for i, change in enumerate(proposal.amendments):
        if change.old:
            for field in ("drug", "dose", "frequency", "route", "timing", "duration"):
                if value := getattr(change.old, field):
                    claims.append(Claim(change.item, f"prior:{i}:{field}", value))
    claims.extend(Claim(t.item, "deadline", t.resolved.model_dump_json()) for t in proposal.timings)
    return tuple(claims)


def _fingerprint(proposal: Proposal) -> str:
    body = {
        "candidate": proposal.candidate.model_dump(
            mode="json", exclude={"orders": {"__all__": {"action_quote"}}}
        ),
        "claims": [(c.item, c.field, c.value) for c in inventory(proposal)],
        "source": proposal.source_text,
        "source_partition": proposal.source_partition.model_dump(mode="json")
        if proposal.source_partition
        else None,
        "receipt": proposal.source_receipt_id,
        "scope": proposal.scope.model_dump(mode="json"),
        "versions": [v.model_dump(mode="json") for v in proposal.base_versions],
        "names": [n.model_dump(mode="json") for n in proposal.names],
        "amendments": [
            a.model_dump(mode="json", exclude={"old": {"action_quote"}, "new": {"action_quote"}})
            for a in proposal.amendments
        ],
        "timezone": proposal.timezone,
        "created_at": proposal.created_at.isoformat(),
        "selected_patient": proposal.selected_patient_id,
        "selected_display_name": proposal.selected_display_name,
        "choices": [
            choice.model_dump(mode="json", exclude={"headline", "score"})
            for choice in proposal.choices
        ],
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _evidence(
    claim: Claim,
    proposal: Proposal,
    origin: Origin,
    offsets: tuple[tuple[int, int], ...],
    transformation: str,
    ref: str | None = None,
) -> FieldEvidence:
    return FieldEvidence(
        item=claim.item,
        field=claim.field,
        value=claim.value,
        origin=origin,
        offsets=offsets,
        transformation=transformation,
        source_ref=ref or proposal.source_receipt_id,
    )


def _safe_alias(value: str, spoken: str) -> bool:
    # This legacy alias adds a finding absent from its spoken phrase. A seed entry
    # is spelling evidence, never permission to add a clinical interpretation.
    return not (
        "hypokinesia" in value.casefold()
        and not re.search(r"hypokines|هيبوكين|هيبوكاين|هايبوكين|هايبوقنيز", spoken, re.I)
    )


def _record(
    claim: Claim,
    proposal: Proposal,
    ctx: Context,
    previous: Proposal | None,
    drugs: dict[str, tuple[int, int] | None],
    all_drugs: tuple[tuple[int, int], ...],
) -> FieldEvidence | None:
    from sanad.scribe.resolver import resolve_name, source_spans

    source = proposal.source_text
    span = grounded(claim.value, source)
    if claim.item.startswith("fact:") and claim.field == "text":
        from sanad.scribe.amend import history_source

        if history_source(proposal, claim.value):
            return _evidence(claim, proposal, "code_computed", (), "home_medication_instruction")
    if claim.field == "compiled_text" or claim.field.startswith("schedule:"):
        from sanad.scribe.monitoring import compile_schedule, task_instruction

        mission = proposal.candidate.missions[int(claim.item.split(":")[1])]
        if grounded(mission.text, source) and (
            (claim.field == "compiled_text" and task_instruction(mission.text) == claim.value)
            or (claim.field.startswith("schedule:") and compile_schedule(mission.text))
        ):
            return _evidence(
                claim,
                proposal,
                "code_computed",
                grounded(mission.text, source),
                "monitor_request_compiler",
            )
        return None
    if (
        previous
        and claim.item.startswith("order:")
        and not claim.field.startswith(("name:", "prior:"))
        and claim.field not in {"deadline", "previous_drug", "previous_dose"}
    ):
        handled, correction = _correction_record(claim, proposal, previous, ctx)
        if handled:
            return correction
    if claim.field == "deadline":
        return _evidence(
            claim, proposal, "code_computed", (), "resolved_timing", "timing:" + claim.item
        )
    if claim.field.startswith("prior:"):
        _, prior_index, field = claim.field.split(":")
        change = proposal.amendments[int(prior_index)]
        if change.head_version and proposal.selected_patient_id:
            return _evidence(
                claim,
                proposal,
                "stored_prior_order",
                (),
                "scoped_order_version",
                _prior_ref(proposal, claim.item),
            )
        binding = bind_change(change.new, source, partition_for(proposal), ctx, claim.item)
        if not binding:
            return None
        supplier = (
            binding.previous_name
            if field == "drug"
            else binding.previous_dose
            if field == "dose"
            else None
        )
        if supplier:
            return _evidence(
                claim,
                proposal,
                "vocabulary_alias" if field == "drug" else "transcript_span",
                (supplier,),
                "bound_previous_instruction",
            ).model_copy(
                update={
                    "correction_id": binding.correction_id,
                    "correction_version": binding.correction_version,
                }
            )
        return None
    if claim.field.startswith("name:"):
        reading = proposal.names[int(claim.field.split(":")[1])]
        anchors = grounded(reading.spoken, source)
        if not anchors:
            anchors = source_spans(reading.latin, source, reading.kind, ctx)
        if not anchors or not _safe_alias(reading.latin, reading.spoken):
            return None
        if reading.kind == "finding":
            parent = proposal.candidate.facts[int(claim.item.split(":")[1])]
            if not any(
                _claim_context(parent.text, source, s) for s in grounded(parent.text, source)
            ):
                return None
        if (
            reading.latin != reading.spoken
            and not reading.verified
            and resolve_name(reading.spoken, reading.kind, source, reading.latin, ctx).latin
            != reading.latin
        ):
            return None
        return _evidence(
            claim,
            proposal,
            "vocabulary_alias" if reading.latin != reading.spoken else "transcript_span",
            (anchors[-1],),
            "name_resolver:"
            + resolve_name(reading.spoken, reading.kind, source, reading.latin, ctx).tier,
        )
    if claim.item.startswith("order:"):
        index = int(claim.item.split(":")[1])
        order = proposal.candidate.orders[index]
        drug = drugs[claim.item]
        if claim.field == "drug":
            if _ACTION.search(normalize(claim.value)) or len(claim.value.split()) > 6:
                return None
            drug_reading = next(
                (n for n in proposal.names if n.item == claim.item and n.kind == "drug"), None
            )
            anchors = name_spans(claim.value, source, "drug", ctx) or grounded(
                drug_reading.spoken if drug_reading else claim.value, source
            )
            if anchors:
                return _evidence(
                    claim,
                    proposal,
                    "transcript_span" if span else "vocabulary_alias",
                    (drug or anchors[-1],),
                    "drug_name",
                )
            return None
        if claim.field == "action":
            if order.action_quote is not None:
                cited = _cited_action(order, source, drug) if drug else None
                if cited and not cited[1] and drug:
                    return _evidence(
                        claim, proposal, "transcript_span", (cited[0], drug), "instruction_clause"
                    )
                return None
            if drug and (action := _action_at(source, drug)):
                return _evidence(
                    claim, proposal, "transcript_span", (action[1], drug), "instruction_clause"
                )
            # Amend reconciliation can turn a spoken start/continue into change.
            old = next(
                (a for a in proposal.amendments if a.item == claim.item and a.head_version), None
            )
            if old and any(
                (old_action := _action_at(source, s)) and not old_action[2]
                for s in name_spans(order.drug, source, "drug", ctx)
            ):
                return _evidence(
                    claim, proposal, "code_computed", (), "existing_order_reconciliation"
                )
        elif claim.field in {
            "dose",
            "frequency",
            "route",
            "timing",
            "duration",
            "effective_expression",
            "checkin_expression",
        }:
            if drug and (
                attached := _dose_span(
                    claim.value,
                    source[: partition_for(proposal).original_end],
                    drug,
                    all_drugs,
                    dose=claim.field == "dose",
                )
            ):
                return _evidence(
                    claim, proposal, "transcript_span", (drug, attached), "instruction_attachment"
                )
            old = next(
                (
                    a
                    for a in proposal.amendments
                    if a.item == claim.item and a.head_version and a.old
                ),
                None,
            )
            if (
                old
                and old.old
                and claim.field in {"dose", "frequency", "route", "timing", "duration"}
                and _unchanged_field(proposal, claim)
                and getattr(old.old, claim.field) == claim.value
            ):
                return _evidence(
                    claim,
                    proposal,
                    "stored_prior_order",
                    (),
                    "unchanged_prior_field",
                    _prior_ref(proposal, claim.item),
                )
        elif claim.field in {"previous_drug", "previous_dose"}:
            binding = bind_change(order, source, partition_for(proposal), ctx, claim.item)
            if binding and binding.previous_dose:
                return _evidence(
                    claim,
                    proposal,
                    "code_computed",
                    binding.offsets,
                    "validated_previous_instruction",
                ).model_copy(
                    update={
                        "correction_id": binding.correction_id,
                        "correction_version": binding.correction_version,
                    }
                )
        return None
    if claim.field in {"kind", "category"}:
        return _evidence(claim, proposal, "code_computed", (), "candidate_classification")
    if claim.item == "patient" and claim.field == "sex":
        sex_aliases = ("male", "ذكر") if claim.value == "male" else ("female", "انثى", "أنثى")
        for alias in sex_aliases:
            if anchors := grounded(alias, source):
                return _evidence(claim, proposal, "vocabulary_alias", (anchors[-1],), "sex_label")
        return None
    if span:
        if claim.item.startswith(("fact:", "alert:", "mission:")):
            span = tuple(s for s in span if _claim_context(claim.value, source, s))
        if span:
            return _evidence(claim, proposal, "transcript_span", (span[-1],), "normalized_words")
    if claim.item.startswith("alert:"):
        words = claim.value.split()
        for position in range(1, len(words)):
            conditional = " ".join((*words[:position], "لو", *words[position:]))
            if anchors := grounded(conditional, source):
                return _evidence(
                    claim, proposal, "vocabulary_alias", (anchors[-1],), "alert_condition"
                )
    if claim.item.startswith("mission:"):
        mission = proposal.candidate.missions[int(claim.item.split(":")[1])]
        grammar = {
            "حضور زيارة متابعة": "يحضر زيارة متابعة",
            "الإبلاغ عن الاتصال بالعيادة": "يبلغنا إنه اتصل بالعيادة",
        }
        if claim.field == "text" and claim.value in grammar:
            if anchors := grounded(grammar[claim.value], source):
                return _evidence(
                    claim, proposal, "vocabulary_alias", (anchors[-1],), "request_grammar"
                )
        if mission.kind == "TEST" and claim.field == "text":
            # The resolver has per-analyte evidence; the original model phrase is
            # never the rendered TEST value or confirmation's analyte source.
            return _evidence(claim, proposal, "code_computed", (), "resolved_test_list")
    return None


def _correction_record(
    claim: Claim, proposal: Proposal, previous: Proposal, ctx: Context
) -> tuple[bool, FieldEvidence | None]:
    partition = partition_for(proposal)
    if not partition.valid(proposal.source_text) or not partition.corrections:
        return False, None
    extent = partition.corrections[-1]
    if extent.proposal_id != previous.id or extent.proposal_version != previous.version:
        return True, None
    index = int(claim.item.split(":")[1])
    if index >= len(previous.candidate.orders):
        offsets = _reply_instruction_offsets(claim, proposal, extent.start, extent.end)
        if not offsets:
            return True, None
        record = _evidence(
            claim, proposal, "authorized_correction", offsets, "reply_instruction:" + claim.item
        ).model_copy(
            update={
                "correction_id": extent.proposal_id,
                "correction_version": extent.proposal_version,
            }
        )
        return True, record if _valid_correction(record, claim, proposal) else None
    prior_order = previous.candidate.orders[index]
    if not hasattr(prior_order, claim.field):
        return False, None
    offset = extent.start
    reply = proposal.source_text[extent.start : extent.end]
    answer = dict(extent.answers).get(claim.item, "")
    old_value = getattr(prior_order, claim.field)
    if claim.field == "action" and answer:
        actions = list(_ACTION.finditer(normalize(answer)))
        if actions:
            action = actions[-1]
            current_order = proposal.candidate.orders[index]
            pending_dose_edit = (
                action.lastgroup == "change"
                and claim.value == old_value
                and current_order.dose != prior_order.dose
                and current_order.dose is not None
                and bool(grounded(current_order.dose, answer))
            )
            if (action.lastgroup != claim.value and not pending_dose_edit) or _NEGATIVE.search(
                normalize(answer)[: action.start()]
            ):
                return True, None
    if claim.field == "dose" and answer and numbers_in(answer):
        if not _answer_dose(claim.value, answer, proposal.candidate.orders[index]):
            return True, None
        # Frequency/timing-only answers do not retire a prescription dose.
        other_field = re.search(
            r"\b(?:times|daily|days?|hours?|tomorrow)\b|مرات|مرتين|يومي|ساعه|ساعات|بكره",
            normalize(answer),
        )
        if not other_field and not grounded(claim.value, answer):
            return True, None
    if old_value != claim.value:
        anchors = (
            name_spans(claim.value, reply, "drug", ctx)
            if claim.field == "drug"
            else grounded(claim.value, reply)
        )
        answer_anchors = (
            name_spans(claim.value, answer, "drug", ctx)
            if claim.field == "drug"
            else grounded(claim.value, answer)
        )
        if not answer or not answer_anchors or not anchors:
            return True, None
        return True, _correction_evidence(
            claim, proposal, tuple((a + offset, b + offset) for a, b in anchors)
        )
    old_evidence = next(
        (
            e
            for e in previous.evidence
            if e.item == claim.item and e.field == claim.field and e.value == claim.value
        ),
        None,
    )
    if old_evidence and valid_record(old_evidence, claim, previous):
        return True, old_evidence.model_copy(
            update={
                "source_ref": proposal.source_receipt_id
                if old_evidence.origin != "stored_prior_order"
                else old_evidence.source_ref
            }
        )
    # An answer can supply support for a previously ungrounded value without changing it.
    if answer and claim.field != "action" and (anchors := grounded(claim.value, reply)):
        return True, _correction_evidence(
            claim, proposal, tuple((a + offset, b + offset) for a, b in anchors)
        )
    return False, None


def instruction_fact_blocks(proposal: Proposal) -> tuple[int, ...]:
    """R5: only a fact's own instruction verb can trigger the segment block."""
    from sanad.scribe.terms import instruction_verb

    boundaries = sorted(
        {
            0,
            len(proposal.source_text),
            *(
                point
                for match in _BOUNDARY.finditer(proposal.source_text)
                for point in (match.start(), match.end())
            ),
            *(
                point
                for match in re.finditer(r"[,،]|\band\b|(?<= )و(?= )", proposal.source_text, re.I)
                for point in (match.start(), match.end())
            ),
        }
    )
    instructions = tuple(
        span
        for evidence in proposal.evidence
        if (
            (evidence.item.startswith("order:") and evidence.field in {"drug", "dose", "action"})
            or (
                evidence.item.startswith("mission:")
                and (evidence.field == "text" or evidence.field.startswith("name:"))
            )
        )
        for span in evidence.offsets
    )
    blocked = []
    for i, fact in enumerate(proposal.candidate.facts):
        if fact.category not in {
            "condition",
            "history",
            "medication_history",
        } or not instruction_verb(fact.text):
            continue
        spans = tuple(
            span
            for e in proposal.evidence
            if e.item == f"fact:{i}"
            and e.field == "text"
            and e.origin in {"transcript_span", "vocabulary_alias"}
            for span in e.offsets
        )
        if any(
            a < right and b > left and c < right and d > left
            for left, right in zip(boundaries, boundaries[1:], strict=False)
            for a, b in spans
            for c, d in instructions
        ):
            blocked.append(i)
    return tuple(blocked)


def seal(proposal: Proposal, ctx: Context, previous: Proposal | None = None) -> Proposal:
    """Build evidence, filter/reindex, rebuild, then seal the final candidate."""
    from sanad.scribe.extract import ProposalIssue
    from sanad.scribe.terms import drop_instruction_fact

    if proposal.photo:
        return proposal
    built = _seal_evidence(proposal, ctx, previous)
    dropped = {
        i
        for i, fact in enumerate(built.candidate.facts)
        if not (set(numbers_in(fact.text)) - set(numbers_in(built.source_text)))
        and drop_instruction_fact(fact, built.candidate, built.source_text)
    }
    if dropped:
        mapping = {
            old: new
            for new, old in enumerate(
                i for i in range(len(built.candidate.facts)) if i not in dropped
            )
        }

        def remap(item: str) -> str | None:
            if not item.startswith("fact:"):
                return item
            index = int(item.split(":")[1])
            return f"fact:{mapping[index]}" if index in mapping else None

        candidate = built.candidate.model_copy(
            update={
                "facts": tuple(f for i, f in enumerate(built.candidate.facts) if i not in dropped)
            }
        )
        candidate._dropped_facts = (
            *candidate._dropped_facts,
            *("fact_instruction_content" for _ in dropped),
        )
        candidate._merge_issues = tuple(
            issue.model_copy(update={"item": item})
            for issue in candidate._merge_issues
            if (item := remap(issue.item)) is not None
        )
        built = built.model_copy(
            update={
                "candidate": candidate,
                "names": tuple(
                    n.model_copy(update={"item": item})
                    for n in built.names
                    if (item := remap(n.item)) is not None
                ),
                "issues": tuple(
                    i.model_copy(update={"item": item})
                    for i in built.issues
                    if not i.grounding_issue and (item := remap(i.item)) is not None
                ),
                "single_source": tuple(
                    item for i in built.single_source if (item := remap(i)) is not None
                ),
                "evidence": (),
                "evidence_fingerprint": "",
            }
        )
        built = _seal_evidence(built, ctx, previous)
    blocked = instruction_fact_blocks(built)
    if blocked:
        issue = ProposalIssue(
            item="all",
            code="clinical_unclear",
            field="fact_instruction",
            grounding_issue=True,
            question=(
                f'I heard "{built.candidate.facts[blocked[0]].text}"; what should I record?'
                if built.language == "en"
                else f'سمعت "{built.candidate.facts[blocked[0]].text}"؛ أسجل إيه؟'
            ),
        )
        built = built.model_copy(
            update={
                "issues": (*built.issues, issue),
                "evidence": tuple(
                    e
                    for e in built.evidence
                    if not (e.field == "category" and e.item in {f"fact:{i}" for i in blocked})
                ),
            }
        )
    return built.model_copy(update={"evidence_fingerprint": _fingerprint(built)})


def _seal_evidence(proposal: Proposal, ctx: Context, previous: Proposal | None = None) -> Proposal:
    """Run after code has resolved names, corrections, stored orders and deadlines."""
    from sanad.scribe.extract import ProposalIssue

    if proposal.photo:
        return proposal
    drugs = {
        f"order:{i}": _instruction_span(
            order,
            proposal.source_text[: partition_for(proposal).original_end],
            ctx,
            next(
                (n.spoken for n in proposal.names if n.item == f"order:{i}" and n.kind == "drug"),
                order.drug,
            ),
        )
        for i, order in enumerate(proposal.candidate.orders)
    }
    all_drugs = tuple(
        m.span for m in mentions(proposal.source_text, ctx, partition_for(proposal).lexicon)
    )
    evidence: list[FieldEvidence] = []
    issues: list[ProposalIssue] = [i for i in proposal.issues if not i.grounding_issue]
    for request in missing_instructions(proposal.candidate, proposal.source_text):
        issues.append(
            ProposalIssue(
                item="all",
                grounding_issue=True,
                field="request",
                code="clinical_unclear",
                question=(
                    f'I heard "{request}"; what should I record?'
                    if proposal.language == "en"
                    else f'سمعت "{request}"؛ أسجل إيه؟'
                ),
            )
        )
    for claim in inventory(proposal):
        record = _record(claim, proposal, ctx, previous, drugs, all_drugs)
        if record and valid_record(record, claim, proposal):
            evidence.append(record)
            if (
                claim.item.startswith("order:")
                and record.transformation == "name_resolver:proposal"
            ):
                reading = proposal.names[int(claim.field.split(":")[1])]
                issues.append(
                    ProposalIssue(
                        item=claim.item,
                        field="verification",
                        grounding_issue=True,
                        code="drug_unclear",
                        blocked=False,
                        question=(
                            f'I heard "{reading.spoken}"; confirm the unverified name '
                            f"{reading.latin}."
                            if proposal.language == "en"
                            else f'سمعت "{reading.spoken}"؛ أكد الاسم غير المتحقق {reading.latin}.'
                        ),
                    )
                )
            continue
        if claim.field.startswith("name:") and any(
            i.item == claim.item and i.field in {"text", "drug"} and i.blocked for i in issues
        ):
            continue
        # One question per item, field, reason and occurrence. A missing action
        # cannot swallow an independent missing-dose question.
        if claim.field == "action" and any(
            i.item == claim.item and i.field == "action" and i.code == "extraction_conflict"
            for i in issues
        ):
            continue
        if any(
            i.blocked
            and i.item == claim.item
            and (
                i.field == claim.field
                or (
                    i.code in {"unsupported_number", "disputed_number"}
                    and set(i.numbers) & set(numbers_in(claim.value))
                )
            )
            for i in issues
        ):
            continue
        quote = _heard_question(claim, proposal, drugs.get(claim.item))
        issues.append(
            ProposalIssue(
                item=claim.item,
                field=claim.field,
                code="clinical_unclear",
                question=quote,
                grounding_issue=True,
            )
        )
    return proposal.model_copy(
        update={
            "evidence": tuple(evidence),
            "evidence_fingerprint": _fingerprint(proposal),
            "issues": deduplicate_questions(located_questions(tuple(issues), proposal.source_text)),
        }
    )


def missing_instructions(candidate: DictationCandidate, source: str) -> tuple[str, ...]:
    """Complement the existing request rail with per-clause nonnumeric coverage."""
    requests = re.finditer(
        r"(?:^|[.;\n,،]|\band\s+)(?:\s*(?:please|i\s+asked\s+(?:him|her)\s+to)\s+)?\s*"
        r"((?:call|send|bring|attend|visit|record|measure|book)\b[^.;\n]*|(?:اتصل|احضر|ابعت|قيس|سجل)\s+[^.;\n]*)",
        source,
        re.I,
    )
    missing = []
    for request in requests:
        text = request[1].strip()
        if not any(grounded(m.text, text) or grounded(text, m.text) for m in candidate.missions):
            missing.append(text)
    return tuple(missing)


def _heard_question(claim: Claim, proposal: Proposal, drug: tuple[int, int] | None) -> str:
    source = proposal.source_text
    if claim.field.startswith("name:"):
        named_reading = proposal.names[int(claim.field.split(":")[1])]
        if matches := grounded(named_reading.spoken, source):
            a, b = matches[-1]
            quote = source[a:b]
            return (
                f'I heard "{quote}"; please clarify this clinical phrase.'
                if proposal.language == "en"
                else f'سمعت "{quote}"؛ وضّح العبارة الطبية.'
            )
    label = "clinical phrase" if claim.field.startswith("name:") else claim.field.replace("_", " ")
    if proposal.language == "ar":
        label = {
            "drug": "اسم الدواء",
            "action": "الإجراء",
            "dose": "الجرعة",
            "frequency": "التكرار",
            "route": "طريقة الاستعمال",
            "timing": "التوقيت",
            "duration": "المدة",
            "text": "العبارة",
            "sex": "النوع",
            "age": "العمر",
            "name_as_spoken": "اسم المريض",
            "previous_drug": "اسم الدواء السابق",
            "previous_dose": "الجرعة السابقة",
            "effective_expression": "موعد بدء التغيير",
            "checkin_expression": "موعد المتابعة",
            "timing_expression": "الموعد",
        }.get(claim.field, "هذا البند")
    anchors = grounded(claim.value, source)
    span = drug or (anchors[-1] if anchors else None)
    if claim.item.startswith("order:"):
        cited_order = proposal.candidate.orders[int(claim.item.split(":")[1])]
        invalid_citation = (
            claim.field == "action"
            and cited_order.action_quote is not None
            and (
                not drug
                or not (citation := _cited_action(cited_order, source, drug))
                or citation[1]
            )
        )
    else:
        invalid_citation = False
    if claim.item.startswith("order:") and (not span or invalid_citation):
        order = proposal.candidate.orders[int(claim.item.split(":")[1])]
        reading = next(
            (n for n in proposal.names if n.item == claim.item and n.kind == "drug"), None
        )
        spoken = reading.spoken if reading else order.drug
        matches = grounded(spoken, source)
        if matches:
            span = matches[-1]
            if invalid_citation or not _action_at(source, span):
                quote = source[span[0] : span[1]]
                return (
                    f'I heard "{quote}"; please clarify the {label}.'
                    if proposal.language == "en"
                    else f'سمعت "{quote}"؛ وضّح {label}.'
                )
    if span:
        a, b = clause(source, *span)
        quote = source[a:b].strip(" ,،")
    else:
        # Never label invented candidate content as something the doctor said.
        quote = source.strip()[:300]
    return (
        f'I heard "{quote}"; please clarify the {label}.'
        if proposal.language == "en"
        else f'سمعت "{quote}"؛ وضّح {label}.'
    )


def located_questions(issues: tuple[ProposalIssue, ...], source: str) -> tuple[ProposalIssue, ...]:
    result = []
    for issue in issues:
        quotes = re.findall(r'"([^"\n]+)"', issue.question or "")
        anchors = grounded(quotes[0], source) if quotes else ()
        if issue.occurrence is None and len(anchors) == 1:
            issue = issue.model_copy(update={"occurrence": anchors[0]})
        result.append(issue)
    return tuple(result)


def unique_questions(questions: list[str], proposal: Proposal) -> tuple[str, ...]:
    """Text equality must not erase independent, explicitly located questions."""
    from collections import Counter

    counts = Counter(
        " ".join(i.question.split())
        for i in deduplicate_questions(proposal.issues)
        if i.occurrence is not None and i.question
    )
    seen: Counter[str] = Counter()
    result = []
    for question in questions:
        seen[question] += 1
        if seen[question] <= max(1, counts[question]):
            result.append(question)
    return tuple(result)


def deduplicate_questions(issues: tuple[ProposalIssue, ...]) -> tuple[ProposalIssue, ...]:
    seen: set[tuple[str, str | None, str, object]] = set()
    result = []
    for issue in issues:
        occurrence = (
            "|".join(re.findall(r'"([^"\n]+)"', issue.question or ""))
            if issue.field == "analyte"
            else ""
        )
        key = (
            issue.item,
            issue.field,
            issue.code,
            issue.occurrence or occurrence or "|".join(issue.numbers),
        )
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return tuple(result)


def _prior_ref(proposal: Proposal, item: str) -> str:
    from sanad.scribe.amend import order_key

    change = next((a for a in proposal.amendments if a.item == item and a.old), None)
    if not change or not change.old or not change.head_version:
        return ""
    return (
        f"doctor:{proposal.doctor_id}:patient:{proposal.selected_patient_id}:"
        f"order:{order_key(change.old.drug)}:head:{change.head_version}"
    )


def permitted_origins(claim: Claim) -> frozenset[Origin]:
    """New clinical fields fail closed until their evidence rule is explicitly registered."""
    if (
        claim.field == "deadline"
        or claim.field == "compiled_text"
        or claim.field.startswith("schedule:")
    ):
        return frozenset({"code_computed"})
    if claim.field.startswith("name:"):
        return frozenset({"transcript_span", "vocabulary_alias"})
    if claim.field.startswith("prior:"):
        return frozenset({"transcript_span", "vocabulary_alias", "stored_prior_order"})
    family = claim.item.split(":")[0]
    if family == "order":
        if claim.field in {"previous_drug", "previous_dose"}:
            return frozenset({"code_computed", "authorized_correction"})
        if claim.field == "drug":
            return frozenset({"transcript_span", "vocabulary_alias", "authorized_correction"})
        if claim.field == "action":
            return frozenset({"transcript_span", "authorized_correction", "code_computed"})
        if claim.field in {
            "dose",
            "frequency",
            "route",
            "timing",
            "duration",
            "effective_expression",
            "checkin_expression",
        }:
            return frozenset({"transcript_span", "authorized_correction", "stored_prior_order"})
    if family in {"fact", "mission"} and claim.field in {"category", "kind"}:
        return frozenset({"code_computed"})
    if family in {"fact", "mission", "alert"} and claim.field == "text":
        return frozenset({"transcript_span", "vocabulary_alias", "code_computed"})
    if family == "mission" and claim.field == "timing_expression":
        return frozenset({"transcript_span"})
    if family == "patient":
        if claim.field == "sex":
            return frozenset({"vocabulary_alias"})
        if claim.field in {"name_as_spoken", "age"} or claim.field.startswith("identifiers:"):
            return frozenset({"transcript_span"})
    return frozenset()


def _unchanged_field(proposal: Proposal, claim: Claim) -> bool:
    from sanad.scribe.amend import order_key

    change = next((a for a in proposal.amendments if a.item == claim.item and a.old), None)
    order = proposal.candidate.orders[int(claim.item.split(":")[1])]
    return bool(
        change
        and change.old
        and order_key(change.old.drug) == order_key(order.drug)
        and getattr(change.old, claim.field, None) == claim.value
        and untouched(
            order,
            claim.field,
            proposal.source_text,
            partition_for(proposal),
            item=claim.item,
            prior_value=claim.value,
        )
    )


def _correction_evidence(
    claim: Claim, proposal: Proposal, offsets: tuple[tuple[int, int], ...]
) -> FieldEvidence | None:
    extent = partition_for(proposal).corrections[-1]
    if claim.field in {"dose", "previous_dose"}:
        offsets = tuple(
            s for s in offsets if complete_quantity(claim.value, proposal.source_text, s)
        )
    record = _evidence(
        claim, proposal, "authorized_correction", offsets, "answer_slot:" + claim.item
    ).model_copy(
        update={"correction_id": extent.proposal_id, "correction_version": extent.proposal_version}
    )
    return record if _valid_correction(record, claim, proposal) else None


def _reply_instruction_offsets(
    claim: Claim, proposal: Proposal, start: int, end: int
) -> tuple[tuple[int, int], ...]:
    """Reproduce an explicit instruction inside one authenticated reply only."""
    from sanad.scribe.resolver import Context

    order = proposal.candidate.orders[int(claim.item.split(":")[1])]
    reply = proposal.source_text[start:end]
    found = mentions(reply, lexicon=partition_for(proposal).lexicon)
    named = [m for m in found if not m.ambiguous and normalize(m.name) == normalize(order.drug)]
    if not named:
        return ()
    drug = _instruction_span(order, reply, Context(), reply[slice(*named[-1].span)])
    if drug is None or drug not in {m.span for m in named}:
        return ()
    action = _action_at(reply, drug)
    if not action or action[2] or action[0] != order.action:
        return ()
    offsets: tuple[tuple[int, int], ...] = (action[1], drug)
    if claim.field not in {"drug", "action"}:
        if claim.field not in {
            "dose",
            "frequency",
            "route",
            "timing",
            "duration",
            "effective_expression",
            "checkin_expression",
        }:
            return ()
        attached = _dose_span(
            claim.value, reply, drug, tuple(m.span for m in found), dose=claim.field == "dose"
        )
        if attached is None:
            return ()
        if claim.field == "dose":
            if not complete_quantity(claim.value, reply, attached):
                return ()
            if order.action == "change":
                binding = bind_change(order, reply)
                if not binding or binding.new_dose != attached:
                    return ()
        offsets = (*offsets, attached)
    return tuple((a + start, b + start) for a, b in offsets)


def _answer_dose(value: str, answer: str, order: OrderCandidate) -> bool:
    # Timing-only replies leave a dose alone. A directional dose answer owns only TO.
    if re.search(
        r"\b(?:times|daily|days?|hours?|tomorrow)\b|مرات|مرتين|يومي|ساعه|ساعات|بكره",
        normalize(answer),
    ) and not re.search(r"\bdose\b|جرعه", normalize(answer)):
        return True
    bound = bind_change(
        order.model_copy(update={"action": "change", "previous_drug": None, "previous_dose": None}),
        answer,
    )
    if bound:
        return bool(bound.new_dose and complete_quantity(value, answer, bound.new_dose))
    return any(complete_quantity(value, answer, s) for s in grounded(value, answer))


def _valid_correction(record: FieldEvidence, claim: Claim, proposal: Proposal) -> bool:
    partition = partition_for(proposal)

    def supported(text: str) -> bool:
        if claim.field == "drug":
            return any(
                not m.ambiguous and normalize(m.name) == normalize(claim.value)
                for m in mentions(text, lexicon=partition.lexicon)
            )
        return bool(grounded(claim.value, text))

    for extent in partition.corrections:
        if (record.correction_id, record.correction_version) != (
            extent.proposal_id,
            extent.proposal_version,
        ):
            continue
        if record.transformation == "reply_instruction:" + claim.item:
            instruction_offsets = _reply_instruction_offsets(
                claim, proposal, extent.start, extent.end
            )
            return bool(
                instruction_offsets
                and record.offsets == instruction_offsets
                and record.source_ref == proposal.source_receipt_id
            )
        answer = dict(extent.answers).get(claim.item, "")
        if claim.field in {"previous_drug", "previous_dose"}:
            order = proposal.candidate.orders[int(claim.item.split(":")[1])]
            binding = bind_change(order, proposal.source_text, partition, item=claim.item)
            if not binding or (binding.correction_id, binding.correction_version) != (
                record.correction_id,
                record.correction_version,
            ):
                return False
            expected = (
                binding.previous_name if claim.field == "previous_drug" else binding.previous_dose
            )
            return bool(expected and record.offsets == (expected,))
        if not answer or not supported(answer):
            return False
        if claim.field == "dose" and not _answer_dose(
            claim.value, answer, proposal.candidate.orders[int(claim.item.split(":")[1])]
        ):
            return False
        if claim.field in {"dose", "previous_dose"} and not any(
            complete_quantity(claim.value, answer, s) for s in grounded(claim.value, answer)
        ):
            return False
        return (
            bool(record.offsets)
            and record.source_ref == proposal.source_receipt_id
            and record.transformation == "answer_slot:" + claim.item
            and all(
                extent.start <= a < b <= extent.end
                and supported(proposal.source_text[a:b])
                and (
                    claim.field not in {"dose", "previous_dose"}
                    or complete_quantity(claim.value, proposal.source_text, (a, b))
                )
                for a, b in record.offsets
            )
        )
    return False


def valid_record(record: FieldEvidence, claim: Claim, proposal: Proposal) -> bool:
    if (record.item, record.field, record.value) != (claim.item, claim.field, claim.value):
        return False
    if record.origin not in permitted_origins(claim):
        return False
    if claim.item.startswith("order:"):
        partition = partition_for(proposal)
        if not partition.valid(proposal.source_text):
            return False
        index = int(claim.item.split(":")[1])
        order = proposal.candidate.orders[index]
        if record.origin == "authorized_correction":
            return _valid_correction(record, claim, proposal)
        if claim.field == "dose" and record.origin == "transcript_span":
            if len(record.offsets) != 2:
                return False
            complete = complete_quantity(claim.value, proposal.source_text, record.offsets[1])
            # Unresolved spoken words may remain visible only behind the existing numeric block.
            observed_only = not numbers_in(claim.value) and any(
                i.item == claim.item and i.code == "dose_missing" and i.blocked
                for i in proposal.issues
            )
            if not complete and not observed_only:
                return False
            original = proposal.source_text[: partition.original_end]
            if record.offsets[1][1] > partition.original_end:
                return False
            drug = record.offsets[0]
            all_drugs = tuple(m.span for m in mentions(original, lexicon=partition.lexicon))
            if _dose_span(claim.value, original, drug, all_drugs, dose=True) != record.offsets[1]:
                return False
            if order.action == "change":
                binding = bind_change(order, proposal.source_text, partition, item=claim.item)
                if binding and binding.new_dose != record.offsets[1]:
                    return False
        if claim.field in {"previous_drug", "previous_dose"} or (
            claim.field.startswith("prior:") and record.origin != "stored_prior_order"
        ):
            binding = bind_change(order, proposal.source_text, partition, item=claim.item)
            if not binding or not binding.previous_dose:
                return False
            if (record.correction_id, record.correction_version) != (
                binding.correction_id,
                binding.correction_version,
            ):
                return False
            if claim.field.startswith("prior:"):
                expected = (
                    binding.previous_name
                    if claim.field.endswith(":drug")
                    else binding.previous_dose
                )
                if record.offsets != (expected,):
                    return False
                if claim.field.endswith(":dose") and not complete_quantity(
                    claim.value, proposal.source_text, expected
                ):
                    return False
            elif record.offsets != binding.offsets:
                return False
    if record.origin == "vocabulary_alias" and not (
        claim.field.startswith("name:")
        or claim.field == "drug"
        or (claim.field.startswith("prior:") and claim.field.endswith(":drug"))
        or (claim.item == "patient" and claim.field == "sex")
        or (claim.item.startswith(("mission:", "alert:")) and claim.field == "text")
    ):
        return False
    if record.origin == "authorized_correction" and not claim.item.startswith("order:"):
        return False
    if record.origin == "transcript_span" and claim.item.startswith("order:"):
        required = (
            "instruction_clause"
            if claim.field == "action"
            else "drug_name"
            if claim.field == "drug"
            else "instruction_attachment"
            if claim.field
            in {
                "dose",
                "frequency",
                "route",
                "timing",
                "duration",
                "effective_expression",
                "checkin_expression",
            }
            else None
        )
        if required and record.transformation != required:
            return False
    if record.origin in {"transcript_span", "vocabulary_alias", "authorized_correction"}:
        if record.origin == "transcript_span" and record.offsets:
            if record.transformation in {
                "normalized_words",
                "explicit_previous_instruction",
            } and not all(
                grounded(claim.value, proposal.source_text[a:b]) for a, b in record.offsets
            ):
                return False
            if record.transformation == "instruction_attachment":
                if len(record.offsets) != 2 or not grounded(
                    claim.value, proposal.source_text[record.offsets[1][0] : record.offsets[1][1]]
                ):
                    return False
            if record.transformation == "instruction_clause":
                if len(record.offsets) != 2:
                    return False
                order = proposal.candidate.orders[int(claim.item.split(":")[1])]
                if order.action_quote is not None:
                    cited = _cited_action(order, proposal.source_text, record.offsets[1])
                    if not cited or cited[1] or cited[0] != record.offsets[0]:
                        return False
                else:
                    action = _action_at(proposal.source_text, record.offsets[1])
                    if (
                        not action
                        or action[0] != claim.value
                        or action[2]
                        or action[1] != record.offsets[0]
                    ):
                        return False
        return (
            bool(record.offsets)
            and record.source_ref == proposal.source_receipt_id
            and all(0 <= a < b <= len(proposal.source_text) for a, b in record.offsets)
            and bool(record.transformation)
        )
    if record.origin == "stored_prior_order":
        return (
            bool(proposal.selected_patient_id)
            and record.source_ref == _prior_ref(proposal, claim.item)
            and bool(record.source_ref)
            and (
                claim.field.startswith("prior:")
                or (
                    claim.field in {"dose", "frequency", "route", "timing", "duration"}
                    and record.transformation == "unchanged_prior_field"
                    and _unchanged_field(proposal, claim)
                )
            )
        )
    from sanad.scribe.amend import history_source

    permitted_computation = {
        "home_medication_instruction": claim.field == "text"
        and claim.item.startswith("fact:")
        and history_source(proposal, claim.value) is not None,
        "resolved_timing": claim.field == "deadline",
        "candidate_classification": claim.field in {"kind", "category"}
        and not (
            claim.item.startswith("fact:")
            and claim.field == "category"
            and int(claim.item.split(":")[1]) in instruction_fact_blocks(proposal)
        ),
        "resolved_test_list": claim.field == "text" and claim.item.startswith("mission:"),
        "existing_order_reconciliation": claim.field == "action"
        and claim.item.startswith("order:"),
        "validated_previous_instruction": claim.field in {"previous_drug", "previous_dose"},
        "monitor_request_compiler": claim.item.startswith("mission:")
        and (claim.field == "compiled_text" or claim.field.startswith("schedule:")),
    }
    return (
        record.origin == "code_computed"
        and permitted_computation.get(record.transformation, False)
        and record.source_ref
        == (
            "timing:" + claim.item
            if record.transformation == "resolved_timing"
            else proposal.source_receipt_id
        )
    )


def invalid_claims(proposal: Proposal) -> tuple[Claim, ...]:
    if proposal.photo:
        return ()
    try:
        hash(proposal)
    except TypeError:
        return _invalid_claims.__wrapped__(proposal)
    return _invalid_claims(proposal)


@lru_cache(maxsize=64)
def _invalid_claims(proposal: Proposal) -> tuple[Claim, ...]:
    claims = inventory(proposal)
    if proposal.evidence_fingerprint != _fingerprint(proposal):
        return claims
    return tuple(
        c for c in claims if not any(valid_record(e, c, proposal) for e in proposal.evidence)
    )


def permits(proposal: Proposal, item: str, field: str) -> bool:
    return not any(c.item == item and c.field == field for c in invalid_claims(proposal))


def has_evidence(proposal: Proposal) -> bool:
    return bool(proposal.evidence_fingerprint)


def ensure_evidence(proposal: Proposal) -> Proposal:
    """Revalidate legacy pending dictations through the same code gate before use."""
    from sanad.scribe.resolver import Context

    return (
        proposal if proposal.photo or proposal.evidence_fingerprint else seal(proposal, Context())
    )


def confirmable_evidence(proposal: Proposal) -> bool:
    """Blocked fields may be deferred, never silently compiled or learned."""
    return proposal.photo is not None or (
        proposal.evidence_fingerprint == _fingerprint(proposal)
        and all(
            any(i.blocked and i.item in {"all", c.item} for i in proposal.issues)
            for c in invalid_claims(proposal)
        )
    )


def deduplicate_instructions(
    candidate: DictationCandidate, source: str, ctx: Context
) -> DictationCandidate:
    from sanad.scribe.changes import _without_orders
    from sanad.scribe.resolver import resolve_name

    seen: dict[tuple[str, str, str], int] = {}
    removed: set[int] = set()
    for i, order in enumerate(candidate.orders):
        resolved = resolve_name(order.drug, "drug", source, order.name_latin, ctx)
        identity = resolved.latin or order.drug
        key = (
            normalize(identity),
            order.action,
            order.model_copy(
                update={"drug": identity, "name_latin": None, "generic": None, "action_quote": None}
            ).model_dump_json(),
        )
        occurrences = max(1, len(name_spans(identity, source, "drug", ctx)))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > occurrences:
            removed.add(i)
    return _without_orders(candidate, removed)[0]


def restore_phrase_boundaries(spoken: str, source: str, ctx: Context) -> str:
    from sanad.scribe.resolver import resolve_name

    parts = re.split(r"[,،]\s*", spoken)
    result: list[str] = []
    for part in parts:
        if result:
            combined = result[-1].strip() + " " + part.strip()
            match = grounded(combined, source)
            known = resolve_name(combined, "test", source, ctx=ctx)
            separate = [resolve_name(p, "test", source, ctx=ctx) for p in (result[-1], part)]
            if (
                match
                and (
                    (
                        known.tier in {"seed", "memory", "clinic"}
                        and any(
                            normalize(combined) == normalize(alias)
                            for alias in _aliases(known.latin or "", "test", ctx)
                        )
                    )
                    or all(r.tier in {"proposal", "unresolved"} for r in separate)
                )
                and any(not re.search(r"[,،]", source[a:b]) for a, b in match)
            ):
                result[-1] = combined
                continue
        result.append(part)
    return ", ".join(result)
