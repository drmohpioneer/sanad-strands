"""Exact correction write-set reconstruction and independent browser authority."""

from datetime import datetime
from typing import TYPE_CHECKING

from sanad.domain import PatientScope, Principal, TenantScope
from sanad.domain.entities import DRAFT_POLICY_2026_09
from sanad.steward.apply import CommitBuilder
from sanad.steward.types import StewardPolicy
from sanad.store.keys import AccountScope
from sanad.store.records import AuditEvent, CommitRequest, Doctor, WebSession, from_record

if TYPE_CHECKING:
    from sanad.store._base import Check, StoreBase
    from sanad.store.protocol import Store


def doctor_checks(
    store: "Store", actor: Principal, scope: PatientScope, now: datetime
) -> list["Check"] | None:
    from sanad.store._base import Check
    from sanad.store.reviews import doctor_checks as telegram_checks

    if actor.session_id is None:
        return telegram_checks(store, actor, scope.doctor_id)
    if (
        actor.actor_kind != "doctor"
        or actor.doctor_id != scope.doctor_id
        or "doctor" not in actor.verified_roles
    ):
        return None
    row = store.get(TenantScope(doctor_id=scope.doctor_id), "doctor", scope.doctor_id)
    if not row:
        return None
    doctor = from_record(row, Doctor)
    session_row = store.get(
        AccountScope(bot_id=doctor.telegram_bot_id), "web_session", actor.session_id
    )
    if not session_row:
        return None
    session = from_record(session_row, WebSession)
    if (
        session.role != "doctor"
        or session.doctor_id != scope.doctor_id
        or session.subject != actor.subject
        or session.auth_epoch != actor.auth_epoch
        or session.revoked_at
        or min(session.idle_expires_at, session.absolute_expires_at) <= now
    ):
        return None
    checks = telegram_checks(store, actor.model_copy(update={"session_id": None}), scope.doctor_id)
    return [*checks, Check(session_row.key, session_row.version)] if checks else None


def guards(store: "StoreBase", request: CommitRequest, now: datetime) -> list["Check"] | None:
    from sanad.steward.corrections import prepare

    command = request.command
    if not isinstance(command.scope, PatientScope) or not command.fence or command.worker:
        return None
    checks = doctor_checks(store, command.principal, command.scope, now)
    if checks is None or not request.events:
        return None
    if store.lookup_command(command) is not None:
        return checks
    at = from_record(request.events[0], AuditEvent).accepted_at
    if not command.requested_at <= at <= now:
        return None
    try:
        expected = prepare(
            CommitBuilder(command.scope, command, at, StewardPolicy(DRAFT_POLICY_2026_09), store)
        )
    except ValueError:
        return None
    if expected != request:
        return None
    return checks
