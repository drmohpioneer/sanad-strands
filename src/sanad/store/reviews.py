"""Commit-time authentication and exact write-set checks for review offers/actions."""

from datetime import datetime
from typing import TYPE_CHECKING

from sanad.domain import PatientScope, Principal, TenantScope
from sanad.liaison.records import Notice
from sanad.store.keys import AccountScope, IntakeScope
from sanad.store.records import (
    CommitRequest,
    Doctor,
    DoctorAuthority,
    OutboundIntent,
    StoredRecord,
    WebSession,
    from_record,
    to_record,
)

if TYPE_CHECKING:
    from sanad.domain import ReviewObligation
    from sanad.liaison.records import ReviewSnapshot
    from sanad.store._base import Check, StoreBase
    from sanad.store.protocol import Store

COMMANDS = {"AcknowledgeReview", "ResolveReview"}


def doctor_checks(store: "Store", actor: Principal, doctor_id: str) -> list["Check"] | None:
    from sanad.store._base import Check

    if actor.session_id is not None:
        session = browser_session(store, actor, doctor_id)
        if session is None:
            return None
        checks = doctor_checks(store, actor.model_copy(update={"session_id": None}), doctor_id)
        row = store.get(session.scope, "web_session", session.id)
        if checks is None or row is None or row.version != session.version:
            return None
        return [*checks, Check(row.key, row.version)]
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


def browser_session(
    store: "Store", actor: Principal, doctor_id: str, now: datetime | None = None
) -> WebSession | None:
    if not actor.session_id or not actor.bot_id:
        return None
    row = store.get(AccountScope(bot_id=actor.bot_id), "web_session", actor.session_id)
    if row is None:
        return None
    session = from_record(row, WebSession)
    if (
        session.role != "doctor"
        or session.doctor_id != doctor_id
        or session.subject != actor.subject
        or session.auth_epoch != actor.auth_epoch
        or session.revoked_at is not None
        or (now is not None and min(session.idle_expires_at, session.absolute_expires_at) <= now)
    ):
        return None
    return session


def listing_guards(
    store: "StoreBase", request: CommitRequest, now: datetime
) -> list["Check"] | None:
    from sanad.domain import ReviewObligation
    from sanad.steward.reviews import ReviewRefused, load_listing, prepare_listing
    from sanad.store._base import Check
    from sanad.store.records import AuditEvent

    command = request.command
    if type(command.scope) is not PatientScope or command.worker or not command.fence:
        return None
    checks = doctor_checks(store, command.principal, command.scope.doctor_id)
    session = browser_session(store, command.principal, command.scope.doctor_id, now)
    if checks is None or session is None or len(request.events) != 1:
        return None
    at = from_record(request.events[0], AuditEvent).accepted_at
    if not command.requested_at <= at <= now:
        return None
    try:
        listing, selected = load_listing(store, command, now)
        review_row = store.get(command.scope, "review", selected.review_ref.id)
        if review_row is None:
            return None
        observed, source_checks = listing_source_observation(
            store, from_record(review_row, ReviewObligation)
        )
        if request != prepare_listing(store, command, at, observed=(review_row, observed)):
            return None
    except (ReviewRefused, ValueError):
        return None
    listing_row = to_record(listing, listing.scope)
    checks.extend(Check(r.key, r.version) for r in (listing_row, review_row))
    checks.extend(source_checks)
    return checks


def listing_source_observation(
    store: "Store", review: "ReviewObligation"
) -> tuple["ReviewSnapshot", list["Check"]]:
    """Capture one source observation for both validation and transaction fencing."""
    from sanad.liaison.records import ReviewSnapshot
    from sanad.liaison.snapshot import source_row
    from sanad.store.records import model_scope

    source = source_row(store, review)
    scope = model_scope(review)
    assert isinstance(scope, TenantScope)
    observed = ReviewSnapshot(
        scope=scope,
        review_ref=to_record(review, scope).ref,
        source_type=review.source_type,
        source_id=review.source_id,
        source_version=review.source_version,
        material_version=review.last_material_change_version,
        source_ref=source.ref if source else None,
    )
    return observed, listing_source_checks(review, source)


def listing_source_checks(review: "ReviewObligation", source: StoredRecord | None) -> list["Check"]:
    """Build conditions only from the captured source, including explicit absence."""
    from sanad.store import keys
    from sanad.store._base import Check
    from sanad.store.records import model_scope

    if source:
        return [Check(source.key, source.version)]
    scope = model_scope(review)
    kind, id = review.source_type, review.source_id
    if kind == "slot" and review.source_mission_id:
        kind, id = "mission", review.source_mission_id
    if kind == "evidence" and isinstance(scope, PatientScope):
        return [Check(keys.evidence(scope, id, review.source_version), None)]
    prefixes = {
        "mission": "MISSION",
        "followup": "FOLLOWUP",
        "incident": "INCIDENT",
        "correction": "CORRECTION",
        "outbound_intent": "OUT",
        "intake": "INTAKE_DRAFT",
    }
    if kind in prefixes:
        return [
            Check(keys.Key(keys.partition(scope), prefixes[kind] + "#" + keys.component(id)), None)
        ]
    return []


def guards(store: "StoreBase", request: CommitRequest, now: datetime) -> list["Check"] | None:
    from sanad.steward.reviews import ReviewRefused, load_offer, prepare
    from sanad.store._base import Check

    command = request.command
    if "listing_token" in command.payload:
        return listing_guards(store, request, now)
    if command.principal.session_id is not None:
        return None  # Telegram offers retain their exact transport principal.
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
