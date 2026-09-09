"""A closed set of code sentences from the exact displayed review snapshot."""

from dataclasses import dataclass

from sanad.domain import ReviewObligation
from sanad.liaison import templates
from sanad.presentation.context import PresentationContext
from sanad.store.keys import digest


@dataclass(frozen=True)
class Fact:
    id: str
    value: str


@dataclass(frozen=True)
class Permitted:
    source_id: str
    facts: tuple[Fact, ...]


def compute(
    notice_id: str, reviews: tuple[ReviewObligation, ...], context: PresentationContext
) -> Permitted:
    keys = ["fact_delivery"]
    if any(r.state == "open" for r in reviews):
        keys.append("fact_open")
    if any(r.state == "acknowledged" for r in reviews):
        keys.append("fact_ack")
    basis = repr(tuple((r.id, r.version, r.last_material_change_version) for r in reviews))
    return Permitted(
        notice_id,
        tuple(
            Fact(digest(notice_id + basis + key), templates.render(key, context)) for key in keys
        ),
    )
