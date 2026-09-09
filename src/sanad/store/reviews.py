"""Commit-time authentication and exact write-set checks for review offers/actions."""

from datetime import datetime
from typing import TYPE_CHECKING

from sanad.domain import PatientScope, Principal, TenantScope
from sanad.liaison.records import Notice
from sanad.store.keys import AccountScope, IntakeScope
from sanad.store.records import CommitRequest, Doctor, DoctorAuthority, OutboundIntent, from_record

if TYPE_CHECKING:
    from sanad.store._base import Check, StoreBase
    from sanad.store.protocol import Store

COMMANDS = {"AcknowledgeReview", "ResolveReview"}


def doctor_checks(store: "Store", actor: Principal, doctor_id: str) -> list["Check"] | None:
    from sanad.store._base import Check

    if (
        actor.actor_kind != "doctor"
        or "doctor" not in actor.verified_roles
        or actor.doctor_id != doctor_id
    ):
        return None
    scope = TenantScope(doctor_id=doctor_id)
    row = store.get(scope, "doctor", doctor_id)
    authority_row = store.get(scope, "doctor_authority", doctor_id)
    if not row or not authority_row:
        return None
    doctor, authority = from_record(row, Doctor), from_record(authority_row, DoctorAuthority)
    auth = store.authorize(doctor.telegram_bot_id, actor.subject)
    if (
        auth.principal != actor
        or not auth.binding
        or doctor.status != "approved"
        or not authority.approved
        or doctor.auth_epoch != actor.auth_epoch
        or authority.auth_epoch != actor.auth_epoch
        or doctor.telegram_user_id != actor.subject
        or authority.subject != actor.subject
        or doctor.telegram_bot_id != actor.bot_id
    ):
        return None
    bound = store.get(AccountScope(bot_id=doctor.telegram_bot_id), "subject_binding", actor.subject)
    if not bound:
        return None
    return [Check(r.key, r.version) for r in (row, authority_row, bound)]


def guards(store: "StoreBase", request: CommitRequest, now: datetime) -> list["Check"] | None:
    from sanad.steward.reviews import ReviewRefused, load_offer, prepare
    from sanad.store._base import Check

    command = request.command
    if type(command.scope) not in {TenantScope, IntakeScope, PatientScope} or command.worker:
        return None
    assert isinstance(command.scope, TenantScope)
    actor = command.principal
    checks = doctor_checks(store, actor, command.scope.doctor_id)
    if checks is None or not store._identity or actor.bot_id != store._identity.bot_id:
        return None
    if len(request.events) != 1:
        return None
    from sanad.store.records import AuditEvent

    recorded_at = from_record(request.events[0], AuditEvent).accepted_at
    if recorded_at > now or recorded_at < command.requested_at:
        return None
    try:
        offer = load_offer(store, command)
        if offer.expires_at <= now:
            return None
        expected = prepare(store, command, recorded_at)
    except (ReviewRefused, ValueError):
        return None
    if request != expected:
        return None  # Includes exact reason, audit, consumption, no hidden writes/effects.
    if (
        offer.doctor_subject != actor.subject
        or offer.bot_id != actor.bot_id
        or offer.auth_epoch != actor.auth_epoch
    ):
        return None
    s = offer.snapshot
    if type(command.scope) is TenantScope and not (s.source_type == "outbound_intent"):
        return None
    row = store.get(command.scope, "review", s.review_ref.id)
    if not row or (
        type(command.scope) is TenantScope and row.body.get("review_kind") != "delivery_failure"
    ):
        return None
    if type(command.scope) is IntakeScope and (
        s.source_type != "intake" or s.source_id != command.scope.intake_id
    ):
        return None
    if type(command.scope) is not PatientScope and (
        command.fence
        or any(
            getattr(command, f) is not None
            for f in (
                "expected_binding_epoch",
                "expected_consent_version",
                "expected_delivery_epoch",
                "expected_safety_epoch",
            )
        )
    ):
        return None
    notice_row = store.get(offer.scope, "liaison_notice", offer.notice_id)
    if not notice_row:
        return None
    notice = from_record(notice_row, Notice)
    intent_row = store.get(notice.intent_scope, "outbound_intent", notice.intent_id)
    if not intent_row:
        return None
    intent = from_record(intent_row, OutboundIntent)
    if (
        intent.status not in {"provider_accepted", "uncertain"}
        or intent.active_attempt_id != notice.attempt_id
    ):
        return None
    checks.extend(Check(r.key, r.version) for r in (row, notice_row, intent_row))
    from sanad.domain import ReviewObligation
    from sanad.liaison.snapshot import source_row

    source = source_row(store, from_record(row, ReviewObligation))
    if source:
        checks.append(Check(source.key, source.version))
    return checks
