"""Scoped vocabulary reads and atomic confirmation effects; never clinical truth."""

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from sanad.domain import TenantScope
from sanad.scribe.names import NameEntry
from sanad.store.keys import AccountScope
from sanad.store.protocol import Store
from sanad.store.records import Doctor, NameMemory, from_record

if TYPE_CHECKING:
    from sanad.scribe.proposal import Proposal


def memory_rows(store: Store, scope: TenantScope | AccountScope) -> tuple[NameMemory, ...]:
    if type(scope) is not TenantScope and type(scope) is not AccountScope:
        raise ValueError("vocabulary reads require exact doctor or clinic scope")
    rows: list[NameMemory] = []
    cursor = None
    while True:
        page, cursor = store.list_records(scope, "name_memory", cursor=cursor)
        rows.extend(from_record(r, NameMemory) for r in page)
        if cursor is None:
            return tuple(
                sorted(
                    rows, key=lambda r: (-r.confirmations, -r.last_confirmed_at.timestamp(), r.id)
                )
            )


class NameVocabulary:
    """One request's cached scoped read; a later request observes new confirmations."""

    def __init__(self, store: Store, doctor: Doctor):
        self.store, self.doctor = store, doctor
        self._rows: tuple[tuple[NameMemory, ...], tuple[NameMemory, ...]] | None = None

    @property
    def rows(self) -> tuple[tuple[NameMemory, ...], tuple[NameMemory, ...]]:
        if self._rows is None:
            self._rows = (
                memory_rows(self.store, self.doctor.scope),
                memory_rows(self.store, AccountScope(bot_id=self.doctor.telegram_bot_id)),
            )
        return self._rows

    def entries(self, kind: str = "drug") -> tuple[NameEntry, ...]:
        return tuple(
            NameEntry(
                kind="drug" if r.kind == "drug" else "term",
                latin=r.latin,
                generic=r.generic,
                arabic_spellings=tuple(s for s in r.spoken_forms if not s.isascii()),
                latin_spellings=tuple(s for s in r.spoken_forms if s.isascii()),
                fixed_combination_strengths=tuple(s for s in r.strengths_seen if "/" in s),
            )
            for tier in self.rows
            for r in tier
            if r.kind == kind or (kind == "term" and r.kind != "drug")
        )

    def find(self, name: str, kind: str = "drug") -> NameMemory | None:
        from sanad.scribe.resolver import find_memory

        return find_memory(self, name, kind)

    def hint(self) -> str:
        from sanad.scribe.resolver import hint_names

        return hint_names(self)


def confirmation_names(
    store: Store, doctor: Doctor, proposal: "Proposal", clock: Callable[[], datetime]
) -> tuple[NameMemory, ...]:
    from sanad.scribe.resolver import learn

    return learn(store, doctor, proposal, clock)
