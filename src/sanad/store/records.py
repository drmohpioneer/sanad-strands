"""Slice 02 storage values; no transport, policy selection or clinical transitions.

The accepted domain provides the three clinical aggregates. Command, audit,
receipt, delivery and memory envelopes were blueprint-only at slice 01 and are
defined here to support persistence, without adding clinical authority.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any, Generic, Literal, Self

from pydantic import BaseModel, Field, JsonValue, StrictBool, model_validator
from typing_extensions import TypeVar

from sanad.concierge.records import BarrierOutcome, BarrierReservation, PatientAction
from sanad.corrections import Correction, CorrectionOffer, FactHead
from sanad.domain import (
    FollowUpTask,
    Mission,
    NonblankStr,
    NonnegativeInt,
    PatientScope,
    PositiveVersion,
    Principal,
    Provenance,
    ReviewObligation,
    TenantScope,
    UtcInstant,
    VersionRef,
)
from sanad.domain.boundaries import IanaZone, _BoundaryValue
from sanad.domain.events import RecordEvidenceAssociation, RetainObservation, SupersedeEvidence
from sanad.domain.language import default_language
from sanad.domain.operations import OperationalClock as OperationalClock
from sanad.domain.predicates import PredicateResult
from sanad.liaison.records import (
    Notice,
    QuestionBinding,
    ReusableAnswer,
    ReuseOffer,
    ReviewOffer,
    ReviewSnapshot,
)
from sanad.media.vision import Disagreement, DocumentItem, DocumentRead, ReaderResult
from sanad.scribe.extract import LabRowCandidate
from sanad.scribe.proposal import InvitationWork, Proposal, ScribeCallback, ScribeState
from sanad.scribe.records import CareOrderHead, CareOrderVersion, CarePlan, ClinicalFact
from sanad.store import keys
from sanad.store.keys import AccountScope, IntakeScope, Key, Scope, ScopedKey


class ProcessingClaim(_BoundaryValue):
    attempt_charged: bool = True
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


class SweepPosition(_Metadata):
    entity_type: Literal["sweep_position"] = "sweep_position"
    scope: AccountScope
    lane: NonblankStr
    shard: NonblankStr
    due_sort: str | None = None
    due_pk: str | None = None
    due_sk: str | None = None

    @model_validator(mode="after")
    def position_key(self) -> Self:
        if self.id != keys.component(self.lane) + "#" + keys.component(self.shard):
            raise ValueError("position identity must bind lane and shard")
        if (self.due_sort is None) != (self.due_pk is None) or (self.due_sort is None) != (
            self.due_sk is None
        ):
            raise ValueError("position key must be complete or reset")
        return self


class IdentityConfig(_BoundaryValue):
    """Nonsecret settings projected into the persistence boundary."""

    bot_id: NonblankStr
    admin_user_id: NonblankStr


class NameMemory(_Metadata):
    entity_type: Literal["name_memory"] = "name_memory"
    scope: TenantScope | AccountScope
    latin: Annotated[str, Field(min_length=1, max_length=120)]
    generic: Annotated[str, Field(max_length=240)] = ""
    kind: Literal["drug", "test", "finding"]
    spoken_forms: Annotated[tuple[str, ...], Field(max_length=10)] = Field(default=(), repr=False)
    strengths_seen: tuple[str, ...] = ()
    confirmations: PositiveVersion = 1
    last_confirmed_at: UtcInstant
    source: Literal["doctor_confirmation", "rxnorm", "seed"]

    @model_validator(mode="after")
    def vocabulary_scope(self) -> Self:
        from sanad.scribe.names import normalize

        if type(self.scope) is not TenantScope and type(self.scope) is not AccountScope:
            raise ValueError("name memory requires exact doctor or clinic scope")
        if self.id != keys.digest(
            keys.partition(self.scope) + ":" + self.kind + ":" + normalize(self.latin)
        ):
            raise ValueError("name memory requires its vocabulary digest")
        if (self.kind != "finding" and not self.latin.isascii()) or any(
            len(s) > 120 for s in self.spoken_forms
        ):
            raise ValueError("name memory contains invalid vocabulary")
        return self


NAME_CACHE_SCOPE = AccountScope(bot_id="rxnorm-name-cache")


class NameCache(_Metadata):
    entity_type: Literal["name_cache"] = "name_cache"
    scope: AccountScope = NAME_CACHE_SCOPE
    query: Annotated[str, Field(min_length=1, max_length=120)]
    canonical: Annotated[str, Field(min_length=1, max_length=120)]
    generic: Annotated[str, Field(min_length=1, max_length=240)]
    strengths: tuple[str, ...] = ()
    source: Literal["rxnorm"] = "rxnorm"
    expires_at: UtcInstant

    @model_validator(mode="after")
    def public_vocabulary(self) -> Self:
        from sanad.scribe.names import normalize

        if self.scope != NAME_CACHE_SCOPE or self.id != keys.digest(normalize(self.query)):
            raise ValueError("RxNorm cache requires its fixed partition and query digest")
        if not all(
            s.isascii() for s in (self.query, self.canonical, self.generic, *self.strengths)
        ):
            raise ValueError("RxNorm cache accepts public Latin vocabulary only")
        if self.expires_at <= self.updated_at:
            raise ValueError("cache expiry must follow the fetch")
        return self


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
    decision_reason: Literal["admin_rejected", "unverified"] | None = None
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
    language: Literal["ar", "en"] = default_language
    digest_time: Annotated[
        str, Field(strict=True, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    ] = "20:00"
    digest_packing: Literal["one", "each"] = "one"
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


class AdminAccount(_Metadata):
    entity_type: Literal["admin_account"] = "admin_account"
    scope: AccountScope
    auth_epoch: NonnegativeInt = 1

    @model_validator(mode="after")
    def admin_identity(self) -> Self:
        keys.subject(self.scope.bot_id, self.id)
        return self


class AuthorizationUnavailable(RuntimeError):
    """A consistent authority snapshot could not be read; this is not revocation."""


class Authorization(_BoundaryValue):
    principal: Principal
    binding: SubjectBinding | None = None
    doctor_status: Literal["pending", "approved", "rejected", "suspended", "revoked"] | None = None
    auth_epoch: NonnegativeInt | None = None
    admin_epoch: NonnegativeInt | None = None
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


class PendingStartClarification(_BoundaryValue):
    mission_ref: VersionRef
    asked_at: UtcInstant
    expires_at: UtcInstant

    @model_validator(mode="after")
    def shape(self) -> Self:
        if self.mission_ref.entity_type != "mission" or self.expires_at <= self.asked_at:
            raise ValueError("start clarification requires a mission and a future expiry")
        return self


class PatientProfile(_Metadata):
    """Operational authority facts, including the accepted enrollment facts."""

    entity_type: Literal["patient_profile"] = "patient_profile"
    doctor_id: NonblankStr
    patient_id: NonblankStr
    removed_at: UtcInstant | None = None
    removed_by: NonblankStr | None = None
    purge_due_at: UtcInstant | None = None
    normalized_name: str = ""
    lease_owner: NonblankStr | None = None
    lease_expires_at: UtcInstant | None = None
    lease_generation: NonnegativeInt = 0
    safety_epoch: NonnegativeInt = 0
    delivery_epoch: NonnegativeInt = 0
    binding_epoch: NonnegativeInt = 0
    consent_version: PositiveVersion | None = None
    consent_active: StrictBool = False
    routine_contact_enabled: StrictBool = True
    routine_paused_until: UtcInstant | None = None
    last_chase_accepted_at: UtcInstant | None = None
    binding_active: StrictBool = False
    recipient_ref: NonblankStr | None = None
    recipient_subject: NonblankStr | None = None
    recipient_auth_epoch: NonnegativeInt = 0
    pending_start_clarification: PendingStartClarification | None = None

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.id != self.patient_id:
            raise ValueError("profile ID must be the patient ID")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("lease owner and expiry must be supplied together")
        return self


class Patient(_Metadata):
    entity_type: Literal["patient"] = "patient"
    scope: PatientScope
    display_name: Annotated[str, Field(strict=True, min_length=1, max_length=160)]
    language: Literal["ar", "en"] = default_language
    timezone: IanaZone
    contact_status: Literal[
        "awaiting_link", "active", "paused", "opted_out", "unreachable", "frozen"
    ] = "awaiting_link"
    resume_at: UtcInstant | None = None
    record_version: PositiveVersion = 1
    identifiers: tuple[str, ...] = ()
    age: str | None = None
    sex: Literal["male", "female"] | None = None
    current_plan_id: str | None = None
    delivery_epoch: NonnegativeInt = 0
    binding_epoch: NonnegativeInt = 0
    active_binding_id: NonblankStr | None = None
    consent_id: NonblankStr | None = None
    consent_version: PositiveVersion | None = None
    invitation_id: NonblankStr | None = None
    invitation_generation: NonnegativeInt = 0

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.id != self.scope.patient_id or self.version != self.record_version:
            raise ValueError("patient identity/version mismatch")
        if self.contact_status == "active" and any(
            x is None for x in (self.active_binding_id, self.consent_id, self.consent_version)
        ):
            raise ValueError("active patient requires binding and consent")
        return self


class ConsentOffer(_BoundaryValue):
    generation: PositiveVersion
    text_version: NonblankStr
    language: Literal["ar", "en"]
    short_text: NonblankStr
    full_text: NonblankStr
    configuration: dict[str, JsonValue]
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Consent(_Metadata):
    entity_type: Literal["consent"] = "consent"
    scope: PatientScope
    binding_id: NonblankStr
    policy_text_version: NonblankStr
    offer_claim_id: NonblankStr | None = None
    offer_generation: PositiveVersion | None = None
    language: Literal["ar", "en"] | None = None
    accepted_at: UtcInstant
    accepted_by: NonblankStr
    permitted_channels: frozenset[Literal["telegram"]] = frozenset({"telegram"})
    routine_contact_enabled: StrictBool = True
    urgent_response_policy_id: NonblankStr
    scheduled_slot_consents: tuple[str, ...] = ()
    quiet_hours: tuple[str, str]
    timezone: IanaZone
    policy_digest: NonblankStr
    withdrawn_at: UtcInstant | None = None


class PatientBinding(_Metadata):
    entity_type: Literal["patient_binding"] = "patient_binding"
    scope: PatientScope
    bot_id: NonblankStr
    subject: NonblankStr
    private_chat_id: NonblankStr
    status: Literal["active", "frozen", "revoked"] = "active"
    binding_epoch: PositiveVersion = 1
    claim_id: NonblankStr
    consent_id: NonblankStr
    consent_version: PositiveVersion
    doctor_confirmed_by: NonblankStr
    doctor_confirmed_at: UtcInstant
    reason_code: NonblankStr | None = None


class TokenHead(_Metadata):
    """One serialization point per subject/purpose or patient invitation generation."""

    entity_type: Literal["token_head"] = "token_head"
    scope: AccountScope
    purpose: Literal["doctor_login", "patient_login", "admin_login", "invitation"]
    owner_key: NonblankStr
    token_hash: NonblankStr

    @model_validator(mode="after")
    def hashed(self) -> Self:
        keys.token(self.purpose, self.token_hash)
        if self.id != keys.digest(f"{self.purpose}:{self.owner_key}"):
            raise ValueError("token head identity mismatch")
        return self


class LoginExchange(_Metadata):
    removal_destination: str | None = Field(default=None, exclude_if=lambda value: value is None)
    removal_binding_hash: str | None = Field(default=None, exclude_if=lambda value: value is None)
    entity_type: Literal["doctor_login", "patient_login", "admin_login"]
    scope: AccountScope
    intended_role: Literal["doctor", "patient", "admin"]
    subject: NonblankStr
    doctor_id: NonblankStr | None
    patient_id: NonblankStr | None = None
    binding_id: NonblankStr | None = None
    binding_epoch: NonnegativeInt | None = None
    consent_version: PositiveVersion | None = None
    auth_epoch: NonnegativeInt
    issued_at: UtcInstant
    expires_at: UtcInstant
    state: Literal["issued", "consumed", "revoked", "expired"] = "issued"
    consumed_at: UtcInstant | None = None
    policy_version: NonblankStr

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.removal_destination is not None:
            import re

            if self.intended_role != "doctor" or not re.fullmatch(
                r"/a/patients/[A-Za-z0-9_-]+", self.removal_destination
            ):
                raise ValueError("invalid removal destination")
            if self.removal_binding_hash != keys.digest(
                f"{self.id}|{self.doctor_id}|{self.removal_destination}"
            ):
                raise ValueError("removal destination hash mismatch")
        elif self.removal_binding_hash is not None:
            raise ValueError("removal hash requires a destination")
        keys.token(self.entity_type, self.id)
        if self.entity_type != self.intended_role + "_login":
            raise ValueError("exchange role mismatch")
        if (self.intended_role == "admin") != (self.doctor_id is None):
            raise ValueError("role/doctor mismatch")
        patient_fields = (
            self.patient_id,
            self.binding_id,
            self.binding_epoch,
            self.consent_version,
        )
        if (self.intended_role == "patient" and any(x is None for x in patient_fields)) or (
            self.intended_role != "patient" and any(x is not None for x in patient_fields)
        ):
            raise ValueError("exchange binding mismatch")
        if (self.state == "consumed") != (self.consumed_at is not None):
            raise ValueError("consumption requires timestamp")
        return self


class Invitation(_Metadata):
    entity_type: Literal["invitation"] = "invitation"
    scope: AccountScope
    doctor_id: NonblankStr
    patient_id: NonblankStr
    issued_by: NonblankStr
    generation: PositiveVersion
    expires_at: UtcInstant
    review_at: UtcInstant
    state: Literal["issued", "claimed", "consumed", "revoked", "expired"] = "issued"
    pending_claim_id: NonblankStr | None = None
    consumed_at: UtcInstant | None = None
    work_clock: OperationalClock | None
    policy_version: NonblankStr

    @model_validator(mode="after")
    def lifecycle(self) -> Self:
        keys.token("invitation", self.id)
        if (self.state in {"issued", "claimed"}) != (self.work_clock is not None):
            raise ValueError("unfinished invitation requires claim clock")
        if self.work_clock and self.work_clock.work_lane != "claim":
            raise ValueError("invitation requires claim lane")
        if self.review_at != self.expires_at:
            raise ValueError("invitation review must equal expiry")
        if self.state in {"claimed", "consumed"} and self.pending_claim_id is None:
            raise ValueError("claimed invitation requires claim")
        if (self.state == "consumed") != (self.consumed_at is not None):
            raise ValueError("consumption requires timestamp")
        return self


class PatientClaim(_Metadata):
    entity_type: Literal["patient_claim"] = "patient_claim"
    scope: AccountScope
    doctor_id: NonblankStr
    patient_id: NonblankStr
    invitation_id: NonblankStr
    invitation_generation: PositiveVersion
    patient_version: PositiveVersion
    candidate_subject: NonblankStr
    private_chat_id: NonblankStr
    minimal_claim_identifier: NonblankStr
    binding_id: NonblankStr
    consent_id: NonblankStr | None = None
    consent_version: PositiveVersion | None = None
    consent_policy: dict[str, JsonValue]
    offer_generation: NonnegativeInt = 0
    consent_offers: tuple[ConsentOffer, ...] = ()
    state: Literal["pending", "approved", "rejected", "expired"] = "pending"
    proof_method: Literal["verified_private_telegram", "doctor_confirmation"]
    proof_reference: NonblankStr
    doctor_confirmed_by: NonblankStr | None = None
    doctor_confirmed_at: UtcInstant | None = None
    review_at: UtcInstant
    work_clock: OperationalClock | None

    @model_validator(mode="after")
    def lifecycle(self) -> Self:
        if (self.state == "pending") != (self.work_clock is not None):
            raise ValueError("pending claim requires claim clock")
        if self.work_clock and self.work_clock.work_lane != "claim":
            raise ValueError("claim requires claim lane")
        if self.private_chat_id != self.candidate_subject:
            raise ValueError("claim requires verified private subject")
        if self.state == "approved" and any(
            x is None
            for x in (
                self.consent_id,
                self.consent_version,
                self.doctor_confirmed_by,
                self.doctor_confirmed_at,
            )
        ):
            raise ValueError("approved claim requires consent and confirmation")
        return self


class ClaimCallback(_Metadata):
    entity_type: Literal["claim_callback"] = "claim_callback"
    scope: AccountScope
    claim_id: NonblankStr
    actor_subject: NonblankStr
    action: Literal["read_terms", "accept", "decline", "confirm", "reject"]
    offer_generation: NonnegativeInt = 0
    expected_versions: tuple[VersionRef, ...]
    expires_at: UtcInstant
    consumed_at: UtcInstant | None = None

    @model_validator(mode="after")
    def hashed(self) -> Self:
        keys.token("claim_callback", self.id)
        return self


class PreSession(_Metadata):
    entity_type: Literal["pre_session"] = "pre_session"
    scope: AccountScope
    csrf_secret_ref: NonblankStr
    expires_at: UtcInstant
    consumed_at: UtcInstant | None = None

    @model_validator(mode="after")
    def hashed(self) -> Self:
        keys.token("pre_session", self.id)
        keys.token("csrf", self.csrf_secret_ref)
        return self


SessionDoctor = TypeVar("SessionDoctor", bound=str | None, default=str, covariant=True)


class WebSession(_Metadata, Generic[SessionDoctor]):
    entity_type: Literal["web_session"] = "web_session"
    scope: AccountScope
    role: Literal["doctor", "patient", "admin"]
    subject: NonblankStr
    doctor_id: SessionDoctor
    patient_id: NonblankStr | None = None
    binding_id: NonblankStr | None = None
    auth_epoch: NonnegativeInt
    binding_epoch: NonnegativeInt | None = None
    consent_version: PositiveVersion | None = None
    csrf_secret_ref: NonblankStr
    issued_at: UtcInstant
    last_seen_at: UtcInstant
    idle_expires_at: UtcInstant
    absolute_expires_at: UtcInstant
    revoked_at: UtcInstant | None = None
    revocation_reason: NonblankStr | None = None

    @model_validator(mode="after")
    def hashed(self) -> Self:
        keys.web_session(self.id)
        keys.token("csrf", self.csrf_secret_ref)
        if self.doctor_id is not None and not self.doctor_id.strip():
            raise ValueError("doctor ID requires a nonblank string")
        if (self.role == "admin") != (self.doctor_id is None):
            raise ValueError("role/doctor mismatch")
        patient_fields = (
            self.patient_id,
            self.binding_id,
            self.binding_epoch,
            self.consent_version,
        )
        if (self.role == "patient" and any(x is None for x in patient_fields)) or (
            self.role != "patient" and any(x is not None for x in patient_fields)
        ):
            raise ValueError("session role/binding mismatch")
        if not self.issued_at <= self.last_seen_at < self.absolute_expires_at:
            raise ValueError("session time mismatch")
        return self


AnyWebSession = WebSession[str | None]


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


class Evidence(_Metadata):
    """Append-only document version; id includes its evidence version like orders."""

    entity_type: Literal["evidence"] = "evidence"
    scope: PatientScope
    evidence_id: NonblankStr
    observation_id: NonblankStr
    media_id: NonblankStr
    content_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    category: Literal[
        "lab_result",
        "imaging_report",
        "discharge_summary",
        "prescription",
        "medication_list",
        "monitor_screen",
        "other",
    ]
    printed_identity: str | None = Field(default=None, repr=False)
    printed_date: date | None = None
    association_state: Literal[
        "unmatched", "candidate", "accepted_pending_identity", "accepted", "rejected", "detached"
    ]
    patient_match_provenance: (
        Literal["caption", "single_open_mission", "patient_choice", "doctor_choice"] | None
    ) = None
    extracted_values: tuple[LabRowCandidate | DocumentItem, ...] = Field(default=(), repr=False)
    readers: tuple[ReaderResult, ReaderResult] = Field(repr=False)
    document_pages: tuple[DocumentPageManifest, ...] = ()
    row_pages: tuple[tuple[int, ...], ...] = ()
    blocked_pages: tuple[int, ...] = ()
    disagreements: tuple[Disagreement, ...] = ()
    shift_guard_fired: bool = False
    required_predicate_results: tuple[PredicateResult, ...] = ()
    accepted_by: str | None = None
    accepted_at: UtcInstant | None = None
    mission_id: str | None = None
    supersedes_evidence_id: str | None = None
    supersedes_evidence_version: PositiveVersion | None = None
    provenance: Provenance
    flags: tuple[str, ...] = ()
    incident_ids: tuple[str, ...] = ()
    candidate_mission_ids: tuple[str, ...] = ()
    rejection_reason: str | None = None
    correction_id: str | None = None

    @model_validator(mode="after")
    def evidence_identity(self) -> Self:
        if self.id != f"{self.evidence_id}:{self.version}":
            raise ValueError("immutable evidence identity must name its version")
        if self.observation_id != self.provenance.source_observation_id:
            raise ValueError("evidence receipt and provenance must agree")
        if self.association_state == "detached" and not (
            self.correction_id and self.rejection_reason and self.supersedes_evidence_version
        ):
            raise ValueError("detachment requires a correcting authority and predecessor")
        retained = self.association_state in {"accepted", "accepted_pending_identity"}
        if retained != bool(self.accepted_by and self.accepted_at):
            raise ValueError("accepted evidence requires attributable acceptance")
        if retained and not self.mission_id:
            raise ValueError("acceptance requires a scoped mission")
        if self.association_state == "accepted_pending_identity" and not self.identity_pending:
            raise ValueError("pending identity requires an unconfirmed identity hint")
        return self

    @property
    def identity_pending(self) -> bool:
        return "identity_unverifiable" in self.flags and "identity_confirmed" not in self.flags


class EvidenceHead(_Metadata):
    entity_type: Literal["evidence_head"] = "evidence_head"
    scope: PatientScope
    evidence_id: NonblankStr
    current_version: PositiveVersion
    status: Literal[
        "candidate", "accepted_pending_identity", "accepted", "rejected", "superseded", "detached"
    ]
    mission_id: str | None = None

    @model_validator(mode="after")
    def evidence_identity(self) -> Self:
        if self.id != self.evidence_id:
            raise ValueError("head must name its evidence")
        return self


class EvidenceHash(_Metadata):
    entity_type: Literal["evidence_hash"] = "evidence_hash"
    scope: PatientScope
    evidence_id: NonblankStr
    id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class EvidenceAction(_Metadata):
    entity_type: Literal["evidence_action"] = "evidence_action"
    scope: PatientScope
    evidence_id: NonblankStr
    evidence_version: PositiveVersion
    actor_subject: NonblankStr = Field(repr=False)
    action: Literal["associate", "accept", "reject", "confirm_identity"]
    mission_id: str | None = None
    expires_at: UtcInstant
    consumed_at: UtcInstant | None = None
    auth_epoch: NonnegativeInt


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
    prior_delivery_refs: tuple[NonblankStr, ...] = ()
    alert_intent_ids: tuple[NonblankStr, ...]
    template_id: NonblankStr


class AuditEvent(_Metadata):
    channel: NonblankStr | None = Field(default=None, exclude_if=lambda value: value is None)
    question_command: dict[str, JsonValue] | None = Field(default=None, repr=False)
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
    payload: dict[str, JsonValue] | None = Field(default=None, repr=False)
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
    barrier_reservation: BarrierReservation | None = Field(default=None, repr=False)
    barrier_outcome: BarrierOutcome | None = Field(default=None, repr=False)

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


class UploadStage(_Metadata):
    """Private reservation; attachment is single-use, retrieval is repeatable."""

    entity_type: Literal["upload_stage"] = "upload_stage"
    id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    scope: PatientScope
    subject: NonblankStr
    session_scope: AccountScope
    session_id: NonblankStr
    binding_id: NonblankStr
    binding_epoch: NonnegativeInt
    consent_version: PositiveVersion
    auth_epoch: NonnegativeInt
    content_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    content_type: NonblankStr
    size: Annotated[int, Field(gt=0, le=20_000_000)]
    object_ref: NonblankStr
    receipt: InboundReceipt = Field(repr=False)
    state: Literal["reserved", "attached", "discarded"] = "reserved"
    recovery_deadline: UtcInstant
    work_clock: OperationalClock | None

    @model_validator(mode="after")
    def staging(self) -> Self:
        if self.content_type != "application/pdf" and self.size > 8 * 1024 * 1024:
            raise ValueError("image_too_large")
        if (
            self.receipt.scope != self.scope
            or self.receipt.source_subject != self.subject
            or self.receipt.provider_media_handle != "upload:" + self.id
            or self.receipt.transport != "browser"
            or self.receipt.channel != "browser"
            or self.receipt.kind not in {"photo", "document"}
            or self.receipt.safety_screen_state != "screened"
            or self.receipt.safety_result is None
            or self.receipt.principal is None
            or self.receipt.principal.actor_kind != "patient"
            or self.receipt.state != "pending"
            or self.receipt.version != 1
        ):
            raise ValueError("invalid_upload_receipt")
        if (self.state == "attached") != (self.work_clock is None):
            raise ValueError("unlinked_upload_requires_recovery")
        if self.work_clock and self.work_clock.work_lane != "operational":
            raise ValueError("upload_requires_own_lane")
        return self


class PhotoAssociationWork(_Metadata):
    entity_type: Literal["photo_association_work"] = "photo_association_work"
    scope: TenantScope
    patient_id: NonblankStr
    intake_id: NonblankStr
    proposal_id: NonblankStr
    state: Literal["pending", "completed"] = "pending"
    work_clock: OperationalClock | None

    @model_validator(mode="after")
    def lifecycle(self) -> Self:
        if (self.state == "pending") != (self.work_clock is not None):
            raise ValueError("photo association requires recoverable work")
        return self


class IntakeDraft(_Metadata):
    entity_type: Literal["intake_draft"] = "intake_draft"
    scope: TenantScope
    owner_doctor_id: NonblankStr
    source_receipt_ids: tuple[NonblankStr, ...]
    media_work_ids: tuple[NonblankStr, ...]
    reads: DocumentRead = Field(repr=False)
    document_page_refs: tuple[str, ...] = ()
    reader_policy_version: NonblankStr
    kind: Literal["prescription", "lab", "other"]
    proposal_id: str | None = None
    selected_patient_id: str | None = None
    state: Literal["pending", "associated", "rejected"] = "pending"
    safety_epoch: NonnegativeInt = 0
    processing_claim: ProcessingClaim | None = None
    review_at: UtcInstant
    work_clock: OperationalClock | None
    review_obligation_id: str | None = None

    @model_validator(mode="after")
    def lifecycle(self) -> Self:
        if type(self.scope) is not TenantScope or self.scope.doctor_id != self.owner_doctor_id:
            raise ValueError("intake requires its owning doctor")
        if self.state == "pending" and not (self.work_clock or self.review_obligation_id):
            raise ValueError("pending intake requires timed ownership")
        if self.state != "pending" and self.work_clock:
            raise ValueError("associated intake hands work to its proposal")
        return self


class IntakeCallback(_Metadata):
    entity_type: Literal["intake_callback"] = "intake_callback"
    scope: TenantScope
    intake_id: NonblankStr
    intake_version: PositiveVersion
    actor_subject: NonblankStr
    action: Literal["select", "new", "later"]
    patient_id: str | None = None
    expires_at: UtcInstant
    consumed_at: UtcInstant | None = None

    @model_validator(mode="after")
    def binding(self) -> Self:
        keys.token("intake", self.id)
        if (self.action == "select") != (self.patient_id is not None):
            raise ValueError("selection requires an explicit scoped patient")
        return self


class IntakeConcern(_Metadata):
    entity_type: Literal["intake_concern"] = "intake_concern"
    scope: IntakeScope
    intake_id: NonblankStr
    unique_source_key: NonblankStr
    source_receipt_ids: tuple[NonblankStr, ...]
    rule_family: NonblankStr = "lab"
    rule_version: NonblankStr
    severity: NonblankStr
    facts: dict[str, JsonValue] = Field(repr=False)
    state: Literal["open", "associated", "resolved"] = "open"
    owner_doctor_id: NonblankStr
    review_obligation_id: NonblankStr
    associated_patient_id: str | None = None
    associated_event_id: str | None = None
    prior_delivery_refs: tuple[NonblankStr, ...] = ()

    @model_validator(mode="after")
    def ownership(self) -> Self:
        if self.scope.doctor_id != self.owner_doctor_id or self.scope.intake_id != self.intake_id:
            raise ValueError("intake concern ownership mismatch")
        return self


class PatientMedia(_Metadata):
    entity_type: Literal["patient_media"] = "patient_media"
    scope: PatientScope
    media_scope: IntakeScope | PatientScope
    media_work_id: NonblankStr
    source_receipt_id: NonblankStr
    kind: Literal["prescription", "lab", "other"]
    mime: NonblankStr

    @model_validator(mode="after")
    def ownership(self) -> Self:
        if self.media_scope.doctor_id != self.scope.doctor_id:
            raise ValueError("media must belong to the same doctor")
        if isinstance(self.media_scope, PatientScope) and self.media_scope != self.scope:
            raise ValueError("media belongs to another patient")
        return self


class MediaWork(_Metadata):
    """Recoverable extraction work; association never follows merely from a read."""

    infrastructure_deferrals: NonnegativeInt = 0
    entity_type: Literal["media_work"] = "media_work"
    scope: PatientScope | IntakeScope
    receipt_id: NonblankStr
    provider_handle_ref: NonblankStr = Field(repr=False)
    source_blob_ref: NonblankStr | None = None
    normalized_blob_ref: NonblankStr | None = None
    byte_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    mime: NonblankStr | None = None
    size: NonnegativeInt | None = None
    duration: Annotated[float, Field(gt=0, le=300, allow_inf_nan=False)] | None = None
    stage: Literal["fetch", "normalize", "extract", "associate"] = "fetch"
    state: Literal["pending", "processing", "completed", "needs_attention", "removed"] = "pending"
    processing_claim: ProcessingClaim | None = None
    work_clock: OperationalClock | None
    last_error: NonblankStr | None = None
    resend_intent_id: NonblankStr | None = None
    review_obligation_id: NonblankStr | None = None
    transcript_ref: NonblankStr | None = None
    association_ref: NonblankStr | None = None
    pending_mission_ids: tuple[str, ...] = ()
    document_pages: tuple[DocumentPageManifest, ...] = ()

    @model_validator(mode="after")
    def recoverable(self) -> Self:
        if self.document_pages and (
            self.mime != "application/pdf"
            or tuple(p.page_index for p in self.document_pages)
            != tuple(range(1, len(self.document_pages) + 1))
            or len({p.work_id for p in self.document_pages}) != len(self.document_pages)
        ):
            raise ValueError("invalid_document_manifest")
        if self.id != keys.digest(self.receipt_id):
            raise ValueError("media work identity must derive from its scoped receipt")
        if (self.state in {"completed", "removed"}) != (self.work_clock is None):
            raise ValueError("unfinished media work requires a clock")
        if self.work_clock and self.work_clock.work_lane != "media":
            raise ValueError("media work requires media lane")
        if self.state == "removed":
            return self
        if self.state == "completed" and self.stage != "associate":
            raise ValueError("extraction alone cannot complete association")
        if self.state == "needs_attention" and not (
            self.last_error and self.review_obligation_id and self.resend_intent_id
        ):
            raise ValueError("media failure requires timed review and resend intent")
        if self.stage != "fetch" and any(
            x is None
            for x in (
                self.source_blob_ref,
                self.byte_hash,
                self.mime,
                self.size,
            )
        ):
            raise ValueError("normalization requires durable source bytes")
        if self.stage in {"extract", "associate"} and self.normalized_blob_ref is None:
            raise ValueError("extraction requires normalized source")
        return self


class PatientRemoval(_Metadata):
    """Durable, bounded cleanup after the atomic contact fence."""

    entity_type: Literal["patient_removal"] = "patient_removal"
    scope: PatientScope
    phase: Literal["suppress", "cancel", "completed"] = "suppress"
    processed: NonnegativeInt = 0
    work_clock: OperationalClock | None

    @model_validator(mode="after")
    def recovery(self) -> Self:
        if self.id != self.scope.patient_id:
            raise ValueError("removal identity must be the patient")
        if (self.phase == "completed") != (self.work_clock is None):
            raise ValueError("unfinished removal requires a clock")
        if self.work_clock and self.work_clock.work_lane != "operational":
            raise ValueError("removal requires operational lane")
        return self


class DocumentPageManifest(_BoundaryValue):
    page_index: Annotated[int, Field(ge=1, le=10)]
    work_id: NonblankStr
    renderer_name: Literal["pypdfium2"] = "pypdfium2"
    renderer_version: NonblankStr
    byte_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    blob_ref: str | None = None


class DocumentPageWork(_Metadata):
    """One independently fenced page; terminal pages never lose their provenance."""

    entity_type: Literal["document_page_work"] = "document_page_work"
    scope: PatientScope | IntakeScope
    parent_id: NonblankStr
    receipt_id: NonblankStr
    source_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    manifest: DocumentPageManifest
    refresh_of: str | None = None
    stage: Literal["render", "read", "commit"] = "render"
    state: Literal["pending", "processing", "committed", "blank", "unreadable", "duplicate"] = (
        "pending"
    )
    processing_claim: ProcessingClaim | None = None
    work_clock: OperationalClock | None
    reads: DocumentRead | None = Field(default=None, repr=False)
    reads_ref: str | None = None
    duplicate_of: str | None = None
    last_error: str | None = None

    @model_validator(mode="after")
    def lifecycle(self) -> Self:
        if self.id != self.manifest.work_id or self.parent_id != keys.digest(self.receipt_id):
            raise ValueError("document_page_identity")
        terminal = self.state in {"committed", "blank", "unreadable", "duplicate"}
        if terminal != (self.work_clock is None) or terminal != (self.stage == "commit"):
            raise ValueError("document_page_clock")
        if self.work_clock and self.work_clock.work_lane != "media":
            raise ValueError("document_page_lane")
        if self.state == "committed" and not (self.reads or self.reads_ref):
            raise ValueError("document_page_read_missing")
        if self.state == "duplicate" and not self.duplicate_of:
            raise ValueError("document_page_duplicate_missing")
        if self.stage == "read" and not (self.manifest.byte_hash and self.manifest.blob_ref):
            raise ValueError("document_page_render_missing")
        return self


class BundleSchedule(_Metadata):
    entity_type: Literal["bundle_schedule"] = "bundle_schedule"
    scope: TenantScope
    doctor_id: NonblankStr
    generation: PositiveVersion = 1
    next_action_at: UtcInstant | None
    last_provider_accepted_at: UtcInstant | None = None
    pending_intent_id: NonblankStr | None = None
    work_clock: OperationalClock | None

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (
            type(self.scope) is not TenantScope
            or self.id != self.doctor_id
            or self.scope.doctor_id != self.doctor_id
        ):
            raise ValueError("bundle requires its doctor's tenant scope")
        if (self.next_action_at is None) != (self.work_clock is None):
            raise ValueError("bundle time and clock must agree")
        if self.work_clock and (
            self.work_clock.work_lane != "bundle"
            or self.work_clock.next_action_at != self.next_action_at
        ):
            raise ValueError("bundle clock mismatch")
        return self


class QuestionDigestSchedule(_Metadata):
    entity_type: Literal["question_digest_schedule"] = "question_digest_schedule"
    scope: TenantScope
    doctor_id: NonblankStr
    last_shown_ids: Annotated[tuple[str, ...], Field(max_length=20)] = ()
    pending_each: Annotated[tuple[tuple[str, str], ...], Field(max_length=20)] = ()
    generation: PositiveVersion = 1
    next_action_at: UtcInstant | None
    last_provider_accepted_at: UtcInstant | None = None
    pending_intent_id: NonblankStr | None = None
    work_clock: OperationalClock | None

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (
            type(self.scope) is not TenantScope
            or self.id != self.doctor_id
            or self.scope.doctor_id != self.doctor_id
        ):
            raise ValueError("question digest requires its doctor's tenant scope")
        if self.pending_intent_id and self.pending_each:
            raise ValueError("only one packing mode may have pending delivery")
        if (self.pending_intent_id or self.pending_each) and self.next_action_at is None:
            raise ValueError("pending delivery requires a durable clock")
        if (self.next_action_at is None) != (self.work_clock is None):
            raise ValueError("question digest time and clock must agree")
        if self.work_clock and (
            self.work_clock.work_lane != "question_digest"
            or self.work_clock.next_action_at != self.next_action_at
        ):
            raise ValueError("question digest clock mismatch")
        return self


class OutboundIntent(_Metadata):
    review_listing: tuple[ReviewSnapshot, ...] = ()
    review_listing_expires_at: UtcInstant | None = None
    record_listing_session_id: NonblankStr | None = Field(default=None, repr=False)
    record_listing_subject: NonblankStr | None = Field(default=None, repr=False)
    record_listing_auth_epoch: NonnegativeInt | None = None
    record_listing_patient_id: NonblankStr | None = None
    entity_type: Literal["outbound_intent"] = "outbound_intent"
    scope: Scope
    scope_kind: Literal["patient", "intake", "account", "doctor"]
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
    contact_kind: Literal["chase", "scheduled"] | None = None
    contact_feedback: Literal["not_applicable", "pending", "applied"] = "not_applicable"
    notice_feedback_pending: StrictBool = False
    expires_at: UtcInstant
    status: Literal["queued", "sending", "provider_accepted", "uncertain", "failed", "suppressed"]
    delivery_claim: ProcessingClaim | None = None
    delivery_lease_seconds: PositiveVersion
    delivered_text: str | None = Field(
        default=None, repr=False, exclude_if=lambda value: value is None
    )
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

    task_accept_token_hash: str | None = Field(default=None, repr=False)
    task_reopen_token_hash: str | None = Field(default=None, repr=False)
    task_action_expires_at: UtcInstant | None = None
    question_bindings: tuple[QuestionBinding, ...] = Field(default=(), repr=False)
    question_listing_token: str | None = Field(default=None, repr=False)
    question_listing_targets: tuple[tuple[str, str], ...] = Field(default=(), repr=False)
    question_listing_expires_at: UtcInstant | None = None

    @model_validator(mode="after")
    def shape(self) -> Self:
        if self.delivered_text is not None and (
            self.status != "provider_accepted" or self.audience != "patient"
        ):
            raise ValueError("delivered text requires patient delivery acceptance")
        if self.scope_kind == "doctor" and (
            type(self.scope) is not TenantScope
            or self.audience != "doctor"
            or self.notification_purpose != "DEADLINE"
            or self.eligibility_class != "bundle"
            or self.payload is not None
            or self.template_id not in {"doctor_weekly_bundle", "doctor_question_digest"}
            or self.recipient_auth_epoch_seen is None
            or self.bot_id is None
            or any(
                v is not None
                for v in (
                    self.binding_epoch_seen,
                    self.consent_version_seen,
                    self.safety_epoch_seen,
                    self.delivery_epoch_seen,
                    self.slot_id,
                )
            )
            or self.order_refs
        ):
            raise ValueError("doctor bundle requires tenant authority and no patient fields")
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
        terminal = (
            self.status == "suppressed"
            or (
                self.status == "provider_accepted"
                and self.contact_feedback != "pending"
                and not self.notice_feedback_pending
            )
            or (self.status in {"uncertain", "failed"} and self.review_obligation_id is not None)
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
    body: dict[str, JsonValue] = Field(repr=False)
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
    recent_patient_id: str | None = None
    media_snapshot: dict[str, Any] | None = Field(default=None, repr=False)

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
    "patient_removal": PatientRemoval,
    "document_page_work": DocumentPageWork,
    "sweep_position": SweepPosition,
    "correction": Correction,
    "correction_offer": CorrectionOffer,
    "fact_head": FactHead,
    "liaison_notice": Notice,
    "review_offer": ReviewOffer,
    "reuse_offer": ReuseOffer,
    "reusable_answer": ReusableAnswer,
    "upload_stage": UploadStage,
    "evidence": Evidence,
    "evidence_head": EvidenceHead,
    "evidence_hash": EvidenceHash,
    "evidence_action": EvidenceAction,
    "name_memory": NameMemory,
    "name_cache": NameCache,
    "bundle_schedule": BundleSchedule,
    "question_digest_schedule": QuestionDigestSchedule,
    "patient_action": PatientAction,
    "photo_association_work": PhotoAssociationWork,
    "intake_draft": IntakeDraft,
    "intake_callback": IntakeCallback,
    "intake_concern": IntakeConcern,
    "patient_media": PatientMedia,
    "scribe_invitation_work": InvitationWork,
    "scribe_proposal": Proposal,
    "scribe_callback": ScribeCallback,
    "scribe_state": ScribeState,
    "clinical_fact": ClinicalFact,
    "care_order_head": CareOrderHead,
    "care_order_version": CareOrderVersion,
    "care_plan": CarePlan,
    "patient": Patient,
    "consent": Consent,
    "patient_binding": PatientBinding,
    "token_head": TokenHead,
    "admin_account": AdminAccount,
    "admin_login": LoginExchange,
    "doctor_login": LoginExchange,
    "patient_login": LoginExchange,
    "invitation": Invitation,
    "patient_claim": PatientClaim,
    "claim_callback": ClaimCallback,
    "pre_session": PreSession,
    "web_session": AnyWebSession,
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
    "media_work": MediaWork,
    "outbound_intent": OutboundIntent,
    "delivery_attempt": DeliveryAttempt,
    "session_snapshot": SessionSnapshot,
    "doctor_authority": DoctorAuthority,
    "care_order": OrderAuthority,
    "evidence_annotation": EvidenceAnnotation,
    "incident": Incident,
}


def model_scope(model: BaseModel) -> Scope:
    if isinstance(model, (SweepPosition, PatientRemoval, DocumentPageWork)):
        return model.scope
    if isinstance(
        model,
        (
            Correction,
            CorrectionOffer,
            FactHead,
            Notice,
            ReviewOffer,
            ReuseOffer,
            ReusableAnswer,
            UploadStage,
        ),
    ):
        return model.scope
    if isinstance(model, (Evidence, EvidenceHead, EvidenceHash, EvidenceAction)):
        return model.scope
    if isinstance(model, (NameMemory, NameCache)):
        return model.scope
    if isinstance(model, (BundleSchedule, QuestionDigestSchedule)):
        return model.scope
    if isinstance(model, PatientAction):
        return model.scope
    if isinstance(model, InvitationWork):
        return model.scope
    if isinstance(
        model,
        (
            PhotoAssociationWork,
            IntakeDraft,
            IntakeCallback,
            IntakeConcern,
            PatientMedia,
            Proposal,
            ScribeCallback,
            ScribeState,
            ClinicalFact,
            CareOrderHead,
            CareOrderVersion,
            CarePlan,
        ),
    ):
        return model.scope
    if isinstance(
        model,
        (
            Patient,
            Consent,
            PatientBinding,
            AdminAccount,
            TokenHead,
            LoginExchange,
            Invitation,
            PatientClaim,
            ClaimCallback,
            PreSession,
            WebSession,
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
        if model.source_type == "outbound_intent" and model.review_kind == "delivery_failure":
            return TenantScope(doctor_id=model.owner_doctor_id)
        if model.source_type != "intake":
            raise ValueError("unassigned review requires intake or doctor delivery failure")
        return IntakeScope(doctor_id=model.owner_doctor_id, intake_id=model.source_id)
    if isinstance(
        model,
        (AuditEvent, InboundReceipt, MediaWork, OutboundIntent, DeliveryAttempt, SessionSnapshot),
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
    if isinstance(model, PatientRemoval):
        return Key(keys.partition(model.scope), f"PATIENT_REMOVAL#{keys.component(model.id)}")
    if isinstance(model, DocumentPageWork):
        return Key(keys.partition(model.scope), f"DOCUMENT_PAGE#{keys.component(model.id)}")
    if isinstance(model, SweepPosition):
        return keys.sweep_position(model.scope, model.lane, model.shard)
    if isinstance(
        model,
        (Correction, CorrectionOffer, FactHead, Notice, ReviewOffer, ReuseOffer, ReusableAnswer),
    ):
        return Key(
            keys.partition(model.scope), f"{model.entity_type.upper()}#{keys.component(model.id)}"
        )
    if isinstance(model, UploadStage):
        return Key(keys.partition(model.scope), f"UPLOAD#{keys.component(model.id)}")
    if isinstance(model, Evidence):
        return keys.evidence(model.scope, model.evidence_id, model.version)
    if isinstance(model, EvidenceHead):
        return keys.evidence_head(model.scope, model.evidence_id)
    if isinstance(model, (EvidenceHash, EvidenceAction)):
        return Key(
            keys.partition(model.scope), f"{model.entity_type.upper()}#{keys.component(model.id)}"
        )
    if isinstance(model, NameMemory):
        if type(model.scope) is TenantScope:
            return keys.doctor(model.scope, "NAME", model.id)
        return Key(keys.partition(model.scope), f"NAME#{keys.component(model.id)}")
    if isinstance(model, NameCache):
        return Key(keys.partition(model.scope), f"NAME_CACHE#{keys.component(model.id)}")
    if isinstance(model, QuestionDigestSchedule):
        return keys.doctor(model.scope, "QUESTION_DIGEST")
    if isinstance(model, BundleSchedule):
        return keys.doctor(model.scope, "BUNDLE")
    if isinstance(model, PatientAction):
        return Key(keys.partition(model.scope), f"PATIENT_ACTION#{keys.component(model.id)}")
    if isinstance(model, InvitationWork):
        return Key(
            keys.partition(model.scope), f"SCRIBE_INVITATION_WORK#{keys.component(model.id)}"
        )
    if isinstance(
        model,
        (
            PhotoAssociationWork,
            IntakeDraft,
            IntakeCallback,
            IntakeConcern,
            PatientMedia,
            Proposal,
            ScribeCallback,
            ScribeState,
            ClinicalFact,
            CareOrderHead,
            CareOrderVersion,
            CarePlan,
        ),
    ):
        return Key(
            keys.partition(model.scope), f"{model.entity_type.upper()}#{keys.component(model.id)}"
        )
    if isinstance(model, (AdminAccount, Patient, Consent, PatientBinding, TokenHead, PatientClaim)):
        return Key(keys.partition(model.scope), f"{model.entity_type.upper()}#{model.id}")
    if isinstance(model, (LoginExchange, Invitation, ClaimCallback, PreSession)):
        return keys.token(model.entity_type, model.id)
    if isinstance(model, WebSession):
        return keys.web_session(model.id)
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
    if isinstance(model, MediaWork):
        return Key(keys.partition(model.scope), f"MEDIA#{keys.component(model.id)}")
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
            "ttl": int(model.expires_at.timestamp()) if isinstance(model, NameCache) else None,
            **projections(model),
        }
    )


def from_record[T: BaseModel](record: StoredRecord, model_type: type[T]) -> T:
    session_decoder = record.entity_type == "web_session" and issubclass(model_type, WebSession)
    if not session_decoder and MODELS.get(record.entity_type) is not model_type:
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


class IdentityRead(_BoundaryValue):
    """Internal scoped read/absence assertion, committed with identity mutations."""

    scope: Scope
    entity_type: NonblankStr
    id: NonblankStr
    version: PositiveVersion | None


class CommitRequest(_BoundaryValue):
    command: CommandEnvelope
    identity_reads: tuple[IdentityRead, ...] = ()
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

    notice: CommitRequest | None = None

    intent: StoredRecord
    reviews: tuple[StoredRecord, ...] = ()
    release_reservation: StrictBool = False
    question_stamps: tuple[StoredRecord, ...] = ()
    question_schedule: StoredRecord | None = None
    question_schedule_expected_version: PositiveVersion | None = None
    obligation_stamp: StoredRecord | None = None
    obligation_expected_version: PositiveVersion | None = None
    bundle_schedule: StoredRecord | None = None
    bundle_expected_version: PositiveVersion | None = None


class Accepted(_BoundaryValue):
    status: Literal["accepted"] = "accepted"
    event_ids: tuple[str, ...]
    resulting_versions: tuple[VersionRef, ...]
    command_status: Literal[
        "accepted", "needs_confirmation", "invalid_input", "forbidden", "unsupported"
    ] = "accepted"
    reason_code: NonblankStr | None = None
    outcome_label: Literal["saved", "queued", "held", "suppressed"] | None = None
    outcome_reason: NonblankStr | None = None


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
