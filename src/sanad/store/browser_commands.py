"""Atomic body reservation; retries recover the same durable inbound command."""

from typing import TYPE_CHECKING

from sanad.store import keys
from sanad.store.identity import live_snapshot
from sanad.store.records import AuthorizationUnavailable, WebSession
from sanad.store.retry import authorization_read

if TYPE_CHECKING:
    from sanad.store._base import StoreBase


@authorization_read
def reserve(store: "StoreBase", session: WebSession, command_id: str, digest: str) -> str:
    from sanad.store._base import Check, Write

    key = keys.Key(
        keys.partition(session.scope), "WEB_COMMAND#" + keys.digest(session.id + ":" + command_id)
    )
    current = store._read(key)
    if current is not None:
        return "existing" if current.get("body_digest") == digest else "conflict"
    snapshot = store.web_session_snapshot(session)
    if snapshot is None:
        return "forbidden"
    if snapshot.doctor_id is None or snapshot.role != session.role:
        return "forbidden"
    session = WebSession.model_validate(snapshot.model_dump())
    checks: list[Check] = []
    row = store.get(session.scope, "web_session", session.id)
    if row is None:
        return "forbidden"
    if row.version != session.version:
        raise AuthorizationUnavailable("browser_reservation_busy")
    if not live_snapshot(store, session.scope, session, checks):
        # Validate the denial as a consistent cut on the next attempt.
        if store.web_session_snapshot(session) is None:
            return "forbidden"
        raise AuthorizationUnavailable("browser_reservation_busy")
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
        raise AuthorizationUnavailable("browser_reservation_busy")
    return "existing" if current.get("body_digest") == digest else "conflict"
