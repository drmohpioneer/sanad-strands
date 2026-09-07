"""Retain explicitly dictated previous/new medication values on one change order."""

import re

from sanad.media.numbers import numbers_in
from sanad.scribe.extract import DictationCandidate, OrderCandidate
from sanad.scribe.names import heard_dose, normalize, split_drug_dose
from sanad.scribe.resolver import Context, generic_key, resolve_name


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
    a, b = set(generic_key(old.generic)), set(generic_key(new.generic))
    if a != b and not (min(len(a), len(b)) >= 2 and (a <= b or b <= a)):
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
