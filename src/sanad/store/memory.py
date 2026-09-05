"""Single-process store with atomic CAS and detached reads; TTL is stored, not run."""

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from threading import RLock

from sanad.store._base import (
    INDEX_FIELDS,
    Check,
    Item,
    StoreBase,
    Write,
    query_identity,
    size_failure,
    utc_now,
)
from sanad.store.keys import Key
from sanad.store.records import Cursor


class MemoryStore(StoreBase):
    def __init__(self, *, clock: Callable[[], datetime] = utc_now):
        super().__init__(clock=clock)
        self._items: dict[Key, Item] = {}
        self._lock = RLock()

    def _read(self, key: Key) -> Item | None:
        with self._lock:
            return deepcopy(self._items.get(key))

    def _atomic(self, writes: list[Write], checks: list[Check]) -> bool:
        if size_failure(writes, checks) is not None:
            return False
        with self._lock:
            for write in writes:
                stored = self._items.get(write.key)
                if write.before is None:
                    if stored is not None:
                        return False
                elif stored is None or stored["version"] != write.before:
                    return False
            if any(
                self._items.get(check.key, {}).get("version") != check.version for check in checks
            ):
                return False
            for write in writes:
                self._items[write.key] = deepcopy(write.item)
            return True

    def _update(self, item: Item, before: int) -> bool:
        return self._atomic([Write(item, before)], [])

    def _query(
        self,
        pk: str,
        *,
        index: str | None = None,
        prefix: str = "",
        through: str | None = None,
        cursor: Cursor | None = None,
        limit: int = 100,
    ) -> tuple[list[Item], Cursor | None]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("page limit must be an integer between 1 and 1000")
        query = query_identity(pk, index, prefix, through)
        if cursor is not None and cursor.query != query:
            return [], None
        pk_field, sk_field = INDEX_FIELDS[index] if index else ("PK", "SK")
        with self._lock:
            items = [
                deepcopy(item)
                for item in self._items.values()
                if item.get(pk_field) == pk
                and sk_field in item
                and item[sk_field].startswith(prefix)
                and (through is None or item[sk_field] <= through)
            ]
        items.sort(key=lambda item: (item[sk_field], item["PK"], item["SK"]))
        if cursor is not None:
            position = cursor.position
            if (
                position.get(pk_field) != pk
                or sk_field not in position
                or set(position) != {"PK", "SK", pk_field, sk_field}
            ):
                return [], None
            start = (position[sk_field], position["PK"], position["SK"])
            items = [item for item in items if (item[sk_field], item["PK"], item["SK"]) > start]
        page = items[:limit]
        if len(page) < limit:
            return page, None
        last = page[-1]
        return page, Cursor(
            query=query, position={field: last[field] for field in {"PK", "SK", pk_field, sk_field}}
        )
