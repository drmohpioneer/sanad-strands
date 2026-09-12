"""Carry the originating browser session in the existing preference transaction."""

from sanad.steward.patient import PatientTurnCommit
from sanad.store.keys import AccountScope
from sanad.store.records import WebSession, from_record, to_record


def advance_preference_session(tx: PatientTurnCommit) -> None:
    if tx.builder.command.payload.get("type") != "SetContactPreference":
        return
    data = tx.receipt.payload or {}
    session_id = data.get("web_session_id")
    if tx.receipt.transport not in {"web-message", "web-preference"} or not isinstance(
        session_id, str
    ):
        return
    scope = AccountScope(bot_id=tx.principal.bot_id or "")
    row = tx.store.get(scope, "web_session", session_id)
    if row is None:
        return  # The store guard refuses the missing originating session.
    session = from_record(row, WebSession)
    tx.builder.command = tx.builder.command.model_copy(
        update={"payload": {**tx.builder.command.payload, "web_session_id": session_id}}
    )
    changed = session.model_copy(
        update={
            "version": session.version + 1,
            "updated_at": tx.now,
            "consent_version": tx.profile.consent_version,
        }
    )
    tx.builder.put(to_record(changed, scope))
