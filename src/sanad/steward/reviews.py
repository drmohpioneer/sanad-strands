"""The two review verbs, with no patient fabrication or executor effects."""

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY
from sanad.domain import (
    DRAFT_POLICY_2026_09,
    PatientScope,
    ReviewAction,
    ReviewObligation,
    TenantScope,
)
from sanad.domain import events as ev
from sanad.domain.transitions import transition_review
from sanad.liaison.records import ReviewOffer
from sanad.liaison.snapshot import snapshot
from sanad.steward.types import CommandResult, command_result
from sanad.store import keys
from sanad.store.keys import IntakeScope
from sanad.store.records import (
    AuditEvent,
    CommandEnvelope,
    CommitRequest,
    from_record,
    model_scope,
    to_record,
)

if TYPE_CHECKING:
    from sanad.steward.service import Steward
    from sanad.store.protocol import Store


class ReviewRefused(ValueError):
    pass


def load_offer(store: "Store", command: CommandEnvelope) -> ReviewOffer:
    doctor_id = command.principal.doctor_id
    row = (
        store.get(
            TenantScope(doctor_id=doctor_id),
            "review_offer",
            str(command.payload.get("offer_id", "")),
        )
        if doctor_id
        else None
    )
    if not row:
        raise ReviewRefused("offer_missing")
    return from_record(row, ReviewOffer)


def prepare(store: "Store", command: CommandEnvelope, now: datetime) -> CommitRequest:
    offer = load_offer(store, command)
    if offer.snapshot.scope != command.scope:
        raise ReviewRefused("review_scope_changed")
    row = store.get(command.scope, "review", offer.snapshot.review_ref.id)
    if not row:
        raise ReviewRefused("review_missing")
    review = from_record(row, ReviewObligation)
    if (
        model_scope(review) != command.scope
        or review.owner_doctor_id != command.principal.doctor_id
    ):
        raise ReviewRefused("review_scope_changed")
    if snapshot(store, review) != offer.snapshot:
        raise ReviewRefused("review_or_source_changed")
    if offer.expires_at <= now or offer.consumed_at:
        raise ReviewRefused("offer_expired_or_used")
    event_id = keys.digest("review-action:" + command.command_id)
    kind = command.payload.get("type")
    if kind not in {"AcknowledgeReview", "ResolveReview"}:
        raise ReviewRefused("unsupported_review_command")
    if set(command.payload) - {"type", "offer_id", "expected_source_version", "reason"}:
        raise ReviewRefused("invalid_action")
    if (
        type(command.payload.get("expected_source_version")) is not int
        or command.payload.get("expected_source_version") != offer.snapshot.source_version
    ):
        raise ReviewRefused("source_version_changed")
    if kind == "AcknowledgeReview":
        if offer.action != "acknowledge":
            raise ReviewRefused("action_not_offered")
        event: ev.AcknowledgeReview | ev.ResolveReview = ev.AcknowledgeReview(
            event_id=event_id, actor_id=command.principal.subject
        )
    else:
        reason = command.payload.get("reason")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > DRAFT_CONCIERGE_POLICY.reply_max_chars
        ):
            raise ReviewRefused("reason_required")
        try:
            event = ev.ResolveReview(
                event_id=event_id,
                actor_id=command.principal.subject,
                action=ReviewAction(offer.action),
                expected_source_version=offer.snapshot.source_version,
                reason=reason.strip(),
            )
        except (ValueError, ValidationError) as error:
            raise ReviewRefused("action_not_offered") from error
    result = transition_review(review, event, now, DRAFT_POLICY_2026_09)
    if not isinstance(result, ev.TransitionResult) or any(
        not isinstance(e, ev.RecordAudit) for e in result.effects
    ):
        raise ReviewRefused("review_transition_refused")
    changed = to_record(result.aggregate, command.scope)
    consumed = to_record(
        offer.model_copy(
            update={
                "version": offer.version + 1,
                "updated_at": now,
                "consumed_at": now,
                "consumed_by": command.command_id,
            }
        ),
        offer.scope,
    )
    audit = to_record(
        AuditEvent(
            id=event_id,
            event_id=event_id,
            command_id=command.command_id,
            scope=command.scope,
            event_type="REVIEW_ACKNOWLEDGED" if kind == "AcknowledgeReview" else "REVIEW_RESOLVED",
            actor=command.principal,
            aggregate_refs=(changed.ref,),
            before_versions=(row.ref,),
            after_versions=(changed.ref,),
            source_refs=(offer.id,),
            accepted_at=now,
            created_at=now,
            updated_at=now,
        ),
        command.scope,
    )
    puts = (consumed,) if changed.version == row.version else (changed, consumed)
    return CommitRequest(
        command=command,
        puts=puts,
        events=(audit,),
        expected=tuple(r.ref for r in (*puts, audit)),
        reason_code="ack_done" if kind == "AcknowledgeReview" else "resolve_done",
    )


def handle(steward: "Steward", command: CommandEnvelope) -> CommandResult:
    # This handler is entered for exact tenant/intake variants before the patient
    # guard, or for a patient only after its original lease/authority machinery.
    if type(command.scope) not in {TenantScope, IntakeScope, PatientScope}:
        return CommandResult(status="unsupported")
    assert isinstance(command.scope, TenantScope)
    from sanad.store.reviews import doctor_checks

    try:
        if doctor_checks(steward.store, command.principal, command.scope.doctor_id) is None:
            return CommandResult(status="forbidden", reason_code="doctor_authority_changed")
        prior = steward.store.lookup_command(command)
        if prior:
            return command_result(prior)
        return command_result(
            steward.store.commit(prepare(steward.store, command, steward.clock()))
        )
    except ReviewRefused as error:
        return CommandResult(
            status="invalid_action" if str(error) == "invalid_action" else "stale_version",
            reason_code=str(error),
        )
