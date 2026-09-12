"""Atomic body reservation; retries recover the same durable inbound command."""

from typing import TYPE_CHECKING

from sanad.store import keys
from sanad.store.identity import live_snapshot
from sanad.store.records import WebSession

if TYPE_CHECKING:
    from sanad.store._base import StoreBase


def reserve(store: "StoreBase", session: WebSession, command_id: str, digest: str) -> str:
    from sanad.store._base import Check, Write

    key = keys.Key(
        keys.partition(session.scope), "WEB_COMMAND#" + keys.digest(session.id + ":" + command_id)
    )
    current = store._read(key)
    if current is not None:
        return "existing" if current.get("body_digest") == digest else "conflict"
    checks: list[Check] = []
    row = store.get(session.scope, "web_session", session.id)
    if (
        row is None
        or row.version != session.version
        or not live_snapshot(store, session.scope, session, checks)
    ):
        return "forbidden"
    now = store._clock()
    if session.revoked_at or min(session.idle_expires_at, session.absolute_expires_at) <= now:
        return "forbidden"
    checks.append(Check(row.key, row.version))
    item = {
        "PK": key.pk,
        "SK": key.sk,
        "version": 1,
        "body_digest": digest,
        "ttl": int(session.absolute_expires_at.timestamp()),
    }
    if store._atomic([Write(item, None)], checks):
        return "created"
    current = store._read(key)
    if current is None:
        return "forbidden"
    return "existing" if current.get("body_digest") == digest else "conflict"
