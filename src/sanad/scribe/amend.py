"""Versioned medication diffs computed from the doctor's current scoped record."""

import re
from typing import TYPE_CHECKING

from sanad.domain import PatientScope
from sanad.domain.boundaries import _BoundaryValue
from sanad.scribe.change_binding import SourcePartition, authority_matches, untouched
from sanad.scribe.extract import DictationCandidate, FactCandidate, OrderCandidate, ProposalIssue
from sanad.store import keys

if TYPE_CHECKING:
    from sanad.scribe.proposal import Proposal
    from sanad.scribe.records import CareOrderHead
    from sanad.scribe.repository import ScribeRepository

FIELDS = ("dose", "frequency", "route", "timing", "duration")


def order_key(name: str) -> str:
    from sanad.scribe.card import plain
    from sanad.scribe.names import entry_for

    entry = entry_for(name)
    return keys.digest("medication:" + plain(entry.latin if entry else name).casefold())


def find_head(repo: "ScribeRepository", scope: PatientScope, name: str) -> "CareOrderHead | None":
    """Keep pre-11b identities when a known spelling now renders in Latin."""
    from sanad.scribe.records import CareOrderHead
    from sanad.store.records import from_record

    cursor = None
    matches: list[CareOrderHead] = []
    while True:
        rows, cursor = repo.store.list_records(scope, "care_order_head", cursor=cursor)
        matches.extend(
            from_record(r, CareOrderHead)
            for r in rows
            if order_key(str(r.body["name"])) == order_key(name)
        )
        if cursor is None:
            break
    return matches[0] if len(matches) == 1 else None


class OrderChange(_BoundaryValue):
    item: str
    old: OrderCandidate | None = None
    new: OrderCandidate
    head_version: int | None = None
    noop: bool = False
    note: str | None = None


def prepare(
    repo: "ScribeRepository",
    scope: PatientScope | None,
    candidate: DictationCandidate,
    *,
    creating: bool = False,
    source: str = "",
    partition: SourcePartition | None = None,
    previous: "Proposal | None" = None,
) -> tuple[DictationCandidate, tuple[OrderChange, ...], tuple[ProposalIssue, ...]]:
    from sanad.scribe.records import CareOrderVersion

    if scope is None and not creating:
        return candidate, (), ()

    if not authority_matches(partition, source, previous):
        return candidate, (), (ProposalIssue(item="all", code="clinical_unclear"),)

    orders, changes, issues = [], [], []
    facts = list(candidate.facts)
    dropped = list(candidate._dropped_numbers)
    for i, supplied in enumerate(candidate.orders):
        from sanad.media.numbers import numbers_in
        from sanad.scribe.changes import previous_instruction

        stated_previous = previous_instruction(
            supplied, source, partition=partition, previous=previous, item=f"order:{i}"
        )
        head = (
            find_head(repo, scope, stated_previous.drug if stated_previous else supplied.drug)
            if scope
            else None
        )
        version = (
            repo.load(scope, "care_order_version", head.current_version_id, CareOrderVersion)
            if scope and head
            else None
        )
        old = (
            version.structured_instruction
            if version and isinstance(version.structured_instruction, OrderCandidate)
            else None
        )
        if (
            old
            and head
            and head.status == "active"
            and supplied.action == "change"
            and not supplied.previous_dose
            and supplied.previous_drug
            and order_key(supplied.previous_drug) == order_key(old.drug)
        ):
            # The stored amendment carries the prior value and its version provenance.
            # A model's duplicate name is not a spoken prior instruction.
            supplied = supplied.model_copy(update={"previous_drug": None})
        if not stated_previous and (supplied.previous_drug or supplied.previous_dose):
            dropped.extend(
                numbers_in((supplied.previous_drug or "") + " " + (supplied.previous_dose or ""))
            )
            issues.append(
                ProposalIssue(item=f"order:{i}", code="clinical_unclear", field="previous_dose")
            )
        order, note, noop = supplied, None, False
        if old and head:
            if supplied.action in {"change", "continue"}:
                same_drug = order_key(supplied.drug) == order_key(old.drug)
                values = {
                    field: getattr(supplied, field)
                    or (
                        getattr(old, field)
                        if same_drug
                        and untouched(
                            supplied,
                            field,
                            source,
                            partition,
                            item=f"order:{i}",
                            prior_value=getattr(old, field),
                        )
                        else None
                    )
                    for field in FIELDS
                }
                for field in FIELDS:
                    if field != "dose" and getattr(old, field) and not values[field] and same_drug:
                        issues.append(
                            ProposalIssue(item=f"order:{i}", code="clinical_unclear", field=field)
                        )
                noop = (
                    same_drug
                    and supplied.action == "continue"
                    and head.status == "active"
                    and all(values[field] == getattr(old, field) for field in FIELDS)
                    and not supplied.effective_expression
                    and not supplied.checkin_expression
                )
                order = supplied.model_copy(
                    update={**values, "action": "continue" if noop else "change"}
                )
            elif supplied.action == "start" and head.status == "active":
                order = supplied.model_copy(update={"action": "change"})
        elif supplied.action in {"stop", "change"} and not (creating and stated_previous):
            note = (
                "doctor instructed, not on file before"
                if supplied.action == "stop"
                else "previous dose not on file"
            )
            from sanad.scribe.grounding import _action_at, name_spans
            from sanad.scribe.resolver import Context

            quote = supplied.action_quote
            if quote is None:
                for span in name_spans(supplied.drug, source, "drug", Context()):
                    action = _action_at(source, span)
                    if action and action[0] == supplied.action and not action[2]:
                        quote = source[action[1][0] : action[1][1]]
            order = supplied.model_copy(
                update={
                    "action": "start" if supplied.action == "change" else "stop",
                    "action_quote": quote,
                }
            )
            history = home_history(order, note, source, partition)
            if history and not any(f.text == history for f in facts):
                facts.append(FactCandidate(category="medication_history", text=history))
        orders.append(order)
        if head or note or stated_previous or supplied.action in {"stop", "continue"}:
            changes.append(
                OrderChange(
                    item=f"order:{i}",
                    old=None if note and not head else old or stated_previous,
                    new=order,
                    head_version=head.version if head else None,
                    noop=noop,
                    note=note,
                )
            )
    result = candidate.model_copy(update={"orders": tuple(orders), "facts": tuple(facts)})
    result._dropped_numbers = tuple(dict.fromkeys(dropped))
    return result, tuple(changes), tuple(issues)


def home_history(
    order: OrderCandidate, note: str | None, source: str, partition: SourcePartition | None = None
) -> str | None:
    """A deterministic description of an instruction, never evidence of prior use."""
    from sanad.scribe.grounding import _action_at, _cited_action, name_spans
    from sanad.scribe.resolver import Context

    if note not in {"doctor instructed, not on file before", "previous dose not on file"}:
        return None
    action = "stop" if note == "doctor instructed, not on file before" else "change"
    from sanad.scribe.change_binding import mentions

    spans = name_spans(order.drug, source, "drug", Context()) + tuple(
        mention.span
        for mention in mentions(source, lexicon=partition.lexicon if partition else ())
        if not mention.ambiguous and order_key(mention.name) == order_key(order.drug)
    )
    for drug in dict.fromkeys(spans):
        cited = _cited_action(order, source, drug) if order.action_quote else None
        literal = _action_at(source, drug) if not order.action_quote else None
        if (cited and not cited[1]) or (literal and not literal[2] and literal[0] == action):
            quote = order.action_quote or (source[literal[1][0] : literal[1][1]] if literal else "")
            verb = "hold" if action == "stop" and re.search(r"\bhold\b", quote, re.I) else action
            return f"{order.drug}: doctor instructed {verb}; no prior order on file; dose unknown"
    return None


def history_source(proposal: "Proposal", text: str) -> OrderChange | None:
    return next(
        (
            change
            for change in proposal.amendments
            if change.old is None
            and change.head_version is None
            and home_history(
                change.new, change.note, proposal.source_text, proposal.source_partition
            )
            == text
        ),
        None,
    )


def instruction_line(order: OrderCandidate) -> str:
    from sanad.scribe.card import plain

    return " ".join(plain(v) for name in FIELDS if (v := getattr(order, name)))


def diff_lines(changes: tuple[OrderChange, ...], language: str = "ar") -> tuple[str, ...]:
    from sanad.channels.telegram import wording
    from sanad.scribe.card import plain

    result = []
    for change in changes:
        name = plain(change.new.drug)
        if change.noop:
            result.append(name + wording.label("unchanged", language))
        elif change.old:
            new = (
                wording.label("stop", language)
                if change.new.action == "stop"
                else instruction_line(change.new)
            )
            if change.new.action == "change" and order_key(change.old.drug) != order_key(
                change.new.drug
            ):
                result.append(
                    wording.render(
                        "scribe_brand_change_line",
                        language,
                        old_drug=plain(change.old.drug),
                        old=instruction_line(change.old),
                        new_drug=name,
                        new=new,
                    )
                )
                continue
            result.append(
                wording.render(
                    "scribe_amendment_line",
                    language,
                    drug=name,
                    old=instruction_line(change.old),
                    new=new,
                )
            )
        if change.note:
            result.append(name + ": " + change.note)
    return tuple(result)
