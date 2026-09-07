"""One sending gateway. Source truth is re-read immediately before transport IO."""

from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import JsonValue

from sanad.accounts.delivery import account_freshness
from sanad.channels.transport import ProvablyUnsent, SendOutcome, Transport
from sanad.domain import (
    DRAFT_POLICY_2026_09,
    CreateReview,
    FollowUpTask,
    Mission,
    PatientScope,
    ReviewKind,
    TenantScope,
    create_review,
)
from sanad.domain.operations import transition_operational_clock
from sanad.steward.service import Steward
from sanad.steward.types import StewardPolicy
from sanad.store.keys import AccountScope, IntakeScope, ScopedKey
from sanad.store.protocol import Store
from sanad.store.records import (
    DeliveryOutcome,
    DeliveryResolution,
    Doctor,
    DoctorAuthority,
    IdentityConfig,
    OperationalClock,
    OperationalIssue,
    OutboundIntent,
    StoredRecord,
    from_record,
    to_record,
)


def freshness(
    store: Store, intent: OutboundIntent, now: datetime, *, settings: IdentityConfig | None = None
) -> str | None:
    scope = intent.scope
    if intent.scope_kind == "account":
        return account_freshness(store, intent, now, settings)
    if intent.scope_kind == "intake" and isinstance(scope, IntakeScope):
        doctor_row = store.get(scope, "doctor", scope.doctor_id)
        intake_doctor = from_record(doctor_row, Doctor) if doctor_row else None
        if (
            intake_doctor is None
            or intake_doctor.status != "approved"
            or settings is None
            or intake_doctor.telegram_bot_id != settings.bot_id
            or intake_doctor.private_chat_id != intent.recipient_ref
            or intake_doctor.auth_epoch != intent.recipient_auth_epoch_seen
            or store.authorize(settings.bot_id, intake_doctor.telegram_user_id).principal.actor_kind
            != "doctor"
        ):
            return "recipient_authority"
        if (
            intent.notification_purpose not in {"solicited_reply", "DANGER"}
            or intent.expires_at <= now
        ):
            return "expired_or_unsupported"
        for ref in intent.source_versions:
            row = store.get(scope, ref.entity_type, ref.id)
            if row is None or row.version != ref.version:
                return "source_version"
            if intent.template_id == "scribe_card" and (
                row.body.get("status") != "pending" or row.body.get("editing")
            ):
                return "proposal_not_pending"
        return None
    if not isinstance(scope, PatientScope) or intent.scope_kind != "patient":
        return "unsupported_variant"
    profile = store.get_patient_profile(scope)
    if profile is None:
        return "patient_missing"
    doctor_row = store.get(scope, "doctor_authority", scope.doctor_id)
    doctor = from_record(doctor_row, DoctorAuthority) if doctor_row else None
    safety = intent.notification_purpose == "patient_safety_response"
    if intent.audience == "doctor":
        if intent.notification_purpose == "DANGER" and doctor is not None:
            # Slice 05 can distinguish an actually suspended, previously approved
            # doctor from the absent/unapproved fixture facts of slices 02–03.
            row = store.get(TenantScope(doctor_id=scope.doctor_id), "doctor", scope.doctor_id)
            account = from_record(row, Doctor) if row else None
            binding = (
                store.authorize(account.telegram_bot_id, account.telegram_user_id).binding
                if account is not None
                else None
            )
            if (
                account is not None
                and account.status == "suspended"
                and binding is not None
                and binding.status == "frozen"
                and "doctor" in binding.role_set
                and binding.doctor_id == scope.doctor_id
                and binding.private_chat_id == account.private_chat_id
                and account.private_chat_id == doctor.recipient_ref == intent.recipient_ref
                and account.telegram_user_id == doctor.subject
                and account.auth_epoch == doctor.auth_epoch
                and intent.recipient_auth_epoch_seen is not None
                and 1 <= intent.recipient_auth_epoch_seen <= account.auth_epoch
            ):
                return None
        if (
            doctor is None
            or not doctor.approved
            or doctor.recipient_ref != intent.recipient_ref
            or doctor.auth_epoch != intent.recipient_auth_epoch_seen
        ):
            return "recipient_authority"
        if intent.notification_purpose == "DANGER":
            return None
    elif intent.audience == "patient":
        if (
            profile.recipient_ref != intent.recipient_ref
            or profile.recipient_subject is None
            or profile.recipient_auth_epoch != intent.recipient_auth_epoch_seen
        ):
            return "recipient_authority"
        if not safety:
            if (
                doctor is None
                or not doctor.approved
                or doctor.auth_epoch != intent.doctor_auth_epoch_seen
            ):
                return "doctor_coverage"
            if not profile.binding_active or profile.binding_epoch != intent.binding_epoch_seen:
                return "binding"
            if (
                not profile.consent_active
                or profile.consent_version is None
                or profile.consent_version != intent.consent_version_seen
            ):
                return "consent"
            if intent.notification_purpose == "routine_prompt" and (
                not profile.routine_contact_enabled
                or (profile.routine_paused_until is not None and profile.routine_paused_until > now)
            ):
                return "routine_contact_disabled"
            if profile.delivery_epoch != intent.delivery_epoch_seen:
                return "delivery_epoch"
        if profile.safety_epoch != intent.safety_epoch_seen:
            return "safety_epoch"
    else:
        return "unsupported_variant"
    if intent.expires_at <= now:
        return "expired"
    sources = []
    for ref in intent.source_versions:
        row = store.get(scope, ref.entity_type, ref.id)
        if row is None or row.version != ref.version:
            return "source_version"
        sources.append(row)
    for ref in intent.order_refs:
        row = store.get(scope, "care_order", ref.id)
        if row is None or row.version != ref.version or row.body.get("status") != "active":
            return "order_inactive"
    if intent.notification_purpose in {"DONE:FULFILLMENT", "DEADLINE"}:
        aggregates = [row for row in sources if row.entity_type in {"mission", "followup"}]
        if len(aggregates) != 1:
            return "objective_source_required"
        source = aggregates[0]
        if source.entity_type == "mission":
            mission = from_record(source, Mission)
            valid = (
                (mission.state == "fulfilled" and mission.fulfillment_validity == "valid")
                if intent.notification_purpose == "DONE:FULFILLMENT"
                else (
                    mission.state == "overdue"
                    and mission.handled_deadline_generation == mission.deadline_generation
                )
            )
        else:
            followup = from_record(source, FollowUpTask)
            valid = (
                (followup.state == "fulfilled")
                if intent.notification_purpose == "DONE:FULFILLMENT"
                else (followup.state == "overdue" and followup.deadline_handled)
            )
        if not valid:
            return "objective_not_current"
    if intent.notification_purpose == "DONE:CORRECTION":
        return "correction_notice_not_released"
    if safety and (
        intent.template_id is None or not any(row.entity_type == "incident" for row in sources)
    ):
        return "safety_template_or_incident_missing"
    if intent.notification_purpose == "routine_prompt" and intent.slot_id is None:
        return "slot_required"
    return None


def transition_delivery(
    intent: OutboundIntent,
    outcome: SendOutcome,
    now: datetime,
    policy: StewardPolicy,
    *,
    suppression: str | None = None,
    settings: IdentityConfig | None = None,
) -> tuple[OutboundIntent, tuple[StoredRecord, ...]]:
    """Choose the next durable delivery state without mistaking uncertainty for success."""
    assert intent.work_clock is not None
    changes: dict[str, object] = {"version": intent.version + 1, "updated_at": now}
    review_needed = False
    if suppression is not None:
        changes.update(status="suppressed", suppression_reason=suppression, work_clock=None)
    elif outcome.status == "accepted":
        changes.update(
            status="provider_accepted",
            accepted_message_id=outcome.provider_message_id,
            accepted_at=now,
            work_clock=None,
        )
    elif outcome.status == "uncertain":
        count = intent.uncertain_retry_count + 1
        changes.update(status="uncertain", uncertain_retry_count=count, last_error="uncertain")
        if (
            intent.notification_purpose == "DANGER"
            and count <= policy.operations.danger_uncertain_resend_limit
        ):
            changes.update(
                status="queued",
                work_clock=transition_operational_clock(
                    intent.work_clock,
                    now + policy.operations.retry_backoff(count),
                    attempt_delta=1,
                    error="uncertain",
                ),
            )
        else:
            review_needed = True
    else:
        count = intent.retry_count + 1
        changes.update(
            status="failed", retry_count=count, last_error=outcome.code, retryable=outcome.retryable
        )
        if outcome.retryable and count < policy.operations.max_delivery_attempts:
            changes.update(
                status="queued",
                work_clock=transition_operational_clock(
                    intent.work_clock,
                    now
                    + (
                        timedelta(seconds=outcome.retry_after_seconds)
                        if outcome.retry_after_seconds is not None
                        else policy.operations.retry_backoff(count)
                    ),
                    attempt_delta=1,
                    error=outcome.code,
                ),
            )
        else:
            review_needed = True
    reviews: tuple[StoredRecord, ...] = ()
    if review_needed and isinstance(intent.scope, PatientScope):
        result = create_review(
            CreateReview(
                event_id="delivery:" + (intent.active_attempt_id or intent.id),
                source_type="outbound_intent",
                source_id=intent.id,
                source_version=intent.version + 1,
                review_kind=ReviewKind.delivery_failure,
                owner_doctor_id=intent.scope.doctor_id,
                patient_id=intent.scope.patient_id,
                review_at=now + policy.timing.result_review_interval,
            ),
            now,
            policy.timing,
        )
        changes.update(review_obligation_id=result.aggregate.id, work_clock=None)
        reviews = (to_record(result.aggregate, intent.scope),)
    if review_needed and isinstance(intent.scope, AccountScope):
        issue = OperationalIssue(
            id="delivery:" + (intent.active_attempt_id or intent.id),
            scope=intent.scope,
            kind="delivery_failure",
            affected_id=intent.id,
            owner_id=settings.admin_user_id if settings else "unconfigured-admin",
            due_at=now + policy.timing.result_review_interval,
            reason="delivery_" + outcome.status,
            created_at=now,
            updated_at=now,
            work_clock=OperationalClock(
                next_action_at=now + policy.timing.result_review_interval, work_lane="operational"
            ),
        )
        changes.update(review_obligation_id=issue.id, work_clock=None)
        reviews = (to_record(issue, intent.scope),)
    if review_needed and isinstance(intent.scope, IntakeScope):
        result = create_review(
            CreateReview(
                event_id="delivery:" + (intent.active_attempt_id or intent.id),
                source_type="intake",
                source_id=intent.scope.intake_id,
                source_version=intent.version + 1,
                review_kind=ReviewKind.delivery_failure,
                owner_doctor_id=intent.scope.doctor_id,
                review_at=now + policy.timing.result_review_interval,
            ),
            now,
            policy.timing,
        )
        changes.update(review_obligation_id=result.aggregate.id, work_clock=None)
        reviews = (to_record(result.aggregate, intent.scope),)
    return OutboundIntent.model_validate(intent.model_dump() | changes), reviews


class Dispatcher:
    def __init__(
        self,
        steward: Steward,
        transport: Transport,
        *,
        settings: IdentityConfig | None = None,
        payload_resolver: Callable[[OutboundIntent], dict[str, JsonValue]] | None = None,
        extra_freshness: Callable[[OutboundIntent, datetime], str | None] | None = None,
    ):
        self.steward, self.store, self.transport = steward, steward.store, transport
        self.settings, self.payload_resolver = settings, payload_resolver
        self.extra_freshness = extra_freshness

    def dispatch_one(
        self, intent_key: ScopedKey, owner: str, now: datetime
    ) -> OutboundIntent | None:
        scope = intent_key.scope
        row = self.store.get(scope, "outbound_intent", _intent_id(intent_key))
        if row is None or row.key != intent_key.key:
            return None
        intent = from_record(row, OutboundIntent)
        if not isinstance(scope, (PatientScope, AccountScope, IntakeScope)):
            # Such records are accepted by the passive store; no authority adapter is released here.
            return None
        if intent.work_clock is None or intent.work_clock.next_action_at > now:
            return intent
        if intent.template_id == "scribe_card" and intent.conversation_sequence:
            from sanad.steward.types import records

            earlier = [
                from_record(r, OutboundIntent)
                for r in records(self.store, scope, "outbound_intent")
                if r.body.get("source_event_ids") == list(intent.source_event_ids)
                and r.body.get("template_id") == intent.template_id
                and int(str(r.body.get("conversation_sequence", 0))) < intent.conversation_sequence
            ]
            if any(i.status != "provider_accepted" for i in earlier):
                return intent
        policy = self._policy(intent)
        if intent.status == "uncertain":
            return self._finish(intent, SendOutcome(status="uncertain"), now)
        if intent.status == "failed":
            return self._finish(
                intent,
                SendOutcome(
                    status="failed",
                    retryable=intent.retryable is True,
                    code=intent.last_error or "unclassified_failure",
                ),
                now,
            )
        doctor_row = (
            self.store.get(scope, "doctor_authority", scope.doctor_id)
            if isinstance(scope, (PatientScope, IntakeScope))
            else None
        )
        profile_row = (
            self.store.get(scope, "patient_profile", scope.patient_id)
            if isinstance(scope, PatientScope)
            else None
        )
        authority = tuple(
            r.ref
            for r in (doctor_row, profile_row)
            if r is not None
            and (r.entity_type == "doctor_authority" or intent.notification_purpose != "DANGER")
        )
        attempt = self.store.start_delivery(
            intent.id,
            (row.ref, *intent.source_versions),
            owner,
            now,
            scope=scope,
            freshness_versions=authority,
        )
        row = self.store.get(scope, "outbound_intent", intent.id)
        if row is None:
            return None
        intent = from_record(row, OutboundIntent)
        if attempt is None:
            if intent.status == "uncertain" and intent.work_clock is not None:
                return self._finish(intent, SendOutcome(status="uncertain"), now)
            return intent
        reason = self._freshness(intent, self.steward.clock())
        if reason is not None:
            return self._finish(
                intent, SendOutcome(status="uncertain"), self.steward.clock(), suppression=reason
            )
        if intent.notification_purpose == "routine_prompt" and isinstance(scope, PatientScope):
            lease = self.store.acquire_patient(
                scope, "dispatch:" + owner, self.steward.clock(), policy.operations.lease_ttl
            )
            if lease is None:
                return self._finish(
                    intent,
                    SendOutcome(status="failed", retryable=True, code="patient_busy"),
                    self.steward.clock(),
                    unsent=True,
                )
            try:
                reserved = self.store.reserve_contact(scope, intent.slot_id or "", intent.id, lease)
            finally:
                self.store.release_patient(lease)
            if reserved == "already_taken":
                return self._finish(
                    intent,
                    SendOutcome(status="uncertain"),
                    self.steward.clock(),
                    suppression="budget",
                )
        # Re-read after reservation IO as well. A replaced/expired attempt never sends.
        latest = self.store.get(scope, "outbound_intent", intent.id)
        current = from_record(latest, OutboundIntent) if latest else None
        at = self.steward.clock()
        if (
            current is None
            or current.status != "sending"
            or current.active_attempt_id != attempt.id
            or current.delivery_claim is None
            or current.delivery_claim.expires_at <= at
        ):
            return current
        reason = self._freshness(current, at)
        if reason is not None:
            return self._finish(
                current, SendOutcome(status="uncertain"), at, suppression=reason, unsent=True
            )
        unsent = False
        try:
            outcome = self.transport.send(
                current.recipient_ref,
                current.payload
                if current.payload is not None
                else self.payload_resolver(current)
                if self.payload_resolver
                else {
                    "purpose": current.notification_purpose,
                    "payload_ref": current.payload_ref,
                    "template_id": current.template_id,
                    "logical_key": current.logical_key,
                    "source_event_ids": list(current.source_event_ids),
                    "same_incident_resend": current.notification_purpose == "DANGER"
                    and current.uncertain_retry_count > 0,
                },
            )
        except ProvablyUnsent:
            outcome, unsent = (
                SendOutcome(status="failed", retryable=True, code="provably_unsent"),
                True,
            )
        except TimeoutError:
            outcome = SendOutcome(status="uncertain")
        return self._finish(current, outcome, self.steward.clock(), unsent=unsent)

    def _freshness(self, intent: OutboundIntent, now: datetime) -> str | None:
        return freshness(self.store, intent, now, settings=self.settings) or (
            self.extra_freshness(intent, now) if self.extra_freshness else None
        )

    def _policy(self, intent: OutboundIntent) -> StewardPolicy:
        return (
            self.steward.policy_provider(intent.scope)
            if isinstance(intent.scope, PatientScope)
            else StewardPolicy(DRAFT_POLICY_2026_09)
        )

    def _finish(
        self,
        intent: OutboundIntent,
        outcome: SendOutcome,
        now: datetime,
        *,
        suppression: str | None = None,
        unsent: bool = False,
    ) -> OutboundIntent | None:
        changed, reviews = transition_delivery(
            intent,
            outcome,
            now,
            self._policy(intent),
            suppression=suppression,
            settings=self.settings,
        )
        outcomes: dict[str, DeliveryOutcome] = {
            "accepted": "provider_accepted",
            "uncertain": "uncertain",
            "failed": "definite_failure",
        }
        state: DeliveryOutcome = "suppressed" if suppression else outcomes[outcome.status]
        row = self.store.complete_delivery(
            intent.active_attempt_id or "",
            state,
            outcome.provider_message_id,
            scope=intent.scope,
            resolution=DeliveryResolution(
                intent=to_record(changed, intent.scope), reviews=reviews, release_reservation=unsent
            ),
        )
        if row is not None:
            return from_record(row, OutboundIntent)
        # A send can outlive its lease. Persisted attempt-start remains uncertain.
        saved = self.store.get(intent.scope, "outbound_intent", intent.id)
        if saved is not None:
            self.store.start_delivery(
                intent.id,
                (saved.ref, *intent.source_versions),
                "late-completion",
                now,
                scope=intent.scope,
            )
            saved = self.store.get(intent.scope, "outbound_intent", intent.id)
        return from_record(saved, OutboundIntent) if saved else None


def _intent_id(key: ScopedKey) -> str:
    from urllib.parse import unquote

    return unquote(key.sk.removeprefix("OUT#"))
