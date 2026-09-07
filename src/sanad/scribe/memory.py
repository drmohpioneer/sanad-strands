"""Scoped vocabulary reads and atomic confirmation effects; never clinical truth."""

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from sanad.auth.service import revise
from sanad.domain import TenantScope
from sanad.scribe.names import NameEntry, normalize
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.store import keys
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
        for tier in self.rows:
            matches = [
                r
                for r in tier
                if r.kind == kind
                and normalize(name) in {normalize(s) for s in (r.latin, *r.spoken_forms)}
            ]
            if matches:
                identities = {normalize(r.generic if kind == "drug" else r.latin) for r in matches}
                return matches[0] if len(identities) == 1 else None
        return None

    def hint(self) -> str:
        from sanad.scribe.names import known_names

        return known_names(tuple(r.latin for tier in self.rows for r in tier))


def confirmation_names(
    store: Store,
    doctor: Doctor,
    proposal: "Proposal",
    clock: Callable[[], datetime],
) -> tuple[NameMemory, ...]:
    """Compile both tiers together; the caller puts every row in ScribeConfirm."""
    now = clock()
    result: dict[tuple[str, str], NameMemory] = {}
    for name in proposal.names:
        if name.kind == "finding":
            from sanad.scribe.card import plain
            from sanad.scribe.terms import vocabulary_term

            name = name.model_copy(
                update={
                    "spoken": vocabulary_term(plain(name.spoken)),
                    "latin": vocabulary_term(plain(name.latin)),
                }
            )
        if proposal.blocked(name.item) or not name.latin or not name.learnable:
            continue
        for scope in (doctor.scope, AccountScope(bot_id=doctor.telegram_bot_id)):
            id = keys.digest(keys.partition(scope) + ":" + name.kind + ":" + normalize(name.latin))
            key = (keys.partition(scope), id)
            current = result.get(key)
            if current is None:
                row = store.get(scope, "name_memory", id)
                current = from_record(row, NameMemory) if row else None
            spoken = tuple(dict.fromkeys((*(current.spoken_forms if current else ()), name.spoken)))
            strengths = tuple(
                dict.fromkeys((*(current.strengths_seen if current else ()), *name.strengths))
            )
            values = {
                "latin": name.latin,
                "generic": name.generic,
                "spoken_forms": spoken[-DRAFT_SCRIBE_POLICY.spoken_forms_max :],
                "strengths_seen": strengths,
                "last_confirmed_at": now,
                "source": "doctor_confirmation",
            }
            # A name appearing twice on one card is one confirmation, not two votes.
            if key in result:
                result[key] = NameMemory.model_validate(current.model_dump() | values)  # type: ignore[union-attr]
            elif current:
                result[key] = revise(
                    current, now, confirmations=current.confirmations + 1, **values
                )
            else:
                result[key] = NameMemory.model_validate(
                    {
                        "id": id,
                        "scope": scope,
                        "kind": name.kind,
                        "created_at": now,
                        "updated_at": now,
                        **values,
                    }
                )
    return tuple(result.values())
