"""Occurrence-bound change evidence over explicitly partitioned canonical source."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import Field

from sanad.domain.boundaries import _BoundaryValue
from sanad.scribe.names import dictionary, normalize, split_drug_dose
from sanad.scribe.resolver import Context

if TYPE_CHECKING:
    from sanad.scribe.extract import OrderCandidate
    from sanad.scribe.proposal import Proposal

type Span = tuple[int, int]


class CorrectionExtent(_BoundaryValue):
    start: int
    end: int
    proposal_id: str
    proposal_version: int
    # Code-owned answer slots, captured from the authorising proposal, never a live lookup.
    answers: tuple[tuple[str, str], ...] = Field(default=(), repr=False)


class SourcePartition(_BoundaryValue):
    original_end: int
    corrections: tuple[CorrectionExtent, ...] = ()
    lexicon: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def valid(self, source: str) -> bool:
        end = self.original_end
        if not 0 <= end <= len(source):
            return False
        for reply in self.corrections:
            if not end <= reply.start <= reply.end <= len(source):
                return False
            if not reply.proposal_id or reply.proposal_version < 1:
                return False
            end = reply.end
        return end == len(source)


def partition_for(proposal: Proposal) -> SourcePartition:
    if proposal.source_partition is not None:
        return proposal.source_partition
    # Legacy corrected proposals have no trustworthy partition. Fail closed.
    return SourcePartition(original_end=0 if proposal.corrected else len(proposal.source_text))


def append_reply(previous: Proposal, reply: str) -> SourcePartition:
    from sanad.scribe.corrections import answer_slots

    prior = partition_for(previous)
    start = len(previous.source_text) + 1
    return prior.model_copy(
        update={
            "corrections": (
                *prior.corrections,
                CorrectionExtent(
                    start=start,
                    end=start + len(reply),
                    proposal_id=previous.id,
                    proposal_version=previous.version,
                    answers=tuple(answer_slots(previous, reply).items()),
                ),
            )
        }
    )


def authority_matches(
    partition: SourcePartition | None, source: str, previous: Proposal | None
) -> bool:
    if previous is None:
        return partition is None or partition.valid(source)
    if partition is None or not partition.valid(source) or not partition.corrections:
        return False
    extent = partition.corrections[-1]
    prior = partition_for(previous)
    return (
        extent.proposal_id == previous.id
        and extent.proposal_version == previous.version
        and partition.original_end == prior.original_end
        and partition.corrections[:-1] == prior.corrections
        and source[: extent.start] == previous.source_text + "\n"
    )


def with_context(partition: SourcePartition, source: str, ctx: Context) -> SourcePartition:
    """Retain source-matching resolver aliases for deterministic later replay."""
    from sanad.scribe.grounding import grounded

    lexicon = dict(partition.lexicon)
    if ctx.vocabulary:
        for rows in ctx.vocabulary.rows:
            for row in rows:
                if row.kind != "drug":
                    continue
                aliases = tuple(a for a in (row.latin, *row.spoken_forms) if grounded(a, source))
                if aliases:
                    lexicon[row.latin] = tuple(
                        dict.fromkeys((*lexicon.get(row.latin, ()), *aliases))
                    )
    return partition.model_copy(update={"lexicon": tuple(sorted(lexicon.items()))})


_NUMBER = r"\d+(?:[.٫]\d+)?"
_UNIT = r"(?:mcg|mg|ml|g|units?|drops?|tablets?|capsules?|puffs?|مجم|مج|مليجرام|ميكروجرام|مل)"
_QUANTITY = re.compile(
    rf"(?<![\w.٫]){_NUMBER}(?:(?:\s*(?:/|,|،|\bover\b|على)\s*|\s+){_NUMBER})*(?:\s*{_UNIT}(?!\w))?",
    re.I,
)


def quantity_key(value: str) -> tuple[str, ...]:
    value = normalize(value).replace("٫", ".")
    for arabic, latin in (
        ("مليجرام", "mg"),
        ("ميكروجرام", "mcg"),
        ("مجم", "mg"),
        ("مج", "mg"),
        ("مل", "ml"),
    ):
        value = value.replace(arabic, latin)
    return tuple(
        str(Decimal(token).normalize()) if token[0].isdigit() else token
        for token in re.findall(r"\d+(?:\.\d+)?|[a-z]+", value)
        if token != "over"
    )


def quantities(source: str) -> tuple[Span, ...]:
    return tuple(m.span() for m in _QUANTITY.finditer(source))


def complete_quantity(value: str, source: str, span: Span) -> bool:
    return (
        bool(_QUANTITY.fullmatch(value.strip()))
        and span in quantities(source)
        and quantity_key(value) == quantity_key(source[slice(*span)])
    )


@dataclass(frozen=True)
class Mention:
    name: str
    span: Span
    ambiguous: bool = False


def mentions(
    source: str, ctx: Context | None = None, lexicon: tuple[tuple[str, tuple[str, ...]], ...] = ()
) -> tuple[Mention, ...]:
    from sanad.scribe.grounding import _tokens, grounded

    # A local first-token index avoids repeatedly scanning a long dictation for
    # every absent alias. It is only a prefilter: grounded still decides matches.
    def forms(word: str) -> set[str]:
        return {
            word,
            *(
                word[len(p) :]
                for p in ("وال", "ال", "و")
                if word.startswith(p) and len(word) > len(p) + 1
            ),
        }

    source_words = {form for token, _, _ in _tokens(source) for form in forms(token)}
    ctx = ctx or Context()
    vocabulary: dict[str, set[str]] = {}
    for entry in dictionary():
        if entry.kind == "drug":
            vocabulary.setdefault(entry.latin, set()).update(
                (entry.latin, *entry.arabic_spellings, *entry.latin_spellings)
            )
    if ctx.vocabulary:
        for rows in ctx.vocabulary.rows:
            for row in rows:
                if row.kind == "drug":
                    vocabulary.setdefault(row.latin, set()).update((row.latin, *row.spoken_forms))
    for lookup in ctx.lookups.values():
        if lookup.found:
            vocabulary.setdefault(lookup.canonical, set()).add(lookup.canonical)
    for name, aliases in lexicon:
        vocabulary.setdefault(name, set()).update(aliases)
    found: dict[Span, set[str]] = {}
    for name, name_aliases in vocabulary.items():
        for alias in name_aliases:
            tokens = _tokens(alias)
            if not tokens or not forms(tokens[0][0]) & source_words:
                continue
            for span in grounded(alias, source):
                found.setdefault(span, set()).add(name)
    maximal = [s for s in found if not any(t != s and t[0] <= s[0] and s[1] <= t[1] for t in found)]
    return tuple(
        Mention(sorted(found[s])[0], s, len({normalize(n) for n in found[s]}) != 1)
        for s in sorted(maximal)
    )


_CHANGE = re.compile(
    r"(?<!\w)(?:و)?(?:increase|decrease|raise|reduce|upgrade|change|switch|replace|زود(?:ت)?|قلل(?:ت)?|غير(?:ت)?|بدل)(?!\w)",
    re.I,
)
_FORWARD = re.compile(r"\bto(?:\s+be)?\b|\bwith\b|(?<!\w)(?:الي|الى|لـ?|بـ)(?=\s|\d)", re.I)
_REVERSE = re.compile(r"\bfrom\b|\binstead of\b|(?<!\w)(?:بدل|من)(?!\w)", re.I)
_UNSAFE = re.compile(
    r"\b(?:no|not|never|avoid|if|might|could|consider|would|previously|formerly|"
    r"was|were|used to|mother|father|sister|brother|wife|husband|family|"
    r"another patient|new patient)\b|n['’]?t\b|"
    r"(?<!\w)(?:لا|مش|لو|كان|زمان|امه|ابوه|والدته|والده|مريض تاني)(?!\w)",
    re.I,
)

_CURRENT = re.compile(
    r"\b(?:taking|on|continue|currently)\b|(?<!\w)(?:بياخد|واخد|ماشي علي|ماشى علي|استمر|كمل)(?!\w)",
    re.I,
)
_EVENT = re.compile(
    r"\b(?:stop|discontinue|actually|correction|instead|sorry)\b|(?<!\w)(?:وقف|بطل|اقصد|قصدي)(?!\w)",
    re.I,
)


def assertion(source: str, span: Span) -> Span:
    from sanad.scribe.grounding import clause

    left, right = clause(source, *span)
    # Commas separate local assertions, except they do not define cross-clause suppliers.
    left_candidate = left
    for m in re.finditer(r"[,،]", source[left:right]):
        at = left + m.start()
        if at < span[0]:
            left_candidate = at + 1
        elif at >= span[1]:
            right = at
            break
    return left_candidate, right


def attached(source: str, mention: Mention, bounds: Span) -> Span | None:
    left, right = bounds
    matches = []
    for span in quantities(source):
        if not left <= span[0] < span[1] <= right:
            continue
        if mention.span[1] <= span[0] and not source[mention.span[1] : span[0]].strip():
            matches.append(span)
        elif span[1] <= mention.span[0] and re.fullmatch(
            r"\s*(?:of|من)?\s*", source[span[1] : mention.span[0]], re.I
        ):
            matches.append(span)
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class ChangeBinding:
    anchor: Span
    previous_name: Span
    previous_dose: Span | None
    new_name: Span
    new_dose: Span | None
    previous_identity: str
    new_identity: str
    correction_id: str | None = None
    correction_version: int | None = None

    @property
    def offsets(self) -> tuple[Span, ...]:
        return tuple(
            s
            for s in (
                self.anchor,
                self.previous_name,
                self.previous_dose,
                self.new_name,
                self.new_dose,
            )
            if s is not None
        )


def bind_change(
    order: OrderCandidate,
    source: str,
    partition: SourcePartition | None = None,
    ctx: Context | None = None,
    item: str | None = None,
) -> ChangeBinding | None:
    """Select suppliers before comparing the candidate. Never populate a missing dose."""
    from sanad.scribe.amend import order_key

    partition = partition or SourcePartition(original_end=len(source))
    if not partition.valid(source):
        return None
    original = source[: partition.original_end]
    inventory = mentions(original, ctx, partition.lexicon)
    if any(m.ambiguous for m in inventory):
        return None
    target = order_key(split_drug_dose(order.drug)[0])
    results = []
    corrected_supplier: tuple[Span, CorrectionExtent] | None = None
    if order.previous_dose:
        from sanad.scribe.grounding import grounded

        for extent in partition.corrections:
            reply = source[extent.start : extent.end]
            # The answer must explicitly identify the prior value; a TO-dose reply
            # is never a supplier for FROM, even if its numeric string is equal.
            for answer_item, answer in extent.answers:
                if item is None or answer_item != item:
                    continue
                if not re.search(
                    r"\b(?:previous|prior|old|from)\b|السابق|القديم|كانت", normalize(answer)
                ):
                    continue
                from sanad.scribe.grounding import _NEGATIVE, _OTHER

                if (
                    _NEGATIVE.search(normalize(answer))
                    or _OTHER.search(normalize(answer))
                    or re.search(r"\b(?:if|might|could|would)\b|(?<!\w)لو(?!\w)", normalize(answer))
                ):
                    continue
                supplier_values = {
                    quantity_key(answer[slice(*span)]) for span in quantities(answer)
                }
                if supplier_values != {quantity_key(order.previous_dose)}:
                    continue
                if not order.previous_drug or not any(
                    order_key(m.name) == order_key(order.previous_drug)
                    for m in mentions(answer, ctx, partition.lexicon)
                ):
                    continue
                spans = [
                    s
                    for s in grounded(order.previous_dose, reply)
                    if complete_quantity(order.previous_dose, reply, s)
                ]
                if len(spans) == 1 and any(
                    complete_quantity(order.previous_dose, answer, s)
                    for s in grounded(order.previous_dose, answer)
                ):
                    a, b = spans[0]
                    corrected_supplier = ((a + extent.start, b + extent.start), extent)
    for action in _CHANGE.finditer(original):
        left, right = assertion(original, action.span())
        if _UNSAFE.search(normalize(original[left:right])):
            continue
        connectors = list(_FORWARD.finditer(original, action.end(), right))
        if len(connectors) != 1:
            continue
        connector = connectors[0]
        local = [m for m in inventory if action.end() <= m.span[0] and m.span[1] <= right]
        if any(m.ambiguous for m in local):
            continue
        before = [m for m in local if m.span[1] <= connector.start()]
        after = [m for m in local if m.span[0] >= connector.end()]
        reverse = list(_REVERSE.finditer(original, connector.end(), right))
        prior: Mention | None = None
        new: Mention | None = None
        if reverse:
            if len(reverse) != 1 or before:
                continue
            to_names = [m for m in after if m.span[1] <= reverse[0].start()]
            from_names = [m for m in after if m.span[0] >= reverse[0].end()]
            if len(to_names) == len(from_names) == 1:
                new, prior = to_names[0], from_names[0]
        elif len(before) == len(after) == 1:
            prior, new = before[0], after[0]
        elif len(before) == 1 and not after:
            prior = new = before[0]
        elif not before and len(after) == 1:
            new = after[0]
            eligible = [
                m for m in inventory if m.span[1] <= action.start() and current(original, m)
            ]
            if len({order_key(m.name) for m in eligible}) == 1:
                prior = eligible[-1]
        if prior is None or new is None or order_key(new.name) != target:
            continue
        if order.previous_drug and order_key(order.previous_drug) != order_key(prior.name):
            continue
        old_bounds = (
            (left, connector.start())
            if prior.span[0] >= action.end() and not reverse
            else assertion(original, prior.span)
        )
        if reverse:
            old_bounds = (reverse[0].end(), right)
        old_dose = attached(original, prior, old_bounds)
        if prior.span[1] <= action.start():
            # An omitted FROM still obeys the same intervening-event fence.
            broken = any(
                order_key(m.name) == order_key(prior.name)
                and prior.span[0] < m.span[0] < action.start()
                and not current(original, m)
                for m in inventory
            )
            if broken or _UNSAFE.search(normalize(original[prior.span[1] : action.start()])):
                continue
        if new == prior:
            values = [
                s
                for s in quantities(original)
                if connector.end() <= s[0] < s[1] <= right
                and not original[connector.end() : s[0]].strip()
            ]
            new_dose = values[0] if len(values) == 1 else None
        else:
            new_dose = attached(
                original, new, (connector.end(), reverse[0].start() if reverse else right)
            )
        suppliers: list[tuple[Mention, Span]] = [(prior, old_dose)] if old_dose else []
        for earlier in inventory:
            if (
                earlier.span[1] > action.start()
                or earlier == prior
                or order_key(earlier.name) != order_key(prior.name)
            ):
                continue
            if not current(original, earlier):
                continue
            intervening = original[earlier.span[1] : action.start()]
            # Only events concerning this identity break its current-medication chain.
            broken = any(
                order_key(m.name) == order_key(prior.name)
                and m.span[0] > earlier.span[0]
                and m.span[1] < action.start()
                and not current(original, m)
                for m in inventory
            )
            if broken or _UNSAFE.search(normalize(intervening)):
                continue
            dose = attached(original, earlier, assertion(original, earlier.span))
            if dose:
                suppliers.append((earlier, dose))
        if suppliers:
            keys = {quantity_key(original[slice(*s)]) for _, s in suppliers}
            if len(keys) != 1:
                continue
            selected, dose = next(
                ((m, s) for m, s in suppliers if m == prior),
                max(suppliers, key=lambda pair: pair[0].span[0]),
            )
            if (
                order.previous_dose
                and not corrected_supplier
                and not complete_quantity(order.previous_dose, original, dose)
            ):
                continue
            previous_name, previous_dose = selected.span, dose
        else:
            if order.previous_dose and not corrected_supplier:
                continue
            previous_name, previous_dose = prior.span, None
        if corrected_supplier:
            previous_dose = corrected_supplier[0]
        if (
            previous_dose
            and new_dose
            and previous_dose[0] < new_dose[1]
            and new_dose[0] < previous_dose[1]
        ):
            continue
        results.append(
            ChangeBinding(
                action.span(),
                previous_name,
                previous_dose,
                new.span,
                new_dose,
                prior.name,
                new.name,
                corrected_supplier[1].proposal_id if corrected_supplier else None,
                corrected_supplier[1].proposal_version if corrected_supplier else None,
            )
        )
    if not results and item:
        for extent in partition.corrections:
            answer = dict(extent.answers).get(item, "")
            if not answer:
                continue
            reply = source[extent.start : extent.end]
            local_binding = bind_change(
                order,
                reply,
                SourcePartition(original_end=len(reply), lexicon=partition.lexicon),
                ctx,
            )
            if not local_binding or not local_binding.previous_dose:
                continue
            from sanad.scribe.grounding import grounded

            if not order.previous_dose or not any(
                complete_quantity(order.previous_dose, answer, s)
                for s in grounded(order.previous_dose, answer)
            ):
                continue

            def shift(span: Span, offset: int = extent.start) -> Span:
                return span[0] + offset, span[1] + offset

            results.append(
                ChangeBinding(
                    shift(local_binding.anchor),
                    shift(local_binding.previous_name),
                    shift(local_binding.previous_dose),
                    shift(local_binding.new_name),
                    shift(local_binding.new_dose) if local_binding.new_dose else None,
                    local_binding.previous_identity,
                    local_binding.new_identity,
                    extent.proposal_id,
                    extent.proposal_version,
                )
            )
    return results[0] if len(results) == 1 else None


def current(source: str, mention: Mention) -> bool:
    left, right = assertion(source, mention.span)
    prefix = normalize(source[left : mention.span[0]])
    return (
        not mention.ambiguous
        and bool(_CURRENT.search(prefix))
        and not (
            _UNSAFE.search(prefix)
            or _CHANGE.search(prefix)
            or _EVENT.search(normalize(source[left:right]))
        )
    )


_FIELD_CUES = {
    "frequency": r"\b(?:frequency|times|daily|twice|once)\b|مره|مرتين|مرات|يومي",
    "timing": r"\b(?:morning|night|evening|bedtime|timing)\b|بالليل|الصبح|مساء|التوقيت",
    "duration": r"\b(?:duration|days?|weeks?|months?)\b|ايام|اسبوع|المده",
    "route": r"\b(?:route|oral|orally|intravenous|iv|subcutaneous)\b|بالفم|وريد|تحت الجلد",
}


def untouched(
    order: OrderCandidate,
    field: str,
    source: str,
    partition: SourcePartition | None = None,
    ctx: Context | None = None,
    item: str = "",
    prior_value: str | None = None,
) -> bool:
    """Inheritance is a field-specific right, independent of prior-value display."""
    from sanad.scribe.amend import order_key

    partition = partition or SourcePartition(original_end=len(source))
    if not partition.valid(source):
        return False
    texts = [(source[: partition.original_end], False)]
    texts.extend(
        (dict(c.answers)[item], True) for c in partition.corrections if item in dict(c.answers)
    )
    for text, is_answer in texts:
        inventory = mentions(text, ctx, partition.lexicon)
        if is_answer and not inventory:
            # The previous proposal's answer slot supplies the subject when the
            # doctor answers a question without repeating the medication name.
            local = normalize(text)
            if _UNSAFE.search(local) or re.search(r"unclear|unsure|not sure|مش متاكد", local):
                return False
            if field == "dose":
                if re.search(r"\bdose\b|جرعه", local) or (
                    quantities(local) and not any(re.search(p, local) for p in _FIELD_CUES.values())
                ):
                    return False
            elif re.search(_FIELD_CUES[field], local):
                from sanad.scribe.grounding import grounded

                if not prior_value or not grounded(prior_value, local):
                    return False
        for mention in inventory:
            if order_key(mention.name) != order_key(order.drug):
                continue
            left, right = assertion(text, mention.span)
            left = max(
                (m.span[1] for m in inventory if left <= m.span[1] <= mention.span[0]), default=left
            )
            right = min(
                (m.span[0] for m in inventory if mention.span[1] <= m.span[0] <= right),
                default=right,
            )
            local = normalize(text[left:right])
            if field == "dose":
                if re.search(r"\bdose\b|جرعه", local) or attached(text, mention, (left, right)):
                    return False
                if _CHANGE.search(local) and (
                    quantities(local) or not any(re.search(p, local) for p in _FIELD_CUES.values())
                ):
                    return False
            elif re.search(_FIELD_CUES[field], local):
                from sanad.scribe.grounding import grounded

                cues = re.findall(_FIELD_CUES[field], local)
                if (
                    not prior_value
                    or not grounded(prior_value, local)
                    or any(not grounded(cue, prior_value) for cue in cues)
                    or _UNSAFE.search(local)
                ):
                    return False
    return True
