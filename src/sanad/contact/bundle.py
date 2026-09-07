"""One recoverable weekly DEADLINE bundle per doctor, from current obligations."""

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import JsonValue

from sanad.auth.service import revise
from sanad.contact.policy import DRAFT_CONTACT_POLICY as POLICY
from sanad.contact.templates import render
from sanad.domain import DRAFT_POLICY_2026_09, Principal, ReviewObligation, TenantScope, VersionRef
from sanad.steward.types import StewardPolicy
from sanad.store import keys
from sanad.store.protocol import Store
from sanad.store.records import (
    AuditEvent,
    BundleSchedule,
    CommandEnvelope,
    CommitRequest,
    DeliveryResolution,
    Doctor,
    IdentityConfig,
    OperationalClock,
    OutboundIntent,
    Patient,
    StoredRecord,
    WorkerCapability,
    from_record,
    model_scope,
    to_record,
)

if TYPE_CHECKING:
    from sanad.steward.service import Steward


def eligible(store: Store, scope: TenantScope, now: datetime) -> tuple[ReviewObligation, ...]:
    cursor = None
    result = []
    while True:
        page, cursor = store.list_reviews(scope, cursor, limit=100)
        for row in page:
            review = from_record(row, ReviewObligation)
            if (
                review.owner_doctor_id == scope.doctor_id
                and review.state in {"open", "acknowledged"}
                and review.first_notice_at
                and review.first_notice_at <= now - POLICY.bundle_interval
            ):
                result.append(review)
        if cursor is None:
            return tuple(sorted(result, key=lambda r: (r.first_notice_at or r.created_at, r.id)))


def stamp(store: Store, intent: OutboundIntent, now: datetime) -> dict[str, object]:
    if (
        intent.notification_purpose != "DEADLINE"
        or intent.scope_kind == "doctor"
        or not intent.review_obligation_id
    ):
        return {}
    row = store.get(intent.scope, "review", intent.review_obligation_id)
    if row is None:
        return {}
    review = from_record(row, ReviewObligation)
    if review.first_notice_at is not None:
        return {}
    accepted_at = intent.accepted_at or now
    changed = revise(review, now, first_notice_at=accepted_at)
    tenant = TenantScope(doctor_id=review.owner_doctor_id)
    old_row = store.get(tenant, "bundle_schedule", tenant.doctor_id)
    old = from_record(old_row, BundleSchedule) if old_row else None
    at = accepted_at + POLICY.bundle_interval
    fields: dict[str, object] = {
        "obligation_stamp": to_record(changed, model_scope(review)),
        "obligation_expected_version": row.version,
    }
    if old is None:
        schedule = BundleSchedule(
            id=tenant.doctor_id,
            doctor_id=tenant.doctor_id,
            scope=tenant,
            next_action_at=at,
            work_clock=OperationalClock(work_lane="bundle", next_action_at=at),
            created_at=now,
            updated_at=now,
        )
    elif old.pending_intent_id or (old.next_action_at and old.next_action_at <= at):
        return fields
    else:
        schedule = revise(
            old,
            now,
            next_action_at=at,
            work_clock=OperationalClock(
                work_lane="bundle", next_action_at=at, work_generation=old.generation
            ),
        )
    fields.update(
        bundle_schedule=to_record(schedule, tenant),
        bundle_expected_version=old.version if old else None,
    )
    return fields


def accepted_schedule(store: Store, intent: OutboundIntent, now: datetime) -> dict[str, object]:
    if intent.scope_kind != "doctor" or type(intent.scope) is not TenantScope:
        return {}
    row = store.get(intent.scope, "bundle_schedule", intent.scope.doctor_id)
    if row is None:
        return {}
    schedule = from_record(row, BundleSchedule)
    if schedule.pending_intent_id != intent.id:
        return {}
    at = (intent.accepted_at or now) + POLICY.bundle_interval
    changed = revise(
        schedule,
        now,
        pending_intent_id=None,
        generation=schedule.generation + 1,
        last_provider_accepted_at=intent.accepted_at or now,
        next_action_at=at,
        work_clock=OperationalClock(
            next_action_at=at, work_lane="bundle", work_generation=schedule.generation + 1
        ),
    )
    return {
        "bundle_schedule": to_record(changed, intent.scope),
        "bundle_expected_version": schedule.version,
    }


def freshness(
    store: Store, intent: OutboundIntent, now: datetime, settings: IdentityConfig | None
) -> str | None:
    scope = intent.scope
    if type(scope) is not TenantScope:
        return "unsupported_variant"
    row = store.get(scope, "doctor", scope.doctor_id)
    doctor = from_record(row, Doctor) if row else None
    if (
        not doctor
        or doctor.status != "approved"
        or not settings
        or doctor.telegram_bot_id != settings.bot_id
        or intent.bot_id != settings.bot_id
        or doctor.private_chat_id != intent.recipient_ref
        or doctor.auth_epoch != intent.recipient_auth_epoch_seen
        or store.authorize(settings.bot_id, doctor.telegram_user_id).principal.actor_kind
        != "doctor"
    ):
        return "recipient_authority"
    schedule_row = store.get(scope, "bundle_schedule", scope.doctor_id)
    schedule = from_record(schedule_row, BundleSchedule) if schedule_row else None
    if (
        not schedule
        or schedule.pending_intent_id != intent.id
        or intent.logical_key != f"bundle:{scope.doctor_id}:{schedule.generation}"
    ):
        return "bundle_generation"
    if not eligible(store, scope, now):
        return "bundle_empty"
    if intent.expires_at <= now:
        return "expired"
    return None


KINDS = {
    "result_review": "مراجعة نتيجة",
    "correction_disposition": "مراجعة تصحيح",
    "incident_response": "متابعة تنبيه خطر",
    "unmet_objective": "مطلوب لم يكتمل",
    "question_answer": "سؤال محتاج إجابة",
    "media_failure": "ملف محتاج مراجعة",
    "delivery_failure": "مشكلة توصيل رسالة",
    "binding_review": "مراجعة التواصل",
    "coverage_review": "مراجعة التغطية",
    "followup_disposition": "متابعة بدون رد",
    "evidence_association": "ربط مستند",
    "intake_clarification": "توضيح بيانات",
}


def payload(store: Store, intent: OutboundIntent, now: datetime) -> dict[str, JsonValue]:
    return payload_snapshot(store, intent, now)[0]


def payload_snapshot(
    store: Store, intent: OutboundIntent, now: datetime
) -> tuple[dict[str, JsonValue], tuple[VersionRef, ...]]:
    assert type(intent.scope) is TenantScope
    reviews = eligible(store, intent.scope, now)
    lines = []
    for review in reviews[: POLICY.bundle_max_lines]:
        patient_row = (
            store.get(model_scope(review), "patient", review.patient_id)
            if review.patient_id
            else None
        )
        name = from_record(patient_row, Patient).display_name if patient_row else "غير مرتبط بمريض"
        days = (now - (review.first_notice_at or now)).days
        lines.append(f"{name} — {KINDS[review.review_kind]} — {days} يوم من أول تنبيه")
    if len(reviews) > POLICY.bundle_max_lines:
        lines.append(f"و {len(reviews) - POLICY.bundle_max_lines} بنود تانية")
    return (
        {"text": render("doctor_weekly_bundle", lines="\n".join(lines))},
        tuple(to_record(r, model_scope(r)).ref for r in reviews),
    )


def wake(steward: "Steward", record: StoredRecord) -> None:
    store, now = steward.store, steward.clock()
    initial = from_record(record, BundleSchedule)
    row = store.get(initial.scope, "bundle_schedule", initial.id)
    if row is None:
        return
    schedule = from_record(row, BundleSchedule)
    if schedule.next_action_at is None or schedule.next_action_at > now:
        return
    policy = StewardPolicy(DRAFT_POLICY_2026_09)
    next_at = None
    pending = schedule.pending_intent_id
    generation = schedule.generation
    intents: tuple[StoredRecord, ...] = ()
    if pending:
        outgoing = store.get(schedule.scope, "outbound_intent", pending)
        intent = from_record(outgoing, OutboundIntent) if outgoing else None
        if intent and intent.status in {"queued", "sending", "provider_accepted"}:
            return  # Its delivery clock owns recovery, including accepted schedule feedback.
        if intent and intent.status in {"uncertain", "failed"}:
            # Timed delivery disposition; never manufacture a new weekly attempt on uncertainty.
            next_at = now + policy.timing.result_review_interval
        else:
            pending = None
            if intent and intent.status == "suppressed":
                generation += 1
    if pending is None and eligible(store, schedule.scope, now):
        doctor_row = store.get(schedule.scope, "doctor", schedule.doctor_id)
        if doctor_row is None:
            return
        doctor = from_record(doctor_row, Doctor)
        logical = f"bundle:{schedule.doctor_id}:{generation}"
        id = keys.digest(logical)
        existing = store.get(schedule.scope, "outbound_intent", id)
        if existing:
            # Suppression never advances an accepted generation. Keep timed operational ownership.
            next_at = now + policy.timing.result_review_interval
        else:
            intent = OutboundIntent(
                id=id,
                scope=schedule.scope,
                scope_kind="doctor",
                audience="doctor",
                logical_key=logical,
                source_event_ids=(logical,),
                source_versions=(),
                recipient_ref=doctor.private_chat_id,
                notification_purpose="DEADLINE",
                eligibility_class="bundle",
                payload_ref=schedule.id,
                payload_digest=keys.digest(schedule.id),
                conversation_sequence=0,
                expires_at=now + POLICY.bundle_interval,
                status="queued",
                created_at=now,
                updated_at=now,
                recipient_auth_epoch_seen=doctor.auth_epoch,
                bot_id=doctor.telegram_bot_id,
                template_id="doctor_weekly_bundle",
                work_clock=OperationalClock(work_lane="delivery", next_action_at=now),
                delivery_lease_seconds=int(policy.operations.lease_ttl.total_seconds()),
            )
            intents = (to_record(intent, schedule.scope),)
            pending, next_at = id, now + policy.operations.retry_backoff(1)
    changed = revise(
        schedule,
        now,
        pending_intent_id=pending,
        generation=generation,
        next_action_at=next_at,
        work_clock=OperationalClock(
            work_lane="bundle", next_action_at=next_at, work_generation=generation
        )
        if next_at
        else None,
    )
    command_id = f"sweep:bundle:{schedule.id}:{schedule.version}"
    actor = Principal(subject="steward:bundle", actor_kind="system", doctor_id=schedule.doctor_id)
    command = CommandEnvelope(
        command_id=command_id,
        principal=actor,
        scope=schedule.scope,
        requested_at=now,
        payload={"type": "_BundleWake", "generation": schedule.generation},
        expected_versions=(row.ref,),
        worker=WorkerCapability(
            service_subject=actor.subject,
            resolved_scope=schedule.scope,
            permitted_lanes=frozenset({"bundle"}),
            auth_expiry=now + policy.operations.claim_ttl,
            invocation_id=command_id,
        ),
    )
    audit = to_record(
        AuditEvent(
            id=command_id,
            event_id=command_id,
            command_id=command_id,
            scope=schedule.scope,
            event_type="BUNDLE_WAKE",
            actor=actor,
            accepted_at=now,
            created_at=now,
            updated_at=now,
        ),
        schedule.scope,
    )
    changed_row = to_record(changed, schedule.scope)
    store.commit(
        CommitRequest(
            command=command,
            puts=(changed_row,),
            events=(audit,),
            intents=intents,
            expected=tuple(r.ref for r in (changed_row, audit, *intents)),
        )
    )


def recover_notice(steward: "Steward", intent: OutboundIntent) -> OutboundIntent:
    fields = (
        accepted_schedule(steward.store, intent, steward.clock())
        if intent.scope_kind == "doctor"
        else stamp(steward.store, intent, steward.clock())
    )
    changed = revise(intent, steward.clock(), notice_feedback_pending=False, work_clock=None)
    resolution = DeliveryResolution.model_validate(
        {"intent": to_record(changed, intent.scope), **fields}
    )
    row = steward.store.complete_notice_feedback(intent.scope, intent.id, resolution)
    return from_record(row, OutboundIntent) if row else intent


def review_wake(steward: "Steward", record: StoredRecord) -> None:
    """Re-arm the new tenant delivery review with the existing pure review clock."""
    from sanad.domain.events import RecordAudit, TransitionResult
    from sanad.domain.operations import AccountabilityWake
    from sanad.domain.transitions import transition_review

    source = from_record(record, ReviewObligation)
    scope = model_scope(source)
    if type(scope) is not TenantScope or source.review_kind != "delivery_failure":
        return
    now = steward.clock()
    command_id = f"sweep:review:{source.id}:{source.version}"
    event = AccountabilityWake(event_id=command_id)
    result = transition_review(source, event, now, DRAFT_POLICY_2026_09)
    if not isinstance(result, TransitionResult):
        return
    actor = Principal(subject="steward:review", actor_kind="system", doctor_id=scope.doctor_id)
    command = CommandEnvelope(
        command_id=command_id,
        principal=actor,
        scope=scope,
        requested_at=now,
        payload={"type": "_Wake", "review_id": source.id},
        expected_versions=(record.ref,),
        worker=WorkerCapability(
            service_subject=actor.subject,
            resolved_scope=scope,
            permitted_lanes=frozenset({"review"}),
            auth_expiry=now + StewardPolicy(DRAFT_POLICY_2026_09).operations.claim_ttl,
            invocation_id=command_id,
        ),
    )
    row = to_record(result.aggregate, scope)
    events = tuple(
        to_record(
            AuditEvent(
                id=effect.event_id,
                event_id=effect.event_id,
                command_id=command_id,
                scope=scope,
                event_type=effect.event_type,
                actor=actor,
                accepted_at=now,
                created_at=now,
                updated_at=now,
            ),
            scope,
        )
        for effect in result.effects
        if isinstance(effect, RecordAudit)
    )
    steward.store.commit(
        CommitRequest(
            command=command,
            puts=(row,),
            events=events,
            expected=tuple(r.ref for r in (row, *events)),
        )
    )
