"""Slice 02 storage values; no transport, policy selection or clinical transitions.

The accepted domain provides the three clinical aggregates. Command, audit,
receipt, delivery and memory envelopes were blueprint-only at slice 01 and are
defined here to support persistence, without adding clinical authority.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, JsonValue, StrictBool, model_validator

from sanad.domain import (
    FollowUpTask,
    Mission,
    NonblankStr,
    NonnegativeInt,
    PatientScope,
    PositiveVersion,
    Principal,
    ReviewObligation,
    TenantScope,
    UtcInstant,
    VersionRef,
)
from sanad.domain.boundaries import IanaZone, _BoundaryValue
from sanad.domain.events import RecordEvidenceAssociation, RetainObservation, SupersedeEvidence
from sanad.domain.operations import OperationalClock as OperationalClock
from sanad.store import keys
from sanad.store.keys import AccountScope, IntakeScope, Key, Scope, ScopedKey


class ProcessingClaim(_BoundaryValue):
    owner: NonblankStr
    generation: PositiveVersion
    expires_at: UtcInstant
    claimed_at: UtcInstant


class _Metadata(_BoundaryValue):
    id: NonblankStr
    version: PositiveVersion = 1
    created_at: UtcInstant
    updated_at: UtcInstant

    @model_validator(mode="after")
    def times(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        return self


class IdentityConfig(_BoundaryValue):
    """Nonsecret settings projected into the persistence boundary."""

    bot_id: NonblankStr
    admin_user_id: NonblankStr


class Application(_Metadata):
    entity_type: Literal["application"] = "application"
    scope: AccountScope
    telegram_user_id: Annotated[str, Field(strict=True, pattern=r"^[0-9]+$", max_length=32)]
    private_chat_id: NonblankStr
    claimed_name: Annotated[str, Field(strict=True, max_length=160)] = ""
    claimed_specialty: Annotated[str, Field(strict=True, max_length=160)] = ""
    claimed_city: Annotated[str, Field(strict=True, max_length=160)] = ""
    status: Literal["pending", "approved", "rejected"] = "pending"
    reviewer_id: NonblankStr | None = None
    reviewed_at: UtcInstant | None = None
    approval_reference: NonblankStr | None = None
    doctor_id: NonblankStr | None = None
    work_clock: OperationalClock | None

    @model_validator(mode="after")
    def lifecycle(self) -> Self:
        if self.id != keys.digest(f"{self.scope.bot_id}:{self.telegram_user_id}"):
            raise ValueError("application ID must identify the bot and verified subject")
        if (self.status == "pending") != (self.work_clock is not None):
            raise ValueError("pending applications require an account clock")
        if self.work_clock is not None and self.work_clock.work_lane != "account":
            raise ValueError("application requires account lane")
        if (self.status == "approved") != (self.doctor_id is not None):
            raise ValueError("only approved applications have a doctor ID")
        if self.status != "pending" and (self.reviewer_id is None or self.reviewed_at is None):
            raise ValueError("decisions require a reviewer and timestamp")
        return self


class Doctor(_Metadata):
    entity_type: Literal["doctor"] = "doctor"
    scope: TenantScope
    telegram_bot_id: NonblankStr
    telegram_user_id: NonblankStr
    private_chat_id: NonblankStr
    name: Annotated[str, Field(strict=True, max_length=160)] = ""
    specialty: Annotated[str, Field(strict=True, max_length=160)] = ""
    city: Annotated[str, Field(strict=True, max_length=160)] = ""
    language: Literal["ar", "en"] = "ar"
    timezone: IanaZone = "Africa/Cairo"
    status: Literal["pending", "approved", "rejected", "suspended", "revoked"]
    approved_by: NonblankStr | None = None
    approved_at: UtcInstant | None = None
    auth_epoch: NonnegativeInt
    policy_version: NonblankStr
    application_id: NonblankStr

    @model_validator(mode="after")
    def identity(self) -> Self:
        if type(self.scope) is not TenantScope or self.id != self.scope.doctor_id:
            raise ValueError("doctor requires its exact tenant and opaque doctor ID")
        if self.status in {"approved", "suspended"} and (
            self.approved_by is None or self.approved_at is None or self.auth_epoch < 1
        ):
            raise ValueError("activated doctors require approval evidence")
        return self


class SubjectBinding(_Metadata):
    entity_type: Literal["subject_binding"] = "subject_binding"
    scope: AccountScope
    telegram_user_id: Annotated[str, Field(strict=True, pattern=r"^[0-9]+$", max_length=32)]
    role_set: frozenset[Literal["admin", "doctor", "patient"]] = frozenset()
    doctor_id: NonblankStr | None = None
    patient_id: NonblankStr | None = None
    status: Literal["active", "frozen", "revoked"] = "active"
    binding_epoch: NonnegativeInt = 1
    private_chat_id: NonblankStr

    @model_validator(mode="after")
    def roles(self) -> Self:
        if self.id != keys.subject(self.scope.bot_id, self.telegram_user_id).pk:
            raise ValueError("subject binding requires its deterministic global key")
        if "patient" in self.role_set and self.role_set & {"admin", "doctor"}:
            raise ValueError("patient role is exclusive")
        if self.role_set & {"doctor", "patient"} and self.doctor_id is None:
            raise ValueError("bound role requires a doctor ID")
        if ("patient" in self.role_set) != (self.patient_id is not None):
            raise ValueError("only patient bindings require a patient ID")
        return self


class Authorization(_BoundaryValue):
    principal: Principal
    binding: SubjectBinding | None = None
    doctor_status: Literal["pending", "approved", "rejected", "suspended", "revoked"] | None = None
    auth_epoch: NonnegativeInt | None = None
    private_chat_id: NonblankStr | None = None


class CallbackToken(_Metadata):
    entity_type: Literal["callback_token"] = "callback_token"
    scope: AccountScope
    application_id: NonblankStr
    expected_application_version: PositiveVersion
    actor_subject: NonblankStr
    action: Literal["approve", "reject"]
    expires_at: UtcInstant
    consumed_at: UtcInstant | None = None

    @model_validator(mode="after")
    def hashed(self) -> Self:
        keys.token("callback", self.id)
        return self


class AccountAcknowledgment(_Metadata):
    entity_type: Literal["account_ack"] = "account_ack"
    scope: AccountScope
    application_id: NonblankStr
    application_version: PositiveVersion
    last_queued_at: UtcInstant


class OperationalIssue(_Metadata):
    entity_type: Literal["operational_issue"] = "operational_issue"
    scope: AccountScope
    kind: Literal["coverage_review", "delivery_failure", "inbound_failure"]
    affected_id: NonblankStr
    owner_role: Literal["admin"] = "admin"
    owner_id: NonblankStr
    status: Literal["open", "acknowledged", "resolved"] = "open"
    due_at: UtcInstant
    reason: NonblankStr
    work_clock: OperationalClock | None

    @model_validator(mode="after")
    def accountable(self) -> Self:
        if (self.status == "resolved") != (self.work_clock is None):
            raise ValueError("unresolved issues require an operational clock")
        if self.work_clock is not None and self.work_clock.work_lane != "operational":
            raise ValueError("issue requires operational lane")
        return self


class PatientProfile(_Metadata):
    """Operational authority facts supplied by trusted fixtures until slice 06."""

    entity_type: Literal["patient_profile"] = "patient_profile"
    doctor_id: NonblankStr
    patient_id: NonblankStr
    normalized_name: str = ""
    lease_owner: NonblankStr | None = None
    lease_expires_at: UtcInstant | None = None
    lease_generation: NonnegativeInt = 0
    safety_epoch: NonnegativeInt = 0
    delivery_epoch: NonnegativeInt = 0
    binding_epoch: NonnegativeInt = 0
    consent_version: PositiveVersion | None = None
    consent_active: StrictBool = False
    binding_active: StrictBool = False
    recipient_ref: NonblankStr | None = None
    recipient_subject: NonblankStr | None = None
    recipient_auth_epoch: NonnegativeInt = 0

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.id != self.patient_id:
            raise ValueError("profile ID must be the patient ID")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("lease owner and expiry must be supplied together")
        return self


class DoctorAuthority(_Metadata):
    """Patient dispatch authority snapshot, written atomically by the account service."""

    entity_type: Literal["doctor_authority"] = "doctor_authority"
    doctor_id: NonblankStr
    approved: StrictBool = False
    subject: NonblankStr
    recipient_ref: NonblankStr
    auth_epoch: NonnegativeInt

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.id != self.doctor_id:
            raise ValueError("doctor authority ID must equal doctor ID")
        return self


class OrderAuthority(_Metadata):
    """Current order version/status fact, without prescribing or amendment workflows."""

    entity_type: Literal["care_order"] = "care_order"
    scope: PatientScope
    status: Literal["active", "stopped", "superseded"]


class EvidenceAnnotation(_Metadata):
    entity_type: Literal["evidence_annotation"] = "evidence_annotation"
    scope: PatientScope
    source_event_id: NonblankStr
    aggregate_ref: VersionRef
    annotation: Annotated[
        RetainObservation | RecordEvidenceAssociation | SupersedeEvidence,
        Field(discriminator="effect_type"),
    ]


class Incident(_Metadata):
    entity_type: Literal["incident"] = "incident"
    scope: PatientScope
    unique_source_key: NonblankStr
    facts: dict[str, JsonValue]
    severity: NonblankStr
    verified_status: Literal["unverified", "verified"] = "unverified"
    state: Literal["open", "resolved"] = "open"
    raised_at: UtcInstant
    review_obligation_id: NonblankStr
    alert_intent_ids: tuple[NonblankStr, ...]
    template_id: NonblankStr


class AuditEvent(_Metadata):
    entity_type: Literal["audit_event"] = "audit_event"
    event_id: NonblankStr
    command_id: NonblankStr
    scope: Scope
    event_type: NonblankStr
    aggregate_refs: tuple[VersionRef, ...] = ()
    before_versions: tuple[VersionRef, ...] = ()
    after_versions: tuple[VersionRef, ...] = ()
    actor: Principal
    accepted_at: UtcInstant
    policy_versions: tuple[NonblankStr, ...] = ()
    source_refs: tuple[NonblankStr, ...] = ()
    payload_ref: NonblankStr | None = None

    @model_validator(mode="after")
    def immutable(self) -> Self:
        if self.version != 1 or self.id != self.event_id:
            raise ValueError("audit events are immutable and use event_id as ID")
        if self.created_at != self.accepted_at or self.updated_at != self.accepted_at:
            raise ValueError("audit timestamps must equal accepted_at")
        return self


class InboundReceipt(_Metadata):
    entity_type: Literal["inbound_receipt"] = "inbound_receipt"
    scope: Scope
    transport: NonblankStr
    transport_key: NonblankStr
    source_subject: NonblankStr
    source_chat: NonblankStr
    channel: NonblankStr
    kind: NonblankStr
    payload: dict[str, JsonValue] | None = None
    payload_ref: NonblankStr | None = None
    provider_media_handle: NonblankStr | None = None
    received_at: UtcInstant
    safety_screen_state: Literal["pending", "screened"] = "pending"
    safety_policy_version: NonblankStr | None = None
    state: Literal["pending", "processing", "completed", "needs_attention"] = "pending"
    processing_claim: ProcessingClaim | None = None
    work_clock: OperationalClock | None
    result_event_ids: tuple[NonblankStr, ...] = ()
    principal: Principal | None = None
    safety_result: dict[str, JsonValue] | None = None
    review_obligation_id: NonblankStr | None = None

    @model_validator(mode="after")
    def recoverable(self) -> Self:
        if (self.payload is None) == (self.payload_ref is None):
            raise ValueError("supply exactly one recoverable payload or protected reference")
        if self.id != keys.inbound(self.transport, keys.digest(self.transport_key)).pk:
            raise ValueError("receipt ID must be its deterministic inbound key")
        if (self.state == "completed") != (self.work_clock is None):
            raise ValueError("unfinished receipts require a work clock")
        if self.work_clock is not None and self.work_clock.work_lane != "ingress":
            raise ValueError("receipt requires ingress clock")
        return self


class OutboundIntent(_Metadata):
    entity_type: Literal["outbound_intent"] = "outbound_intent"
    scope: Scope
    scope_kind: Literal["patient", "intake", "account"]
    audience: Literal["patient", "doctor", "applicant", "admin"]
    logical_key: NonblankStr
    source_event_ids: tuple[NonblankStr, ...]
    source_versions: tuple[VersionRef, ...]
    recipient_ref: NonblankStr
    notification_purpose: Literal[
        "solicited_reply",
        "DANGER",
        "DONE:FULFILLMENT",
        "DONE:CORRECTION",
        "DEADLINE",
        "routine_prompt",
        "patient_safety_response",
    ]
    eligibility_class: NonblankStr
    payload_ref: NonblankStr
    payload_digest: NonblankStr
    conversation_sequence: NonnegativeInt
    slot_id: NonblankStr | None = None
    expires_at: UtcInstant
    status: Literal["queued", "sending", "provider_accepted", "uncertain", "failed", "suppressed"]
    delivery_claim: ProcessingClaim | None = None
    delivery_lease_seconds: PositiveVersion
    accepted_message_id: NonblankStr | None = None
    accepted_at: UtcInstant | None = None
    retry_count: NonnegativeInt = 0
    uncertain_retry_count: NonnegativeInt = 0
    last_error: NonblankStr | None = None
    suppression_reason: NonblankStr | None = None
    work_clock: OperationalClock | None
    recipient_auth_epoch_seen: NonnegativeInt | None = None
    doctor_auth_epoch_seen: NonnegativeInt | None = None
    binding_epoch_seen: NonnegativeInt | None = None
    consent_version_seen: PositiveVersion | None = None
    safety_epoch_seen: NonnegativeInt | None = None
    delivery_epoch_seen: NonnegativeInt | None = None
    order_refs: tuple[VersionRef, ...] = ()
    template_id: NonblankStr | None = None
    review_obligation_id: NonblankStr | None = None
    active_attempt_id: NonblankStr | None = None
    retryable: StrictBool | None = None
    bot_id: NonblankStr | None = None
    recipient_subject: NonblankStr | None = None
    payload: dict[str, JsonValue] | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if self.scope_kind == "account":
            if (
                not isinstance(self.scope, AccountScope)
                or self.bot_id != self.scope.bot_id
                or self.recipient_subject is None
                or self.audience not in {"applicant", "admin", "doctor"}
                or self.notification_purpose not in {"solicited_reply", "patient_safety_response"}
                or not self.source_versions
                or self.payload is None
                or self.payload_digest != keys.digest(canonical_json(self.payload).decode())
                or any(
                    value is not None
                    for value in (
                        self.binding_epoch_seen,
                        self.consent_version_seen,
                        self.safety_epoch_seen,
                        self.delivery_epoch_seen,
                        self.doctor_auth_epoch_seen,
                        self.slot_id,
                    )
                )
                or self.order_refs
            ):
                raise ValueError("account intent requires its own recipient, source and payload")
        if self.scope_kind == "patient" and not isinstance(self.scope, PatientScope):
            raise ValueError("patient intent requires patient scope")
        if self.scope_kind == "intake" and (
            not isinstance(self.scope, IntakeScope) or self.audience != "doctor"
        ):
            raise ValueError("intake intent requires owning intake and doctor audience")
        terminal = self.status in {"provider_accepted", "suppressed"} or (
            self.status in {"uncertain", "failed"} and self.review_obligation_id is not None
        )
        if terminal != (self.work_clock is None):
            raise ValueError("unfinished delivery requires a work clock")
        if self.work_clock is not None and self.work_clock.work_lane != "delivery":
            raise ValueError("intent requires delivery clock")
        if self.status == "provider_accepted" and (
            self.accepted_message_id is None or self.accepted_at is None
        ):
            raise ValueError("acceptance requires provider evidence")
        return self


type DeliveryOutcome = Literal["provider_accepted", "uncertain", "definite_failure", "suppressed"]


class DeliveryAttempt(_Metadata):
    entity_type: Literal["delivery_attempt"] = "delivery_attempt"
    scope: Scope
    intent_id: NonblankStr
    attempt_id: NonblankStr
    lease_generation: PositiveVersion
    freshness_snapshot: tuple[VersionRef, ...]
    started_at: UtcInstant
    ended_at: UtcInstant | None = None
    outcome: Literal[
        "started", "provider_accepted", "uncertain", "definite_failure", "suppressed"
    ] = "started"
    provider_message_id: NonblankStr | None = None
    redacted_error_code: NonblankStr | None = None


MAX_SESSION_BYTES = 32 * 1024


class SessionSnapshot(_Metadata):
    entity_type: Literal["session_snapshot"] = "session_snapshot"
    scope: PatientScope
    role: Literal["coordinator", "concierge"]
    blob: dict[str, JsonValue]
    source_order_versions: tuple[VersionRef, ...] = ()
    safety_epoch: NonnegativeInt
    fence_generation: PositiveVersion
    session_version: PositiveVersion

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(canonical_json(self.blob)) > MAX_SESSION_BYTES:
            raise ValueError("session exceeds 32 KiB memory bound")
        if self.session_version != self.version:
            raise ValueError("session_version must equal record version")
        return self


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


class StoredRecord(_BoundaryValue):
    pk: NonblankStr
    sk: NonblankStr
    entity_type: NonblankStr
    id: NonblankStr
    version: PositiveVersion
    doctor_id: NonblankStr | None = None
    patient_id: NonblankStr | None = None
    body: dict[str, JsonValue]
    created_at: UtcInstant
    updated_at: UtcInstant
    ttl: NonnegativeInt | None = None
    processing_claim: ProcessingClaim | None = None
    due_lane_shard: str | None = None
    claim_generation: NonnegativeInt = 0
    due_sort: str | None = None
    review_pk: str | None = None
    review_sort: str | None = None
    patients_pk: str | None = None
    patients_sort: str | None = None

    @property
    def key(self) -> Key:
        return Key(self.pk, self.sk)

    def scoped_key(self, scope: Scope) -> ScopedKey:
        return ScopedKey(scope=scope, pk=self.pk, sk=self.sk)

    @property
    def ref(self) -> VersionRef:
        return VersionRef(entity_type=self.entity_type, id=self.id, version=self.version)


# These are envelope aliases: their decoded bodies are validated before a write.
type AuditEventRecord = StoredRecord
type OutboundIntentRecord = StoredRecord
type InboundReceiptRecord = StoredRecord


MODELS: dict[str, type[BaseModel]] = {
    "application": Application,
    "doctor": Doctor,
    "subject_binding": SubjectBinding,
    "callback_token": CallbackToken,
    "account_ack": AccountAcknowledgment,
    "operational_issue": OperationalIssue,
    "mission": Mission,
    "followup": FollowUpTask,
    "review": ReviewObligation,
    "patient_profile": PatientProfile,
    "audit_event": AuditEvent,
    "inbound_receipt": InboundReceipt,
    "outbound_intent": OutboundIntent,
    "delivery_attempt": DeliveryAttempt,
    "session_snapshot": SessionSnapshot,
    "doctor_authority": DoctorAuthority,
    "care_order": OrderAuthority,
    "evidence_annotation": EvidenceAnnotation,
    "incident": Incident,
}


def model_scope(model: BaseModel) -> Scope:
    if isinstance(
        model,
        (
            Application,
            Doctor,
            SubjectBinding,
            CallbackToken,
            AccountAcknowledgment,
            OperationalIssue,
        ),
    ):
        return model.scope
    if isinstance(model, DoctorAuthority):
        return TenantScope(doctor_id=model.doctor_id)
    if isinstance(model, (OrderAuthority, EvidenceAnnotation, Incident)):
        return model.scope
    if isinstance(model, (Mission, FollowUpTask, PatientProfile)):
        return PatientScope(doctor_id=model.doctor_id, patient_id=model.patient_id)
    if isinstance(model, ReviewObligation):
        if model.patient_id is not None:
            return PatientScope(doctor_id=model.owner_doctor_id, patient_id=model.patient_id)
        # Only the blueprint's intake row supports unassigned review in this slice.
        if model.source_type != "intake":
            raise ValueError("unassigned review requires an explicit intake source")
        return IntakeScope(doctor_id=model.owner_doctor_id, intake_id=model.source_id)
    if isinstance(
        model, (AuditEvent, InboundReceipt, OutboundIntent, DeliveryAttempt, SessionSnapshot)
    ):
        return model.scope
    raise ValueError("unsupported record model")


def scope_owns(scope: Scope, other: Scope) -> bool:
    if isinstance(scope, AccountScope) or isinstance(other, AccountScope):
        return scope == other
    if scope.doctor_id != other.doctor_id:
        return False
    if type(scope) is TenantScope:
        return True
    return scope == other


def model_key(model: BaseModel, scope: Scope) -> Key:
    if isinstance(model, Application):
        return Key(keys.partition(model.scope), f"APPLICATION#{model.id}")
    if isinstance(model, Doctor):
        return Key(keys.partition(model.scope), "DOCTOR")
    if isinstance(model, SubjectBinding):
        return keys.subject(model.scope.bot_id, model.telegram_user_id)
    if isinstance(model, CallbackToken):
        return keys.token("callback", model.id)
    if isinstance(model, AccountAcknowledgment):
        return Key(keys.partition(model.scope), f"ACK#{model.id}")
    if isinstance(model, OperationalIssue):
        return keys.operational(model.scope.bot_id, "ISSUE", model.id)
    if isinstance(model, DoctorAuthority):
        return keys.doctor(TenantScope(doctor_id=model.doctor_id))
    if isinstance(model, OrderAuthority):
        return keys.patient(model.scope, "ORDER", model.id)
    if isinstance(model, EvidenceAnnotation):
        return keys.patient(model.scope, "FACT", model.id)
    if isinstance(model, Incident):
        return keys.patient(model.scope, "INCIDENT", model.id)
    if isinstance(model, PatientProfile):
        assert isinstance(scope, PatientScope)
        return keys.patient(scope)
    if isinstance(model, (Mission, FollowUpTask)):
        assert isinstance(scope, PatientScope)
        return keys.patient(
            scope, "MISSION" if isinstance(model, Mission) else "FOLLOWUP", model.id
        )
    if isinstance(model, ReviewObligation):
        return Key(keys.partition(scope), f"REVIEW#{keys.component(model.id)}")
    if isinstance(model, AuditEvent):
        return keys.event(model.scope, model.accepted_at, model.event_id)
    if isinstance(model, InboundReceipt):
        return keys.inbound(model.transport, keys.digest(model.transport_key))
    if isinstance(model, OutboundIntent):
        return keys.outbox(scope, model.id)
    if isinstance(model, DeliveryAttempt):
        return keys.outbox(scope, model.intent_id, model.attempt_id)
    if isinstance(model, SessionSnapshot):
        return keys.patient(model.scope, "SESSION", model.id, role=model.role)
    raise ValueError("unsupported record model")


PROJECTION_FIELDS = (
    "due_lane_shard",
    "due_sort",
    "review_pk",
    "review_sort",
    "patients_pk",
    "patients_sort",
)


def projections(model: BaseModel) -> dict[str, str]:
    result = {}
    clock = getattr(model, "work_clock", None)
    if clock is not None:
        result["due_lane_shard"] = f"{clock.work_lane}#{clock.work_shard}"
        body = model.model_dump(mode="json")
        result["due_sort"] = (
            f"{keys.instant(clock.next_action_at)}#{body['entity_type']}#{body['id']}"
        )
    if isinstance(model, ReviewObligation) and model.state != "resolved":
        result["review_pk"] = keys.tenant_pk(TenantScope(doctor_id=model.owner_doctor_id))
        result["review_sort"] = f"{keys.instant(model.review_at)}#{model.id}"
    if isinstance(model, PatientProfile):
        result["patients_pk"] = keys.tenant_pk(TenantScope(doctor_id=model.doctor_id))
        result["patients_sort"] = f"{model.normalized_name}#{model.patient_id}"
    return result


def to_record(model: BaseModel, scope: Scope) -> StoredRecord:
    body = model.model_dump(mode="json")
    # Revalidate even a value produced by Pydantic's unchecked copy/construct APIs.
    model = type(model).model_validate(body)
    actual = model_scope(model)
    if not scope_owns(scope, actual):
        raise ValueError("record is outside authenticated scope")
    key = model_key(model, actual)
    return StoredRecord.model_validate(
        {
            "pk": key.pk,
            "sk": key.sk,
            "entity_type": body["entity_type"],
            "id": body["id"],
            "version": body["version"],
            "doctor_id": None if isinstance(actual, AccountScope) else actual.doctor_id,
            "patient_id": actual.patient_id if isinstance(actual, PatientScope) else None,
            "body": body,
            "created_at": body["created_at"],
            "updated_at": body["updated_at"],
            **projections(model),
        }
    )


def from_record[T: BaseModel](record: StoredRecord, model_type: type[T]) -> T:
    if MODELS.get(record.entity_type) is not model_type:
        raise ValueError("record type does not match decoder")
    model = model_type.model_validate(record.body)
    expected = to_record(model, model_scope(model))
    for field in (
        "pk",
        "sk",
        "entity_type",
        "id",
        "version",
        "doctor_id",
        "patient_id",
        "created_at",
        "updated_at",
    ):
        if getattr(record, field) != getattr(expected, field):
            raise ValueError("record envelope disagrees with validated body")
    return model


class Lease(_BoundaryValue):
    scope: PatientScope
    owner: NonblankStr
    generation: PositiveVersion
    expires_at: UtcInstant
    claimed_at: UtcInstant


class Claim(ProcessingClaim):
    record_key: ScopedKey
    version: PositiveVersion


class CommandEnvelope(_BoundaryValue):
    command_id: NonblankStr
    principal: Principal
    scope: Scope
    expected_versions: tuple[VersionRef, ...] = ()
    expected_auth_epoch: NonnegativeInt | None = None
    expected_binding_epoch: NonnegativeInt | None = None
    expected_consent_version: PositiveVersion | None = None
    expected_delivery_epoch: NonnegativeInt | None = None
    expected_safety_epoch: NonnegativeInt | None = None
    payload: dict[str, JsonValue]
    requested_at: UtcInstant
    fence: Lease | None = None
    work_claim: Claim | None = None
    worker: WorkerCapability | None = None


class ReceiptCompletion(_BoundaryValue):
    claim: Claim
    result_event_ids: tuple[NonblankStr, ...]


class MarkerRecord(_BoundaryValue):
    """Uniqueness reference only; no arbitrary payload/clinical content."""

    scope: Scope
    pk: NonblankStr
    sk: NonblankStr
    target: ScopedKey
    created_at: UtcInstant
    ttl: NonnegativeInt | None = None

    @property
    def key(self) -> Key:
        return Key(self.pk, self.sk)


class CommitRequest(_BoundaryValue):
    command: CommandEnvelope
    expected: tuple[VersionRef, ...] = ()
    puts: tuple[StoredRecord, ...] = ()
    events: tuple[AuditEventRecord, ...] = ()
    intents: tuple[OutboundIntentRecord, ...] = ()
    markers: tuple[MarkerRecord, ...] = ()
    receipt_completion: ReceiptCompletion | None = None
    command_status: Literal[
        "accepted", "needs_confirmation", "invalid_input", "forbidden", "unsupported"
    ] = "accepted"
    reason_code: NonblankStr | None = None


class DeliveryResolution(_BoundaryValue):
    """A dispatcher-selected outcome applied atomically with its attempt and review."""

    intent: StoredRecord
    reviews: tuple[StoredRecord, ...] = ()
    release_reservation: StrictBool = False


class Accepted(_BoundaryValue):
    status: Literal["accepted"] = "accepted"
    event_ids: tuple[str, ...]
    resulting_versions: tuple[VersionRef, ...]
    command_status: Literal[
        "accepted", "needs_confirmation", "invalid_input", "forbidden", "unsupported"
    ] = "accepted"
    reason_code: NonblankStr | None = None


class StaleVersion(_BoundaryValue):
    status: Literal["stale_version"] = "stale_version"
    conflicts: tuple[str, ...] = ()


class Duplicate(_BoundaryValue):
    status: Literal["duplicate"] = "duplicate"
    original: Accepted


class Forbidden(_BoundaryValue):
    status: Literal["forbidden"] = "forbidden"


class TooLarge(_BoundaryValue):
    status: Literal["too_large"] = "too_large"
    reason: Literal["item_count", "item_bytes", "transaction_bytes"]


type CommitResult = Annotated[
    Accepted | StaleVersion | Duplicate | Forbidden | TooLarge, Field(discriminator="status")
]


class InboundAccept(_BoundaryValue):
    status: Literal["created", "existing", "forbidden", "conflict"]
    record: StoredRecord | None = None
    state: str | None = None


class WorkerCapability(_BoundaryValue):
    """Internal dispatcher-established capability; never a model/browser input."""

    service_subject: NonblankStr
    permitted_lanes: frozenset[NonblankStr]
    resolved_scope: Scope
    auth_expiry: UtcInstant
    invocation_id: NonblankStr


class DueItem(_BoundaryValue):
    record_key: ScopedKey
    entity_type: NonblankStr
    id: NonblankStr
    next_action_at: UtcInstant


class Cursor(_BoundaryValue):
    query: NonblankStr
    position: dict[str, str]


type RecordPage = tuple[tuple[StoredRecord, ...], Cursor | None]
type DuePage = tuple[tuple[DueItem, ...], Cursor | None]


class ReviewCreation(_BoundaryValue):
    scope: Scope
    review: ReviewObligation


class ReconcileReport(_BoundaryValue):
    examined: NonnegativeInt = 0
    inconsistent: tuple[Key, ...] = ()
    repaired: tuple[Key, ...] = ()
    conflicts: tuple[Key, ...] = ()
    unrepairable: tuple[Key, ...] = ()
    cursor: Cursor | None = None


def record_item(record: StoredRecord) -> dict[str, Any]:
    item = record.model_dump(mode="json", exclude_none=True)
    item["PK"] = item.pop("pk")
    item["SK"] = item.pop("sk")
    # Preserve exact JSON numbers without DynamoDB Decimal/float coercion.
    item["body"] = canonical_json(record.body).decode("utf-8")
    return item


def item_record(item: dict[str, Any]) -> StoredRecord:
    data = item.copy()
    data["pk"] = data.pop("PK")
    data["sk"] = data.pop("SK")
    data["body"] = json.loads(data["body"])
    return StoredRecord.model_validate(data)


CommandEnvelope.model_rebuild()
CommitRequest.model_rebuild()
