"""Retain explicitly dictated previous/new medication values on one change order."""

import logging
import re
from typing import TYPE_CHECKING

from sanad.scribe.change_binding import SourcePartition, authority_matches, bind_change

if TYPE_CHECKING:
    from sanad.scribe.proposal import Proposal

from sanad.media.numbers import numbers_in
from sanad.scribe.extract import DictationCandidate, OrderCandidate
from sanad.scribe.names import dictionary, normalize, split_drug_dose
from sanad.scribe.resolver import Context, generic_key, resolve_name

logger = logging.getLogger(__name__)


def _bare_continue(order: OrderCandidate) -> bool:
    return (
        order.action == "continue"
        and not split_drug_dose(order.drug)[1]
        and not any(
            getattr(order, field)
            for field in (
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
        )
    )


def _without_orders(
    candidate: DictationCandidate, removed: set[int]
) -> tuple[DictationCandidate, dict[str, tuple[str, ...]]]:
    if not removed:
        return candidate, {}
    kept = [i for i in range(len(candidate.orders)) if i not in removed]
    targets: dict[str, tuple[str, ...]] = {f"order:{i}": () for i in removed}
    targets.update({f"order:{old}": (f"order:{new}",) for new, old in enumerate(kept)})
    edits = []
    for edit in candidate.correction_edits:
        if not edit.item.startswith("order:"):
            edits.append(edit)
            continue
        incoming = f"order:{edit.proposal_index}"
        for target in targets.get(incoming, (incoming,)):
            # item addresses the previous card; only the incoming index moves.
            edits.append(edit.model_copy(update={"proposal_index": int(target.split(":")[1])}))
    result = candidate.model_copy(
        update={
            "orders": tuple(candidate.orders[i] for i in kept),
            "correction_edits": tuple(edits),
        }
    )
    from sanad.scribe.merge import remap_metadata

    remap_metadata(result, targets)
    return result, targets


def drop_bare_continues(
    candidate: DictationCandidate,
    source: str,
    ctx: Context,
    *,
    partition: SourcePartition | None = None,
    previous: "Proposal | None" = None,
) -> tuple[DictationCandidate, dict[str, tuple[str, ...]]]:
    """An existing instruction owns its drug line, including a verified prior brand."""

    def identity(order: OrderCandidate) -> str:
        spoken = split_drug_dose(order.drug)[0]
        resolved = resolve_name(spoken, "drug", source, order.name_latin, ctx)
        return normalize(resolved.latin or spoken)

    instructed = set()
    for index, order in enumerate(candidate.orders):
        if order.action in {"start", "change", "stop"}:
            instructed.add(identity(order))
            prior = previous_instruction(
                order,
                source,
                partition=partition,
                previous=previous,
                ctx=ctx,
                item=f"order:{index}",
            )
            if prior:
                instructed.add(identity(prior))
    removed = {
        i
        for i, order in enumerate(candidate.orders)
        if _bare_continue(order) and identity(order) in instructed
    }
    if not removed:
        return candidate, {}
    result, targets = _without_orders(candidate, removed)
    logger.info("scribe_bare_continue_dropped count=%d", len(removed))
    return result, targets


def _same_family(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    a, b = set(generic_key(left)), set(generic_key(right))
    return a == b or (min(len(a), len(b)) >= 2 and (a <= b or b <= a))


def _source_brand(order: OrderCandidate, source: str, ctx: Context) -> OrderCandidate:
    base = resolve_name(split_drug_dose(order.drug)[0], "drug", source, ctx=ctx)
    if not base.latin or not base.generic:
        return order
    names = {e.latin for e in dictionary() if e.kind == "drug"}
    if ctx.vocabulary:
        names.update(r.latin for rows in ctx.vocabulary.rows for r in rows if r.kind == "drug")
    names.update(r.canonical for r in ctx.lookups.values() if r.found)
    matches = []
    for name in names:
        if not normalize(name).startswith(normalize(base.latin) + " "):
            continue
        target = re.search(
            r"(?:increase|decrease|upgrade|chang\w*|switch|زود\w*|قلل\w*|غير\w*)"
            r"[^.;\n]{0,100}?(?:\bto\s+(?:be\s+)?|لـ?\s*)" + re.escape(name) + r"(?!\w)\s+\d",
            source,
            re.I,
        )
        resolved = resolve_name(name, "drug", source, ctx=ctx)
        if target and _same_family(base.generic, resolved.generic):
            matches.append(resolved)
    if len(matches) != 1:
        return order
    resolved = matches[0]
    return order.model_copy(
        update={"drug": resolved.latin, "name_latin": resolved.latin, "generic": resolved.generic}
    )


def previous_instruction(
    order: OrderCandidate,
    source: str,
    *,
    partition: SourcePartition | None = None,
    previous: "Proposal | None" = None,
    ctx: Context | None = None,
    item: str | None = None,
) -> OrderCandidate | None:
    if order.action != "change" or not order.previous_drug or not order.previous_dose:
        return None
    if not authority_matches(partition, source, previous):
        return None
    binding = bind_change(order, source, partition, ctx, item)
    if binding is None or binding.previous_dose is None:
        return None
    old = resolve_name(order.previous_drug, "drug", source, ctx=ctx)
    new = resolve_name(split_drug_dose(order.drug)[0], "drug", source, order.name_latin, ctx)
    if not old.latin or not new.latin or not _same_family(old.generic, new.generic):
        return None
    return OrderCandidate(action="continue", drug=old.latin, dose=order.previous_dose)


def combine_changes(candidate: DictationCandidate, source: str, ctx: Context) -> DictationCandidate:
    orders = list(candidate.orders)
    removed: set[int] = set()
    for i, order in enumerate(orders):
        if order.action != "change":
            continue
        if order.previous_drug:
            name, suffix = split_drug_dose(order.previous_drug)
            if suffix and (
                not order.previous_dose or numbers_in(suffix) == numbers_in(order.previous_dose)
            ):
                order = order.model_copy(
                    update={"previous_drug": name, "previous_dose": order.previous_dose or suffix}
                )
        order = _source_brand(order, source, ctx)
        orders[i] = order
        if not order.previous_drug:
            matches = []
            for j, before in enumerate(orders):
                if i == j or before.action != "continue":
                    continue
                proposed = order.model_copy(
                    update={
                        "previous_drug": split_drug_dose(before.drug)[0],
                        "previous_dose": before.dose or split_drug_dose(before.drug)[1],
                    }
                )
                if previous_instruction(proposed, source):
                    matches.append((j, proposed))
            if len(matches) == 1:
                j, order = matches[0]
                removed.add(j)
                orders[i] = order
        prior = previous_instruction(order, source)
        if prior:
            for j, other in enumerate(orders):
                resolved = resolve_name(
                    split_drug_dose(other.drug)[0], "drug", source, other.name_latin, ctx
                )
                if j != i and other.action == "continue" and resolved.latin == prior.drug:
                    removed.add(j)
    count = sum(_bare_continue(orders[i]) for i in removed)
    if count:
        logger.info("scribe_bare_continue_dropped count=%d", count)
    result, _ = _without_orders(candidate.model_copy(update={"orders": tuple(orders)}), removed)
    return drop_bare_continues(result, source, ctx)[0]
