"""Exact removal reconstruction and live doctor/session authority at commit."""

from datetime import datetime
from typing import TYPE_CHECKING

from sanad.domain import DRAFT_POLICY_2026_09, PatientScope
from sanad.steward.removal import prepare, terminal_request
from sanad.steward.types import StewardPolicy
from sanad.store.keys import AccountScope
from sanad.store.records import CommitRequest, Doctor, WebSession, from_record

if TYPE_CHECKING:
    from sanad.store._base import Check, StoreBase


def guards(store: "StoreBase", request: CommitRequest, now: datetime) -> list["Check"] | None:
    from sanad.store._base import Check
    from sanad.store.identity import live_snapshot

    command, scope = request.command, request.command.scope
    if not isinstance(scope, PatientScope) or not command.fence:
        return None
    doctor_row = store.get(scope, "doctor", scope.doctor_id)
    if not doctor_row:
        return None
    doctor = from_record(doctor_row, Doctor)
    checks = [Check(doctor_row.key, doctor_row.version)]
    if command.payload.get("type") == "RemovePatient":
        actor = command.principal
        account = AccountScope(bot_id=doctor.telegram_bot_id)
        auth = store.authorize(account.bot_id, actor.subject)
        bound = store.get(account, "subject_binding", actor.subject)
        if (
            actor.actor_kind != "doctor"
            or auth.principal.actor_kind != "doctor"
            or actor.doctor_id != doctor.id
            or actor.auth_epoch != doctor.auth_epoch
            or actor.subject != doctor.telegram_user_id
            or doctor.status != "approved"
            or not bound
            or auth.principal.doctor_id != doctor.id
        ):
            return None
        checks.append(Check(bound.key, bound.version))
        # Removal has exactly one authenticated channel: a live browser session.
        session_row = store.get(account, "web_session", actor.session_id or "")
        if not session_row:
            return None
        session = from_record(session_row, WebSession)
        if (
            session.role != "doctor"
            or session.subject != actor.subject
            or session.revoked_at
            or min(session.idle_expires_at, session.absolute_expires_at) <= now
            or not live_snapshot(store, account, session, checks)
        ):
            return None
        checks.append(Check(session_row.key, session_row.version))
    elif (
        command.principal.actor_kind != "system"
        or not command.worker
        or command.worker.permitted_lanes
        != frozenset(
            {
                "media"
                if command.payload.get("type") == "_RemovedMedia"
                else "ingress"
                if command.payload.get("type") == "_RemovedReceipt"
                else "operational"
            }
        )
    ):
        return None
    try:
        policy = StewardPolicy(DRAFT_POLICY_2026_09)
        expected = (
            terminal_request(store, command, policy)
            if command.payload.get("type") in {"_RemovedMedia", "_RemovedReceipt"}
            else prepare(store, command, command.requested_at, policy)
        )
    except ValueError:
        return None
    if request != expected:
        return None
    for read in request.identity_reads:
        row = store.get(read.scope, read.entity_type, read.id)
        if row is None or row.version != read.version:
            return None
        checks.append(Check(row.key, row.version))
    return checks
