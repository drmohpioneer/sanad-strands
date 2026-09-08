"""Retain explicitly dictated previous/new medication values on one change order."""

import re

from sanad.media.numbers import numbers_in
from sanad.scribe.extract import DictationCandidate, OrderCandidate
from sanad.scribe.names import dictionary, heard_dose, normalize, split_drug_dose
from sanad.scribe.resolver import Context, generic_key, resolve_name


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
            r"(?:increase|decrease|chang\w*|switch|زود\w*|قلل\w*|غير\w*)"
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


def previous_instruction(order: OrderCandidate, source: str) -> OrderCandidate | None:
    if order.action != "change" or not order.previous_drug or not order.previous_dose:
        return None
    if not re.search(
        r"(?:increase|decrease|chang\w*|switch|زود\w*|قلل\w*|غير\w*)[^.;\n]{0,100}\b"
        + re.escape(split_drug_dose(order.drug)[0])
        + r"\b",
        source,
        re.I,
    ):
        return None
    old = resolve_name(order.previous_drug, "drug", source)
    new = resolve_name(split_drug_dose(order.drug)[0], "drug", source, order.name_latin)
    if not old.latin or not new.latin or not old.generic or not new.generic:
        return None
    if not _same_family(old.generic, new.generic):
        return None
    heard = heard_dose(order.previous_drug, source)
    if not heard or numbers_in(heard) != numbers_in(order.previous_dose):
        return None
    if normalize(order.drug) not in normalize(source):
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
    return candidate.model_copy(
        update={"orders": tuple(o for i, o in enumerate(orders) if i not in removed)}
    )
