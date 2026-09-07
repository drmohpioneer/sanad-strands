"""Shared validated operations over backend-specific atomic compare-and-set primitives."""

import json
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from sanad.domain import (
    FollowUpTask,
    Mission,
    PatientScope,
    Principal,
    ReviewObligation,
    TenantScope,
    VersionRef,
)
from sanad.domain.deadlines import utc_instant
from sanad.domain.operations import transition_operational_clock
from sanad.store import keys
from sanad.store.keys import AccountScope, Key, Scope, ScopedKey
from sanad.store.records import (
    MODELS,
    PROJECTION_FIELDS,
    Accepted,
    Application,
    AuditEvent,
    Authorization,
    CallbackToken,
    Claim,
    CommandEnvelope,
    CommitRequest,
    CommitResult,
    Cursor,
    DeliveryAttempt,
    DeliveryOutcome,
    DeliveryResolution,
    Doctor,
    DoctorAuthority,
    DueItem,
    DuePage,
    Duplicate,
    Forbidden,
    IdentityConfig,
    InboundAccept,
    InboundReceipt,
    Incident,
    IntakeDraft,
    Lease,
    MarkerRecord,
    MediaWork,
    OperationalIssue,
    OutboundIntent,
    PatientProfile,
    ProcessingClaim,
    ReconcileReport,
    RecordPage,
    ReviewCreation,
    SessionSnapshot,
    StaleVersion,
    StoredRecord,
    SubjectBinding,
    TooLarge,
    WorkerCapability,
    canonical_json,
    from_record,
    item_record,
    model_scope,
    projections,
    record_item,
    scope_owns,
    to_record,
)

type Item = dict[str, Any]


@dataclass(frozen=True)
class Write:
    item: Item
    before: int | None

    @property
    def key(self) -> Key:
        return Key(self.item["PK"], self.item["SK"])


@dataclass(frozen=True)
class Check:
    key: Key
    version: int | None


def utc_now() -> datetime:
    return datetime.now(UTC)


def size_failure(writes: list[Write], checks: list[Check]) -> TooLarge | None:
    if len(writes) + len(checks) > 100:
        return TooLarge(reason="item_count")
    key_limits = {"PK": 2048, "SK": 1024}
    for hash_key, sort_key in INDEX_FIELDS.values():
        key_limits.update({hash_key: 2048, sort_key: 1024})
    if any(
        len(str(write.item[field]).encode("utf-8")) > limit
        for write in writes
        for field, limit in key_limits.items()
        if field in write.item
    ):
        return TooLarge(reason="item_bytes")
    # JSON wire size is a conservative upper bound on these string/number items.
    sizes = [len(canonical_json(write.item)) + 128 for write in writes]
    if any(size > 400 * 1024 for size in sizes):
        return TooLarge(reason="item_bytes")
    if sum(sizes) + 1024 * len(checks) > 4 * 1024 * 1024:
        return TooLarge(reason="transaction_bytes")
    return None


class StoreBase(ABC):
    def __init__(self, *, clock: Callable[[], datetime] = utc_now):
        self._clock = clock
        self._identity: IdentityConfig | None = None
        # Global index continuation keys can name another tenant. Keep them server-side;
        # a caller receives only a bounded, scope-bound opaque continuation handle.
        self._due_cursors: OrderedDict[str, tuple[str, Cursor]] = OrderedDict()

    @abstractmethod
    def _read(self, key: Key) -> Item | None: ...

    @abstractmethod
    def _atomic(self, writes: list[Write], checks: list[Check]) -> bool: ...

    @abstractmethod
    def _delete_nonce(self, key: Key, before: int) -> bool:
        """CAS deletion of an operational nonce only; never a domain record."""
        ...

    @abstractmethod
    def _update(self, item: Item, before: int) -> bool:
        """Version-checked UpdateItem (including claims/leases/projection repairs)."""
        ...

    @abstractmethod
    def _query(
        self,
        pk: str,
        *,
        index: str | None = None,
        prefix: str = "",
        through: str | None = None,
        cursor: Cursor | None = None,
        limit: int = 100,
    ) -> tuple[list[Item], Cursor | None]: ...

    def _owned(self, scope: Scope, key: Key) -> StoredRecord | None:
        raw = self._read(key)
        if raw is None or raw.get("doctor_id") != (
            None if isinstance(scope, AccountScope) else scope.doctor_id
        ):
            return None
        if isinstance(scope, PatientScope) and raw.get("patient_id") != scope.patient_id:
            return None
        if raw.get("entity_type") not in MODELS:
            return None
        record = item_record(raw)
        model = from_record(record, MODELS[record.entity_type])
        return record if scope_owns(scope, model_scope(model)) else None

    def get(self, scope: Scope, entity_type: str, id: str) -> StoredRecord | None:
        if isinstance(scope, keys.IntakeScope) and entity_type in {
            "intake_draft",
            "intake_callback",
            "scribe_proposal",
            "scribe_state",
            "scribe_callback",
        }:
            return self.get(TenantScope(doctor_id=scope.doctor_id), entity_type, id)
        if entity_type == "doctor" and not isinstance(scope, AccountScope):
            if id != scope.doctor_id:
                return None
            return self._owned(TenantScope(doctor_id=id), Key(keys.tenant_pk(scope), "DOCTOR"))
        if entity_type == "doctor_authority":
            if isinstance(scope, AccountScope):
                return None
            if id != scope.doctor_id:
                return None
            tenant = TenantScope(doctor_id=scope.doctor_id)
            return self._owned(tenant, keys.doctor(tenant))
        prefixes = {
            "photo_association_work": "PHOTO_ASSOCIATION_WORK",
            "intake_draft": "INTAKE_DRAFT",
            "intake_callback": "INTAKE_CALLBACK",
            "intake_concern": "INTAKE_CONCERN",
            "patient_media": "PATIENT_MEDIA",
            "patient_action": "PATIENT_ACTION",
            "scribe_invitation_work": "SCRIBE_INVITATION_WORK",
            "scribe_proposal": "SCRIBE_PROPOSAL",
            "scribe_callback": "SCRIBE_CALLBACK",
            "scribe_state": "SCRIBE_STATE",
            "clinical_fact": "CLINICAL_FACT",
            "care_order_head": "CARE_ORDER_HEAD",
            "care_order_version": "CARE_ORDER_VERSION",
            "care_plan": "CARE_PLAN",
            "patient": "PATIENT",
            "consent": "CONSENT",
            "patient_binding": "PATIENT_BINDING",
            "token_head": "TOKEN_HEAD",
            "patient_claim": "PATIENT_CLAIM",
            "application": "APPLICATION",
            "account_ack": "ACK",
            "mission": "MISSION",
            "followup": "FOLLOWUP",
            "review": "REVIEW",
            "outbound_intent": "OUT",
            "incident": "INCIDENT",
            "evidence_annotation": "FACT",
            "media_work": "MEDIA",
        }
        if entity_type in {"mission", "followup", "patient_profile"} and not isinstance(
            scope, PatientScope
        ):
            return None
        if entity_type == "patient_profile":
            assert isinstance(scope, PatientScope)
            if id != scope.patient_id:
                return None
            key = keys.patient(scope)
        elif entity_type == "care_order" and isinstance(scope, PatientScope):
            key = keys.patient(scope, "ORDER", id)
        elif entity_type in prefixes:
            key = Key(keys.partition(scope), f"{prefixes[entity_type]}#{keys.component(id)}")
        elif entity_type == "subject_binding" and isinstance(scope, AccountScope):
            prefix = keys.subject(scope.bot_id, "0").pk[:-1]
            if id.startswith("SUBJECT#") and not id.startswith(prefix):
                return None
            key = keys.subject(scope.bot_id, id.removeprefix(prefix))
        elif entity_type in {
            "doctor_login",
            "patient_login",
            "invitation",
            "claim_callback",
            "pre_session",
        } and isinstance(scope, AccountScope):
            key = keys.token(entity_type, id)
        elif entity_type == "web_session" and isinstance(scope, AccountScope):
            key = keys.web_session(id)
        elif entity_type == "callback_token" and isinstance(scope, AccountScope):
            key = keys.token("callback", id)
        elif entity_type == "operational_issue" and isinstance(scope, AccountScope):
            key = keys.operational(scope.bot_id, "ISSUE", id)
        elif entity_type == "inbound_receipt":
            if not id.startswith("IN#"):
                return None
            key = Key(id, "META")
        elif entity_type in {"audit_event", "delivery_attempt", "session_snapshot"}:
            prefix = {
                "audit_event": "EVENT#",
                "delivery_attempt": "ATTEMPT#",
                "session_snapshot": "SESSION#",
            }[entity_type]
            cursor = None
            found = None
            while True:
                items, cursor = self._query(keys.partition(scope), prefix=prefix, cursor=cursor)
                for item in items:
                    if item.get("id") == id and item.get("entity_type") == entity_type:
                        record = self._owned(scope, Key(item["PK"], item["SK"]))
                        if record is not None:
                            if found is not None:
                                return None  # An ambiguous bare ID must never select a record.
                            found = record
                if cursor is None:
                    return found
        else:
            return None
        record = self._owned(scope, key)
        return record if record and record.entity_type == entity_type else None

    def get_mission(self, scope: PatientScope, id: str) -> Mission | None:
        record = self.get(scope, "mission", id)
        return from_record(record, Mission) if record else None

    def get_followup(self, scope: PatientScope, id: str) -> FollowUpTask | None:
        record = self.get(scope, "followup", id)
        return from_record(record, FollowUpTask) if record else None

    def get_review(self, scope: Scope, id: str) -> ReviewObligation | None:
        record = self.get(scope, "review", id)
        return from_record(record, ReviewObligation) if record else None

    def get_patient_profile(self, scope: PatientScope) -> PatientProfile | None:
        record = self.get(scope, "patient_profile", scope.patient_id)
        return from_record(record, PatientProfile) if record else None

    def _list(
        self,
        scope: Scope,
        pk: str,
        cursor: Cursor | None,
        limit: int,
        *,
        index: str | None = None,
        prefix: str = "",
    ) -> RecordPage:
        items, next_cursor = self._query(pk, index=index, prefix=prefix, cursor=cursor, limit=limit)
        records = []
        for item in items:
            record = self._owned(scope, Key(item["PK"], item["SK"]))
            if record is None:
                continue
            if index == "GSI_REVIEW" and (
                record.entity_type != "review" or record.body["state"] == "resolved"
            ):
                continue
            if index == "GSI_DOCTOR_PATIENTS" and record.entity_type != "patient_profile":
                continue
            records.append(record)
        return tuple(records), next_cursor

    def list_patients(
        self, tenant_scope: TenantScope, cursor: Cursor | None = None, limit: int = 100
    ) -> RecordPage:
        if type(tenant_scope) is not TenantScope:
            return (), None
        return self._list(
            tenant_scope, keys.tenant_pk(tenant_scope), cursor, limit, index="GSI_DOCTOR_PATIENTS"
        )

    def list_reviews(
        self, tenant_scope: TenantScope, cursor: Cursor | None = None, limit: int = 100
    ) -> RecordPage:
        if type(tenant_scope) is not TenantScope:
            return (), None
        return self._list(
            tenant_scope, keys.tenant_pk(tenant_scope), cursor, limit, index="GSI_REVIEW"
        )

    def list_events(
        self, patient_scope: PatientScope, cursor: Cursor | None = None, limit: int = 100
    ) -> RecordPage:
        if not isinstance(patient_scope, PatientScope):
            return (), None
        return self._list(
            patient_scope, keys.partition(patient_scope), cursor, limit, prefix="EVENT#"
        )

    def _lease_record(self, lease: Lease, now: datetime) -> StoredRecord | None:
        record = self.get(lease.scope, "patient_profile", lease.scope.patient_id)
        if record is None:
            return None
        profile = from_record(record, PatientProfile)
        if (
            profile.lease_owner != lease.owner
            or profile.lease_generation != lease.generation
            or profile.lease_expires_at != lease.expires_at
            or lease.expires_at <= now
        ):
            return None
        return record

    def list_records(
        self, scope: Scope, entity_type: str, cursor: Cursor | None = None, limit: int = 100
    ) -> RecordPage:
        prefixes = {
            "photo_association_work": "PHOTO_ASSOCIATION_WORK#",
            "intake_draft": "INTAKE_DRAFT#",
            "intake_callback": "INTAKE_CALLBACK#",
            "intake_concern": "INTAKE_CONCERN#",
            "patient_media": "PATIENT_MEDIA#",
            "scribe_invitation_work": "SCRIBE_INVITATION_WORK#",
            "scribe_proposal": "SCRIBE_PROPOSAL#",
            "scribe_callback": "SCRIBE_CALLBACK#",
            "scribe_state": "SCRIBE_STATE#",
            "clinical_fact": "CLINICAL_FACT#",
            "patient_action": "PATIENT_ACTION#",
            "care_order_head": "CARE_ORDER_HEAD#",
            "care_order_version": "CARE_ORDER_VERSION#",
            "care_plan": "CARE_PLAN#",
            "patient": "PATIENT#",
            "consent": "CONSENT#",
            "patient_binding": "PATIENT_BINDING#",
            "token_head": "TOKEN_HEAD#",
            "patient_claim": "PATIENT_CLAIM#",
            "application": "APPLICATION#",
            "account_ack": "ACK#",
            "doctor": "DOCTOR",
            "audit_event": "EVENT#",
            "delivery_attempt": "ATTEMPT#",
            "operational_issue": "ISSUE#",
            "mission": "MISSION#",
            "followup": "FOLLOWUP#",
            "review": "REVIEW#",
            "incident": "INCIDENT#",
            "outbound_intent": "OUT#",
            "evidence_annotation": "FACT#",
            "care_order": "ORDER#",
            "media_work": "MEDIA#",
        }
        if entity_type not in prefixes:
            return (), None
        rows, cursor = self._list(
            scope,
            (
                keys.operational(scope.bot_id, "ISSUE", "lookup").pk
                if entity_type == "operational_issue" and isinstance(scope, AccountScope)
                else keys.partition(scope)
            ),
            cursor,
            limit,
            prefix=prefixes[entity_type],
        )
        return tuple(r for r in rows if r.entity_type == entity_type), cursor

    def lookup_command(self, command: CommandEnvelope) -> CommitResult | None:
        if isinstance(command.scope, AccountScope):
            if command.principal.bot_id != command.scope.bot_id:
                return Forbidden()
        elif command.principal.doctor_id != command.scope.doctor_id:
            return Forbidden()
        if (
            command.principal.actor_kind == "patient"
            and not isinstance(command.scope, AccountScope)
            and (
                not isinstance(command.scope, PatientScope)
                or command.principal.patient_id != command.scope.patient_id
            )
        ):
            return Forbidden()
        return self._duplicate(
            keys.uniqueness(command.scope, "CMD", command.command_id),
            keys.digest(canonical_json(command.payload).decode()),
            command.scope,
        )

    def commit_incident(self, request: CommitRequest) -> CommitResult:
        """No lease dependency; ordinary record updates cannot use this escape hatch."""
        scope, worker = request.command.scope, request.command.worker
        if (
            not isinstance(scope, PatientScope)
            or worker is None
            or "urgent" not in worker.permitted_lanes
            or request.receipt_completion
        ):
            return Forbidden()
        incidents = [r for r in request.puts if r.entity_type == "incident"]
        profiles = [r for r in request.puts if r.entity_type == "patient_profile"]
        if len(incidents) != 1 or len(profiles) != 1:
            return Forbidden()
        if any(
            r.entity_type not in {"incident", "patient_profile", "review"} for r in request.puts
        ):
            return Forbidden()
        if any(
            r.body.get("notification_purpose") not in {"DANGER", "patient_safety_response"}
            for r in request.intents
        ):
            return Forbidden()
        prior = self.lookup_command(request.command)
        if prior is not None:
            return prior
        current = self.get_patient_profile(scope)
        if current is None:
            return Forbidden()
        proposed = from_record(profiles[0], PatientProfile)
        allowed = {"version", "updated_at", "safety_epoch"}
        if proposed.safety_epoch != current.safety_epoch + 1 or proposed.model_dump(
            exclude=allowed
        ) != current.model_dump(exclude=allowed):
            return StaleVersion(conflicts=("safety_epoch",))
        incident = from_record(incidents[0], Incident)
        reviews = [
            from_record(r, ReviewObligation) for r in request.puts if r.entity_type == "review"
        ]
        intents = [from_record(r, OutboundIntent) for r in request.intents]
        required_purposes = (
            {"DANGER", "patient_safety_response"}
            if current.recipient_ref is not None
            else {"DANGER"}
        )
        if (
            incident.version != 1
            or len(reviews) != 1
            or len(intents) != len(required_purposes)
            or reviews[0].id != incident.review_obligation_id
            or reviews[0].review_kind != "incident_response"
            or reviews[0].source_type != "incident"
            or reviews[0].source_id != incident.id
            or set(incident.alert_intent_ids) != {i.id for i in intents}
            or {i.notification_purpose for i in intents} != required_purposes
            or any(i.source_event_ids != (incident.id,) for i in intents)
        ):
            return Forbidden()
        return self._commit(request, urgent=True)

    def _duplicate(self, key: Key, digest: str, scope: Scope) -> CommitResult | None:
        existing = self._read(key)
        if existing is None:
            return None
        if existing.get("scope") != scope.model_dump(mode="json"):
            return Forbidden()
        if existing.get("payload_digest") != digest:
            return StaleVersion(conflicts=("command_payload",))
        return Duplicate(original=Accepted.model_validate(existing["accepted_result"]))

    def commit(self, request: CommitRequest) -> CommitResult:
        from sanad.store.scribe import COMMANDS

        kind = request.command.payload.get("type")
        return self._commit(request, scribe=isinstance(kind, str) and kind in COMMANDS)

    def _commit(
        self,
        request: CommitRequest,
        *,
        urgent: bool = False,
        account: bool = False,
        identity: bool = False,
        scribe: bool = False,
    ) -> CommitResult:
        command = request.command
        concierge = command.payload.get("executor") == "concierge-v1"
        scope = command.scope
        actor = command.principal
        if isinstance(scope, AccountScope) and not account:
            return Forbidden()
        doctor_id = None if isinstance(scope, AccountScope) else scope.doctor_id
        worker = command.worker
        system = (
            actor.actor_kind == "system"
            and actor.doctor_id == doctor_id
            and worker is not None
            and worker.service_subject == actor.subject
            and worker.resolved_scope == scope
            and worker.auth_expiry > utc_instant(self._clock())
            and bool(
                worker.permitted_lanes
                & {"mission", "followup", "review", "ingress", "media", "urgent", "scribe"}
            )
        )
        if (
            not account
            and not system
            and (
                actor.doctor_id != doctor_id
                or actor.actor_kind not in {"doctor", "patient"}
                or actor.actor_kind not in actor.verified_roles
            )
        ):
            return Forbidden()
        if (
            not account
            and actor.actor_kind == "patient"
            and (not isinstance(scope, PatientScope) or actor.patient_id != scope.patient_id)
        ):
            return Forbidden()
        # Missing authoritative facts remain a denial, preserving slice 02's safe default.
        epoch_fields = (
            "expected_auth_epoch",
            "expected_binding_epoch",
            "expected_consent_version",
            "expected_delivery_epoch",
            "expected_safety_epoch",
        )
        authority_checks: list[Check] = []
        identity_expiry = None
        if request.identity_reads and not (identity or scribe):
            return Forbidden()
        if scribe:
            from sanad.store.scribe import scribe_guards

            scribe_guarded = scribe_guards(self, request, utc_instant(self._clock()))
            if scribe_guarded is None:
                return Forbidden()
            authority_checks, identity_expiry = scribe_guarded
        if identity:
            from sanad.store.identity import identity_guards

            guarded = identity_guards(self, request, utc_instant(self._clock()))
            if guarded is None:
                return Forbidden()
            authority_checks, identity_expiry = guarded

        if any(getattr(command, field) is not None for field in epoch_fields):
            if not isinstance(scope, PatientScope):
                return Forbidden()
            profile_record = self.get(scope, "patient_profile", scope.patient_id)
            doctor_record = self.get(scope, "doctor_authority", scope.doctor_id)
            if profile_record is None or doctor_record is None:
                return Forbidden()
            profile_fact = from_record(profile_record, PatientProfile)
            doctor_fact = from_record(doctor_record, DoctorAuthority)
            if command.expected_auth_epoch is not None and not doctor_fact.approved:
                return Forbidden()
            actual_epochs = (
                doctor_fact.auth_epoch,
                profile_fact.binding_epoch,
                profile_fact.consent_version,
                profile_fact.delivery_epoch,
                profile_fact.safety_epoch,
            )
            if any(
                getattr(command, field) is not None and getattr(command, field) != actual
                for field, actual in zip(epoch_fields, actual_epochs, strict=True)
            ):
                return StaleVersion(conflicts=("authority_epoch",))
            authority_checks = [
                Check(profile_record.key, profile_record.version),
                Check(doctor_record.key, doctor_record.version),
            ]
        if concierge:
            from sanad.store.concierge import guards as concierge_guards

            guarded_patient = concierge_guards(self, request, utc_instant(self._clock()))
            if guarded_patient is None:
                return Forbidden()
            authority_checks.extend(guarded_patient)
        command_key = keys.uniqueness(scope, "CMD", command.command_id)
        # Replay identity is the immutable command payload, independent of retry time.
        digest = keys.digest(canonical_json(command.payload).decode())
        records = [*request.puts, *request.events, *request.intents]
        # Reject obviously oversized requests before even a base read/duplicate lookup.
        provisional = [Write(record_item(record), record.version - 1 or None) for record in records]
        provisional.append(
            Write(
                {
                    "PK": command_key.pk,
                    "SK": command_key.sk,
                    "payload_digest": digest,
                    "accepted_result": {
                        "event_ids": [r.id for r in records if r.entity_type == "audit_event"],
                        "resulting_versions": [r.ref.model_dump() for r in records],
                    },
                    "scope": scope.model_dump(mode="json"),
                },
                None,
            )
        )
        for record in records:
            if (
                not scribe
                and not (concierge and record.entity_type == "clinical_fact")
                and record.entity_type
                in {
                    "photo_association_work",
                    "intake_draft",
                    "intake_callback",
                    "intake_concern",
                    "patient_media",
                    "scribe_proposal",
                    "scribe_state",
                    "scribe_callback",
                    "scribe_invitation_work",
                    "clinical_fact",
                    "care_order_head",
                    "care_order_version",
                    "care_plan",
                }
            ):
                return Forbidden()
            unique = (
                record.body.get("logical_key")
                if record.entity_type == "outbound_intent"
                else (
                    record.body.get("unique_source_key") if record.entity_type == "review" else None
                )
            )
            if record.version == 1 and isinstance(unique, str):
                provisional_key = Key(
                    record.pk,
                    ("OUTKEY#" if record.entity_type == "outbound_intent" else "REVIEWKEY#")
                    + keys.digest(unique),
                )
                provisional.append(
                    Write(
                        self._marker_item(provisional_key, scope, record.key, command.requested_at),
                        None,
                    )
                )
        for marker in request.markers:
            if marker.key not in {write.key for write in provisional}:
                provisional.append(
                    Write(
                        self._marker_item(
                            marker.key,
                            marker.scope,
                            marker.target.key,
                            marker.created_at,
                            marker.ttl,
                        ),
                        None,
                    )
                )
        extra_checks = []
        if isinstance(scope, PatientScope) and keys.patient(scope) not in {
            w.key for w in provisional
        }:
            extra_checks.append(Check(keys.patient(scope), None))
        written_refs = {(record.entity_type, record.id) for record in records}
        extra_checks.extend(
            Check(Key("preflight", f"{ref.entity_type}#{ref.id}"), ref.version)
            for ref in set(command.expected_versions)
            if (ref.entity_type, ref.id) not in written_refs
        )
        if request.receipt_completion is not None:
            extra_checks.append(Check(request.receipt_completion.claim.record_key.key, None))
        early_large = size_failure(provisional, extra_checks)
        if early_large:
            return early_large
        prior = self._duplicate(command_key, digest, scope)
        if prior is not None:
            return prior
        if any(r.entity_type != "audit_event" for r in request.events) or any(
            r.entity_type != "outbound_intent" for r in request.intents
        ):
            return Forbidden()
        if len({(r.entity_type, r.id) for r in records}) != len(records):
            return StaleVersion(conflicts=("repeated_record",))
        expected = {(r.entity_type, r.id): r.version for r in request.expected}
        if len(expected) != len(request.expected) or expected != {
            (r.entity_type, r.id): r.version for r in records
        }:
            return StaleVersion(conflicts=("expected_versions",))
        if len({marker.key for marker in request.markers}) != len(request.markers):
            return StaleVersion(conflicts=("repeated_marker",))
        writes: list[Write] = []
        checks: list[Check] = authority_checks.copy()
        now = utc_instant(self._clock())
        if isinstance(scope, PatientScope):
            patient = self.get(scope, "patient_profile", scope.patient_id)
            if patient is not None:
                profile = from_record(patient, PatientProfile)
                if profile.lease_generation > 0 and command.fence is None and not urgent:
                    return StaleVersion(conflicts=("patient_fence",))
            checks.append(Check(keys.patient(scope), patient.version if patient else None))
        if command.fence is not None:
            if command.fence.scope != scope and not scribe:
                return Forbidden()
            fenced = self._lease_record(command.fence, now)
            if fenced is None:
                return StaleVersion(conflicts=("patient_fence",))
            checks.append(Check(fenced.key, fenced.version))
        if command.work_claim is not None:
            work_claim = command.work_claim
            if work_claim.record_key.scope != scope and not (account or scribe):
                return Forbidden()
            claimed = self._owned(work_claim.record_key.scope, work_claim.record_key.key)
            token = ProcessingClaim.model_validate(
                work_claim.model_dump(exclude={"record_key", "version"})
            )
            completing = (
                request.receipt_completion is not None
                and request.receipt_completion.claim == work_claim
            )
            checkpointing = claimed is not None and claimed.entity_type in {
                "inbound_receipt",
                "media_work",
            }
            stored_token = (
                (
                    from_record(claimed, InboundReceipt).processing_claim
                    if claimed.entity_type == "inbound_receipt"
                    else from_record(claimed, MediaWork).processing_claim
                )
                if checkpointing and claimed is not None
                else claimed.processing_claim
                if claimed
                else None
            )
            if (
                claimed is None
                or claimed.version != work_claim.version
                or stored_token != token
                or work_claim.expires_at <= now
                or (not completing and claimed.key not in {record.key for record in records})
            ):
                return StaleVersion(conflicts=("work_claim",))
        for record in records:
            if not (identity or scribe or concierge) and record.entity_type in {
                "patient_action",
                "patient",
                "consent",
                "patient_binding",
                "token_head",
                "doctor_login",
                "patient_login",
                "invitation",
                "patient_claim",
                "claim_callback",
                "pre_session",
                "web_session",
            }:
                return Forbidden()
            if not account and record.entity_type in {
                "application",
                "doctor",
                "subject_binding",
                "callback_token",
                "account_ack",
                "operational_issue",
            }:
                return Forbidden()
            try:
                model = from_record(record, MODELS[record.entity_type])
            except (KeyError, ValueError, ValidationError):
                return Forbidden()
            actual_scope = model_scope(model)
            if scope != actual_scope:
                if scribe:
                    pass  # The scribe guard checked every target, owner and patient fence.
                elif not account or not isinstance(scope, AccountScope):
                    return Forbidden()
                elif identity:
                    pass  # Narrow identity guards checked every cross-partition row.
                elif isinstance(model, Doctor):
                    if model.telegram_bot_id != scope.bot_id:
                        return Forbidden()
                elif isinstance(model, InboundReceipt):
                    if (
                        isinstance(actual_scope, PatientScope)
                        or command.work_claim is None
                        or command.work_claim.record_key.scope != actual_scope
                    ):
                        return Forbidden()
                elif isinstance(model, DoctorAuthority):
                    doctor_rows = [
                        r for r in records if r.entity_type == "doctor" and r.id == model.id
                    ]
                    if (
                        len(doctor_rows) != 1
                        or doctor_rows[0].body.get("telegram_bot_id") != scope.bot_id
                    ):
                        return Forbidden()
                else:
                    return Forbidden()
            if isinstance(model, AuditEvent) and (
                model.command_id != command.command_id
                or model.actor != actor
                or (model.scope != scope and not scribe)
            ):
                return Forbidden()
            if record.entity_type in {"delivery_attempt", "session_snapshot"}:
                return Forbidden()  # Their fenced operations own these writes.
            current = self._owned(actual_scope, record.key)
            if isinstance(model, MediaWork):
                if current is None:
                    source = self.get(actual_scope, "inbound_receipt", model.receipt_id)
                    if source is None and isinstance(actual_scope, keys.IntakeScope):
                        candidate_source = self.get(
                            TenantScope(doctor_id=actual_scope.doctor_id),
                            "inbound_receipt",
                            model.receipt_id,
                        )
                        if (
                            candidate_source
                            and candidate_source.body.get("source_subject") == actor.subject
                        ):
                            source = candidate_source
                    if source is None or (
                        from_record(source, InboundReceipt).provider_media_handle
                        != model.provider_handle_ref
                        or model.stage != "fetch"
                        or model.state != "pending"
                    ):
                        return Forbidden()
                    checks.append(Check(source.key, source.version))
                else:
                    old_media = from_record(current, MediaWork)
                    if (
                        command.work_claim is None
                        or command.work_claim.record_key.key != record.key
                    ):
                        return Forbidden()
                    if (old_media.scope, old_media.receipt_id, old_media.provider_handle_ref) != (
                        model.scope,
                        model.receipt_id,
                        model.provider_handle_ref,
                    ):
                        return Forbidden()
                    stages = ("fetch", "normalize", "extract", "associate")
                    delta = stages.index(model.stage) - stages.index(old_media.stage)
                    if old_media.state == "completed" or delta not in {0, 1}:
                        return Forbidden()
                    if old_media.source_blob_ref is not None and any(
                        getattr(old_media, f) != getattr(model, f)
                        for f in ("source_blob_ref", "byte_hash", "mime", "size")
                    ):
                        return Forbidden()
            if isinstance(model, InboundReceipt):
                if (
                    current is None
                    or command.work_claim is None
                    or command.work_claim.record_key.key != record.key
                ):
                    return Forbidden()
                old_receipt = from_record(current, InboundReceipt)
                mutable = {
                    "version",
                    "updated_at",
                    "state",
                    "work_clock",
                    "processing_claim",
                    "result_event_ids",
                    "review_obligation_id",
                }
                if model.model_dump(exclude=mutable) != old_receipt.model_dump(exclude=mutable):
                    return Forbidden()
            if current is not None:
                if (
                    current.created_at != record.created_at
                    or record.updated_at < current.updated_at
                ):
                    return StaleVersion(conflicts=("record_metadata",))
                if current.processing_claim is not None and (
                    command.work_claim is None or command.work_claim.record_key.key != current.key
                ):
                    return StaleVersion(conflicts=("claimed_record",))
                if isinstance(model, PatientProfile):
                    old = from_record(current, PatientProfile)
                    if (model.lease_owner, model.lease_expires_at, model.lease_generation) != (
                        old.lease_owner,
                        old.lease_expires_at,
                        old.lease_generation,
                    ):
                        return Forbidden()
                if isinstance(model, OutboundIntent):
                    old_intent = from_record(current, OutboundIntent)
                    if model.logical_key != old_intent.logical_key or old_intent.status != "queued":
                        return Forbidden()
            elif isinstance(model, PatientProfile) and model.lease_generation != 0:
                return Forbidden()
            if isinstance(model, OutboundIntent) and model.status not in {"queued", "suppressed"}:
                return Forbidden()
            canonical = to_record(model, actual_scope)
            canonical = StoredRecord.model_validate(
                canonical.model_dump()
                | {
                    "ttl": record.ttl,
                    "claim_generation": current.claim_generation if current else 0,
                }
            )
            writes.append(Write(record_item(canonical), record.version - 1 or None))
            if record.version == 1 and isinstance(model, (OutboundIntent, ReviewObligation)):
                kind: Literal["OUTKEY", "REVIEWKEY"] = (
                    "OUTKEY" if isinstance(model, OutboundIntent) else "REVIEWKEY"
                )
                unique = (
                    model.logical_key
                    if isinstance(model, OutboundIntent)
                    else model.unique_source_key
                )
                marker_key = keys.uniqueness(model_scope(model), kind, keys.digest(unique))
                writes.append(
                    Write(self._marker_item(marker_key, model_scope(model), record.key, now), None)
                )
        for ref in command.expected_versions:
            current = (
                self.get_account_source(scope, ref)
                if isinstance(scope, AccountScope)
                else self.get(scope, ref.entity_type, ref.id)
            )
            if current is None or current.version != ref.version:
                return StaleVersion(conflicts=("source_version",))
            checks.append(Check(current.key, ref.version))
        for marker in request.markers:
            if not self._valid_marker(marker, scope, records):
                return Forbidden()
            # An explicitly supplied automatic marker is the same transaction item.
            writes = [write for write in writes if write.key != marker.key]
            writes.append(
                Write(
                    self._marker_item(
                        marker.key, marker.scope, marker.target.key, marker.created_at, marker.ttl
                    ),
                    None,
                )
            )
        if request.receipt_completion is not None:
            completion = request.receipt_completion
            claim = completion.claim
            if (claim.record_key.scope != scope and not (account or scribe)) or (
                account and not identity and isinstance(claim.record_key.scope, PatientScope)
            ):
                return Forbidden()
            receipt = self._owned(claim.record_key.scope, claim.record_key.key)
            if (
                receipt is None
                or receipt.entity_type != "inbound_receipt"
                or receipt.version != claim.version
                or claim.expires_at <= now
            ):
                return StaleVersion(conflicts=("receipt_claim",))
            inbound = from_record(receipt, InboundReceipt)
            token = ProcessingClaim.model_validate(
                claim.model_dump(exclude={"record_key", "version"})
            )
            if inbound.state != "processing" or inbound.processing_claim != token:
                return StaleVersion(conflicts=("receipt_claim",))
            if completion.result_event_ids != tuple(
                r.id for r in records if r.entity_type == "audit_event"
            ):
                return StaleVersion(conflicts=("receipt_result_events",))
            completed = self._revision(
                receipt,
                now,
                state="completed",
                work_clock=None,
                processing_claim=None,
                result_event_ids=completion.result_event_ids,
            )
            records.append(completed)
            writes.append(Write(record_item(completed), receipt.version))
        result = Accepted(
            event_ids=tuple(r.id for r in records if r.entity_type == "audit_event"),
            resulting_versions=tuple(r.ref for r in records),
            command_status=request.command_status,
            reason_code=request.reason_code,
        )
        writes.append(
            Write(
                {
                    "PK": command_key.pk,
                    "SK": command_key.sk,
                    "version": 1,
                    "scope": scope.model_dump(mode="json"),
                    "payload_digest": digest,
                    "accepted_result": result.model_dump(mode="json"),
                    "accepted_at": keys.instant(now),
                },
                None,
            )
        )
        merged = self._merge_checks(writes, checks)
        if merged is None:
            return StaleVersion(conflicts=("repeated_key_or_version",))
        large = size_failure(writes, merged)
        if large:
            return large
        if identity_expiry is not None and utc_instant(self._clock()) >= identity_expiry:
            return Forbidden()
        if self._atomic(writes, merged):
            return result
        return self._duplicate(command_key, digest, scope) or StaleVersion(
            conflicts=("conditional_write",)
        )

    @staticmethod
    def _merge_checks(writes: list[Write], checks: list[Check]) -> list[Check] | None:
        written = {w.key: w.before for w in writes}
        if len(written) != len(writes):
            return None
        result: dict[Key, Check] = {}
        for check in checks:
            if check.key in written:
                if written[check.key] != check.version:
                    return None
            elif check.key in result and result[check.key].version != check.version:
                return None
            else:
                result[check.key] = check
        return list(result.values())

    @staticmethod
    def _marker_item(
        key: Key, scope: Scope, target: Key, at: datetime, ttl: int | None = None
    ) -> Item:
        item: Item = {
            "PK": key.pk,
            "SK": key.sk,
            "version": 1,
            "scope": scope.model_dump(mode="json"),
            "target_pk": target.pk,
            "target_sk": target.sk,
            "created_at": keys.instant(at),
        }
        if ttl is not None:
            item["ttl"] = ttl
        return item

    @staticmethod
    def _valid_marker(marker: MarkerRecord, scope: Scope, records: list[StoredRecord]) -> bool:
        if not scope_owns(scope, marker.scope) or marker.target.scope != marker.scope:
            return False
        if marker.target.key not in {r.key for r in records}:
            return False
        if marker.pk == keys.partition(marker.scope):
            target = next(r for r in records if r.key == marker.target.key)
            if target.entity_type == "outbound_intent":
                value = target.body.get("logical_key")
                return isinstance(value, str) and marker.key == keys.uniqueness(
                    marker.scope, "OUTKEY", keys.digest(value)
                )
            if target.entity_type == "review":
                value = target.body.get("unique_source_key")
                return isinstance(value, str) and marker.key == keys.uniqueness(
                    marker.scope, "REVIEWKEY", keys.digest(value)
                )
            return False
        # Global rows contain scope references only; account activation is deferred.
        parts = marker.pk.split("#")
        try:
            if len(parts) == 3 and parts[0] == "SUBJECT":
                return marker.key == keys.subject(parts[1], parts[2])
            if len(parts) == 3 and parts[0] == "TOKEN":
                return marker.key == keys.token(parts[1], parts[2])
        except ValueError:
            pass
        return False

    def accept_inbound(self, transport_key: str, receipt: StoredRecord) -> InboundAccept:
        try:
            model = from_record(receipt, InboundReceipt)
        except ValueError:
            return InboundAccept(status="forbidden")
        if model.transport_key != transport_key or model.version != 1 or model.state != "pending":
            return InboundAccept(status="conflict")
        canonical = to_record(model, model.scope)
        if size_failure([Write(record_item(canonical), None)], []):
            return InboundAccept(status="conflict")
        if self._atomic([Write(record_item(canonical), None)], []):
            return InboundAccept(status="created", record=canonical, state="pending")
        existing = self._owned(model.scope, receipt.key)
        if (
            existing is None
            and model.transport == "telegram"
            and model.principal is not None
            and model.principal.bot_id is not None
        ):
            # A completed application can change scope between Telegram retries.
            raw = self._read(receipt.key)
            if raw is not None:
                saved = item_record(raw)
                prior = from_record(saved, InboundReceipt)
                if (
                    prior.transport_key == model.transport_key
                    and prior.source_subject == model.source_subject
                    and prior.source_chat == model.source_chat
                    and prior.principal is not None
                    and prior.principal.bot_id == model.principal.bot_id
                    and (
                        isinstance(prior.scope, AccountScope)
                        or isinstance(model.scope, AccountScope)
                    )
                ):
                    existing = saved
        if existing is None:
            return InboundAccept(status="forbidden")
        return InboundAccept(status="existing", record=existing, state=str(existing.body["state"]))

    @staticmethod
    def _revision(record: StoredRecord, now: datetime, **changes: object) -> StoredRecord:
        data = (
            record.body | {"version": record.version + 1, "updated_at": utc_instant(now)} | changes
        )
        model = MODELS[record.entity_type].model_validate(data)
        revised = to_record(model, model_scope(model))
        return StoredRecord.model_validate(
            revised.model_dump()
            | {
                "ttl": record.ttl,
                "processing_claim": record.processing_claim,
                "claim_generation": record.claim_generation,
            }
        )

    def claim_work(
        self,
        record_key: ScopedKey,
        expected_version: int,
        owner: str,
        now: datetime,
        ttl: timedelta,
        *,
        count_attempt: bool = True,
        start_extraction: bool = False,
    ) -> Claim | None:
        now = utc_instant(now)
        record = self._owned(record_key.scope, record_key.key)
        if (
            record is None
            or type(expected_version) is not int
            or expected_version < 1
            or record.version != expected_version
            or ttl <= timedelta()
            or not owner.strip()
            or not record.body.get("work_clock")
        ):
            return None
        model = from_record(record, MODELS[record.entity_type])
        previous = (
            model.processing_claim
            if isinstance(model, (InboundReceipt, MediaWork))
            else record.processing_claim
        )
        if previous is not None and previous.expires_at > now:
            return None
        token = ProcessingClaim(
            owner=owner,
            generation=max(record.claim_generation, previous.generation if previous else 0) + 1,
            claimed_at=now,
            expires_at=now + ttl,
        )
        if isinstance(model, (InboundReceipt, MediaWork)):
            extractor_ready = (
                start_extraction
                and isinstance(model, MediaWork)
                and model.stage in {"extract", "associate"}
            )
            if (
                (model.state == "needs_attention" and count_attempt)
                or model.work_clock is None
                or (model.work_clock.next_action_at > now and not extractor_ready)
            ):
                return None
            updated = self._revision(
                record,
                now,
                processing_claim=token,
                state="needs_attention" if model.state == "needs_attention" else "processing",
                work_clock=transition_operational_clock(
                    model.work_clock, now + ttl, attempt_delta=int(count_attempt)
                ),
            )
        else:
            revised = self._revision(record, now)
            updated = StoredRecord.model_validate(
                revised.model_dump() | {"processing_claim": token}
            )
        updated = StoredRecord.model_validate(
            updated.model_dump() | {"claim_generation": token.generation}
        )
        if not self._update(record_item(updated), record.version):
            return None
        return Claim(**token.model_dump(), record_key=record_key, version=updated.version)

    def acquire_patient(
        self, scope: PatientScope, owner: str, now: datetime, ttl: timedelta
    ) -> Lease | None:
        now = utc_instant(now)
        record = self.get(scope, "patient_profile", scope.patient_id)
        if record is None or ttl <= timedelta() or not owner.strip():
            return None
        profile = from_record(record, PatientProfile)
        if profile.lease_expires_at is not None and profile.lease_expires_at > now:
            return None
        lease = Lease(
            scope=scope,
            owner=owner,
            generation=profile.lease_generation + 1,
            claimed_at=now,
            expires_at=now + ttl,
        )
        updated = self._revision(
            record,
            now,
            lease_owner=owner,
            lease_expires_at=lease.expires_at,
            lease_generation=lease.generation,
        )
        return lease if self._update(record_item(updated), record.version) else None

    def release_patient(self, lease: Lease) -> None:
        now = utc_instant(self._clock())
        record = self._lease_record(lease, now)
        if record is not None:
            updated = self._revision(record, now, lease_owner=None, lease_expires_at=None)
            self._update(record_item(updated), record.version)

    def query_due(
        self,
        lane: str,
        shard: str,
        through: datetime,
        cursor: Cursor | None = None,
        limit: int = 100,
        *,
        capability: WorkerCapability | None = None,
    ) -> DuePage:
        """Index hints only: caller MUST re-read the base record/clock before any action."""
        if (
            capability is None
            or lane not in capability.permitted_lanes
            or capability.auth_expiry <= utc_instant(self._clock())
        ):
            return (), None
        binding = keys.digest(
            canonical_json(
                [
                    capability.resolved_scope.model_dump(mode="json"),
                    lane,
                    shard,
                    keys.instant(through),
                ]
            ).decode()
        )
        internal_cursor = None
        if cursor is not None:
            saved = self._due_cursors.get(cursor.position.get("token", ""))
            if cursor.query != binding or saved is None or saved[0] != binding:
                return (), None
            internal_cursor = saved[1]
        items, next_cursor = self._query(
            f"{lane}#{shard}",
            index="GSI_DUE",
            through=keys.instant(through) + "#\uffff",
            cursor=internal_cursor,
            limit=limit,
        )
        hints = []
        for item in items:
            record = self._owned(capability.resolved_scope, Key(item["PK"], item["SK"]))
            if record is None:
                continue
            hints.append(
                DueItem(
                    record_key=record.scoped_key(
                        model_scope(from_record(record, MODELS[record.entity_type]))
                    ),
                    entity_type=record.entity_type,
                    id=record.id,
                    next_action_at=item["due_sort"].split("#", 1)[0],
                )
            )
        public_cursor = None
        if next_cursor is not None:
            token = uuid4().hex
            self._due_cursors[token] = (binding, next_cursor)
            if len(self._due_cursors) > 256:
                self._due_cursors.popitem(last=False)
            public_cursor = Cursor(query=binding, position={"token": token})
        return tuple(hints), public_cursor

    def reserve_contact(
        self, scope: PatientScope, slot_key: str, intent_id: str, expected_fence: Lease
    ) -> Literal["reserved", "already_taken"]:
        now = utc_instant(self._clock())
        if scope != expected_fence.scope:
            return "already_taken"
        profile = self._lease_record(expected_fence, now)
        intent = self.get(scope, "outbound_intent", intent_id)
        if profile is None or intent is None or intent.body.get("slot_id") != slot_key:
            return "already_taken"
        if (
            intent.body.get("status") not in {"queued", "sending"}
            or from_record(intent, OutboundIntent).expires_at <= now
        ):
            return "already_taken"
        key = keys.contact(scope, slot_key)
        item = self._marker_item(key, scope, intent.key, now)
        item.update(state="reserved", fence_generation=expected_fence.generation)
        previous = self._read(key)
        if previous is not None and previous.get("state") != "released":
            # Retain the same logical reservation across a provably failed retry.
            return (
                "reserved"
                if (
                    intent.body.get("status") == "sending"
                    and previous.get("target_sk") == intent.sk
                )
                else "already_taken"
            )
        if previous is not None:
            item["version"] = previous["version"] + 1
        accepted = self._atomic(
            [Write(item, previous["version"] if previous else None)],
            [Check(profile.key, profile.version), Check(intent.key, intent.version)],
        )
        return "reserved" if accepted else "already_taken"

    def start_delivery(
        self,
        intent_id: str,
        expected_versions: tuple[VersionRef, ...],
        owner: str,
        now: datetime,
        *,
        scope: Scope,
        freshness_versions: tuple[VersionRef, ...] = (),
    ) -> DeliveryAttempt | None:
        now = utc_instant(now)
        record = self.get(scope, "outbound_intent", intent_id)
        if record is None or not owner.strip():
            return None
        intent = from_record(record, OutboundIntent)
        if record.processing_claim is not None:
            return None  # Finish the fenced work update before beginning a delivery attempt.
        if intent.status == "sending":
            if intent.delivery_claim is None or intent.delivery_claim.expires_at > now:
                return None
            # Persisted attempt-start means an interrupted network outcome is unknown.
            attempts, _ = self._query(
                record.pk, prefix=f"ATTEMPT#{keys.component(intent_id)}#", limit=100
            )
            if intent.active_attempt_id is not None:
                active = self.get(scope, "delivery_attempt", intent.active_attempt_id)
                attempts = [record_item(active)] if active else []
            for raw in attempts:
                attempt = from_record(item_record(raw), DeliveryAttempt)
                if (
                    attempt.lease_generation == intent.delivery_claim.generation
                    and attempt.outcome == "started"
                ):
                    changed_attempt = self._revision(
                        item_record(raw), now, outcome="uncertain", ended_at=now
                    )
                    uncertain = self._revision(record, now, status="uncertain")
                    self._atomic(
                        [
                            Write(record_item(changed_attempt), attempt.version),
                            Write(record_item(uncertain), record.version),
                        ],
                        [],
                    )
                    return None
            return None  # No proof of an unsent request; recovery policy is slice 03.
        if intent.status != "queued":
            return None
        required = (record.ref, *intent.source_versions)
        if set(expected_versions) != set(required) or len(expected_versions) != len(set(required)):
            return None
        checks = []
        # DANGER is authority-only: never invalidate it through routine expiry/source rules.
        danger = intent.notification_purpose == "DANGER"
        valid = danger or isinstance(scope, AccountScope) or intent.expires_at > now
        for ref in () if danger or isinstance(scope, AccountScope) else intent.source_versions:
            current = self.get(scope, ref.entity_type, ref.id)
            if current is None or current.version != ref.version:
                valid = False
                break
            checks.append(Check(current.key, ref.version))
        for ref in freshness_versions:
            current = (
                self.get_account_source(scope, ref)
                if isinstance(scope, AccountScope)
                else self.get(scope, ref.entity_type, ref.id)
            )
            if current is None or current.version != ref.version:
                return None
            checks.append(Check(current.key, ref.version))
        if not valid:
            suppressed = self._revision(
                record,
                now,
                status="suppressed",
                work_clock=None,
                suppression_reason="stale_source_or_expired",
            )
            self._update(record_item(suppressed), record.version)
            return None
        claim = ProcessingClaim(
            owner=owner,
            generation=(intent.delivery_claim.generation + 1 if intent.delivery_claim else 1),
            claimed_at=now,
            expires_at=now + timedelta(seconds=intent.delivery_lease_seconds),
        )
        attempt_id = uuid4().hex
        attempt = DeliveryAttempt(
            id=attempt_id,
            attempt_id=attempt_id,
            scope=model_scope(intent),
            intent_id=intent_id,
            lease_generation=claim.generation,
            freshness_snapshot=tuple(dict.fromkeys((*expected_versions, *freshness_versions))),
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        assert intent.work_clock is not None
        sending = self._revision(
            record,
            now,
            status="sending",
            delivery_claim=claim,
            active_attempt_id=attempt_id,
            work_clock=transition_operational_clock(intent.work_clock, claim.expires_at),
        )
        writes = [
            Write(record_item(sending), record.version),
            Write(record_item(to_record(attempt, scope)), None),
        ]
        merged = self._merge_checks(writes, checks)
        return attempt if merged is not None and self._atomic(writes, merged) else None

    def complete_delivery(
        self,
        attempt_id: str,
        outcome: DeliveryOutcome,
        provider_message_id: str | None,
        *,
        scope: Scope,
        resolution: DeliveryResolution | None = None,
    ) -> StoredRecord | None:
        now = utc_instant(self._clock())
        record = self.get(scope, "delivery_attempt", attempt_id)
        if record is None or outcome not in {
            "provider_accepted",
            "uncertain",
            "definite_failure",
            "suppressed",
        }:
            return None
        attempt = from_record(record, DeliveryAttempt)
        intent_record = self.get(scope, "outbound_intent", attempt.intent_id)
        if intent_record is None:
            return None
        intent = from_record(intent_record, OutboundIntent)
        if resolution is not None:
            return self._finish_delivery_resolution(
                record, intent_record, outcome, provider_message_id, resolution, now, scope
            )
        if attempt.outcome != "started":
            return (
                intent_record
                if (
                    attempt.outcome == outcome
                    and attempt.provider_message_id == provider_message_id
                )
                else None
            )
        if (
            intent.status != "sending"
            or intent.delivery_claim is None
            or intent.delivery_claim.generation != attempt.lease_generation
        ):
            return None
        if intent.delivery_claim.expires_at <= now:
            outcome, provider_message_id = "uncertain", None
        if outcome == "provider_accepted" and not provider_message_id:
            return None
        if outcome != "provider_accepted" and provider_message_id is not None:
            return None
        changes: dict[str, object] = {
            "status": "failed" if outcome == "definite_failure" else outcome
        }
        if outcome == "provider_accepted":
            changes.update(
                accepted_message_id=provider_message_id, accepted_at=now, work_clock=None
            )
        finished = self._revision(intent_record, now, **changes)
        ended = self._revision(
            record, now, outcome=outcome, ended_at=now, provider_message_id=provider_message_id
        )
        return (
            finished
            if self._atomic(
                [
                    Write(record_item(finished), intent_record.version),
                    Write(record_item(ended), record.version),
                ],
                [],
            )
            else None
        )

    def _finish_delivery_resolution(
        self,
        attempt_record: StoredRecord,
        current: StoredRecord,
        outcome: DeliveryOutcome,
        provider_id: str | None,
        resolution: DeliveryResolution,
        now: datetime,
        scope: Scope,
    ) -> StoredRecord | None:
        attempt = from_record(attempt_record, DeliveryAttempt)
        old = from_record(current, OutboundIntent)
        new = from_record(resolution.intent, OutboundIntent)
        if (
            model_scope(new) != scope
            or new.id != old.id
            or new.version != old.version + 1
            or old.active_attempt_id != attempt.id
            or old.delivery_claim is None
            or old.delivery_claim.generation != attempt.lease_generation
        ):
            return None
        if old.status == "sending" and old.delivery_claim.expires_at <= now:
            return None  # start_delivery recovers this as uncertain before policy settlement.
        if not (
            (old.status == "sending" and attempt.outcome == "started")
            or (
                old.status == "uncertain"
                and attempt.outcome == "uncertain"
                and old.work_clock is not None
            )
            or (
                old.status == "failed"
                and attempt.outcome == "definite_failure"
                and old.work_clock is not None
            )
        ):
            return None
        if (outcome == "provider_accepted") != (provider_id is not None) or (
            outcome == "provider_accepted" and new.accepted_message_id != provider_id
        ):
            return None
        if outcome == "provider_accepted" and (
            new.status != "provider_accepted" or new.accepted_at != now
        ):
            return None
        if outcome == "suppressed" and (
            new.status != "suppressed" or new.suppression_reason is None
        ):
            return None
        if outcome == "uncertain" and (
            new.uncertain_retry_count != old.uncertain_retry_count + 1
            or new.status not in {"queued", "uncertain"}
            or (
                new.status == "queued"
                and (old.notification_purpose != "DANGER" or new.uncertain_retry_count != 1)
            )
        ):
            return None
        if outcome == "definite_failure" and (
            new.status not in {"queued", "failed"} or new.retry_count != old.retry_count + 1
        ):
            return None
        mutable = {
            "version",
            "updated_at",
            "status",
            "work_clock",
            "accepted_message_id",
            "accepted_at",
            "retry_count",
            "uncertain_retry_count",
            "last_error",
            "suppression_reason",
            "review_obligation_id",
            "retryable",
        }
        if new.model_dump(exclude=mutable) != old.model_dump(exclude=mutable):
            return None
        writes = [Write(record_item(to_record(new, scope)), current.version)]
        if attempt.outcome == "started":
            ended = self._revision(
                attempt_record,
                now,
                outcome=outcome,
                ended_at=now,
                provider_message_id=provider_id,
                redacted_error_code=new.last_error,
            )
            writes.append(Write(record_item(ended), attempt_record.version))
        checks: list[Check] = []
        for record in resolution.reviews:
            if isinstance(scope, AccountScope):
                issue = from_record(record, OperationalIssue)
                if (
                    issue.scope != scope
                    or issue.kind != "delivery_failure"
                    or issue.affected_id != new.id
                ):
                    return None
                writes.append(Write(record_item(record), None))
                continue
            review = from_record(record, ReviewObligation)
            if (
                model_scope(review) != scope
                or review.review_kind != "delivery_failure"
                or review.source_type != "outbound_intent"
                or review.source_id != new.id
                or review.source_version != new.version
            ):
                return None
            existing = self.get(scope, "review", review.id)
            if existing is not None:
                checks.append(Check(existing.key, existing.version))
                continue
            writes.append(Write(record_item(to_record(review, scope)), None))
            marker = keys.uniqueness(scope, "REVIEWKEY", keys.digest(review.unique_source_key))
            writes.append(Write(self._marker_item(marker, scope, record.key, now), None))
        if new.review_obligation_id is not None and not any(
            r.id == new.review_obligation_id for r in resolution.reviews
        ):
            return None
        if (
            isinstance(scope, PatientScope)
            and old.slot_id
            and old.notification_purpose == "routine_prompt"
        ):
            reservation = self._read(keys.contact(scope, old.slot_id))
            if reservation and reservation.get("target_sk") == current.sk:
                if outcome == "provider_accepted" or resolution.release_reservation:
                    if resolution.release_reservation and outcome not in {
                        "definite_failure",
                        "suppressed",
                    }:
                        return None
                    changed = reservation | {
                        "version": reservation["version"] + 1,
                        "state": "consumed" if outcome == "provider_accepted" else "released",
                    }
                    writes.append(Write(changed, reservation["version"]))
        if self._atomic(writes, checks):
            return to_record(new, scope)
        return None

    def create_or_get_review(
        self, payload: ReviewCreation, now: datetime
    ) -> tuple[StoredRecord | None, bool]:
        now = utc_instant(now)
        review = payload.review
        try:
            actual_scope = model_scope(review)
        except ValueError:
            return None, False
        if not scope_owns(payload.scope, actual_scope) or review.version != 1:
            return None, False
        record = to_record(review, payload.scope)
        marker = keys.uniqueness(
            model_scope(review), "REVIEWKEY", keys.digest(review.unique_source_key)
        )
        writes = [
            Write(record_item(record), None),
            Write(self._marker_item(marker, model_scope(review), record.key, now), None),
        ]
        if self._atomic(writes, []):
            return record, True
        existing = self._read(marker)
        if existing is None:
            return None, False
        found = self._owned(payload.scope, Key(existing["target_pk"], existing["target_sk"]))
        if (
            found is None
            or found.entity_type != "review"
            or found.body.get("unique_source_key") != review.unique_source_key
        ):
            return None, False
        return found, False

    def load_session(self, scope: PatientScope, key: str) -> SessionSnapshot | None:
        record = self.get(scope, "session_snapshot", key)
        return from_record(record, SessionSnapshot) if record else None

    def commit_session(
        self,
        scope: PatientScope,
        key: str,
        expected_session_version: int,
        fence: Lease,
        *,
        snapshot: SessionSnapshot,
    ) -> bool:
        now = utc_instant(self._clock())
        if (
            scope != fence.scope
            or type(expected_session_version) is not int
            or expected_session_version < 0
            or snapshot.scope != scope
            or snapshot.id != key
            or snapshot.fence_generation != fence.generation
            or snapshot.session_version != expected_session_version + 1
        ):
            return False
        profile = self._lease_record(fence, now)
        if profile is None:
            return False
        if from_record(profile, PatientProfile).safety_epoch != snapshot.safety_epoch:
            return False
        order_checks: list[Check] = []
        for ref in snapshot.source_order_versions:
            order = self.get(scope, "care_order", ref.id)
            if order is None or order.ref != ref or order.body.get("status") != "active":
                return False
            order_checks.append(Check(order.key, order.version))
        record = to_record(snapshot, scope)
        previous = self.get(scope, "session_snapshot", key)
        if previous is not None and (
            previous.key != record.key
            or previous.created_at != record.created_at
            or previous.updated_at > record.updated_at
        ):
            return False
        return self._atomic(
            [Write(record_item(record), expected_session_version or None)],
            [Check(profile.key, profile.version), *order_checks],
        )

    def reconcile_partition(
        self, scope: Scope, cursor: Cursor | None = None, limit: int = 100
    ) -> ReconcileReport:
        items, next_cursor = self._query(keys.partition(scope), cursor=cursor, limit=limit)
        inconsistent: list[Key] = []
        repaired: list[Key] = []
        conflicts: list[Key] = []
        unrepairable: list[Key] = []
        for item in items:
            if item.get("entity_type") not in MODELS or item.get("doctor_id") != (
                None if isinstance(scope, AccountScope) else scope.doctor_id
            ):
                continue
            key = Key(item["PK"], item["SK"])
            try:
                record = item_record(item)
                model = from_record(record, MODELS[record.entity_type])
                if not scope_owns(scope, model_scope(model)):
                    continue
                desired = projections(model)
            except (ValueError, ValidationError):
                # Missing canonical clocks cannot be invented by a projection repair.
                unrepairable.append(key)
                continue
            actual = {field: item[field] for field in PROJECTION_FIELDS if field in item}
            if actual == desired:
                continue
            inconsistent.append(key)
            updated = {k: v for k, v in item.items() if k not in PROJECTION_FIELDS} | desired
            (repaired if self._update(updated, record.version) else conflicts).append(key)
        return ReconcileReport(
            examined=len(items),
            inconsistent=tuple(inconsistent),
            repaired=tuple(repaired),
            conflicts=tuple(conflicts),
            unrepairable=tuple(unrepairable),
            cursor=next_cursor,
        )

    def configure_identity(self, config: IdentityConfig) -> None:
        """Explicit startup configuration, containing no credential or clinical access."""
        self._identity = config

    def authorize(self, bot_id: str, telegram_user_id: str) -> Authorization:
        subject_key = keys.subject(bot_id, telegram_user_id)
        unknown = Principal(
            subject=telegram_user_id, user_id=telegram_user_id, bot_id=bot_id, actor_kind="unknown"
        )
        for _ in range(3):
            raw = self._read(subject_key)
            binding = None
            doctor = None
            checks = [Check(subject_key, raw.get("version") if raw else None)]
            if raw is not None:
                try:
                    binding = from_record(item_record(raw), SubjectBinding)
                except (KeyError, ValueError, TypeError):
                    return Authorization(principal=unknown)
                if binding.doctor_id is not None:
                    key = Key(keys.tenant_pk(TenantScope(doctor_id=binding.doctor_id)), "DOCTOR")
                    row = self._read(key)
                    checks.append(Check(key, row.get("version") if row else None))
                    if row is not None:
                        doctor = from_record(item_record(row), Doctor)
                        if doctor.telegram_bot_id != bot_id:
                            doctor = None
            # Validate a single consistent version cut across the two strong reads.
            if not self._atomic([], checks):
                continue
            roles: set[Literal["admin", "doctor", "patient"]] = set()
            configured_admin = (
                self._identity is not None
                and self._identity.bot_id == bot_id
                and self._identity.admin_user_id == telegram_user_id
            )
            if binding is not None and binding.status == "active" and doctor is not None:
                if "patient" in binding.role_set and not configured_admin:
                    roles.add("patient")
                elif (
                    "doctor" in binding.role_set
                    and doctor.status == "approved"
                    and doctor.telegram_user_id == telegram_user_id
                    and doctor.private_chat_id == binding.private_chat_id
                ):
                    roles.add("doctor")
            if configured_admin:
                roles.add("admin")
            kind: Literal["unknown", "admin", "doctor", "patient"] = (
                "doctor"
                if "doctor" in roles
                else "admin"
                if "admin" in roles
                else "patient"
                if "patient" in roles
                else "unknown"
            )
            principal = Principal(
                subject=telegram_user_id,
                user_id=telegram_user_id,
                bot_id=bot_id,
                actor_kind=kind,
                verified_roles=frozenset(roles),
                doctor_id=binding.doctor_id if binding and roles & {"doctor", "patient"} else None,
                patient_id=binding.patient_id if binding and "patient" in roles else None,
                auth_epoch=doctor.auth_epoch if doctor else None,
            )
            return Authorization(
                principal=principal,
                binding=binding,
                doctor_status=doctor.status if doctor else None,
                auth_epoch=doctor.auth_epoch if doctor else None,
                private_chat_id=binding.private_chat_id
                if binding
                else (telegram_user_id if configured_admin else None),
            )
        return Authorization(principal=unknown)

    def get_account_source(self, scope: AccountScope, ref: VersionRef) -> StoredRecord | None:
        """Narrow account metadata read; never a tenant/patient enumeration capability."""
        if ref.entity_type in {"doctor", "doctor_authority"}:
            tenant = TenantScope(doctor_id=ref.id)
            doctor_row = self.get(tenant, "doctor", ref.id)
            if doctor_row is None or doctor_row.body.get("telegram_bot_id") != scope.bot_id:
                return None
            return (
                doctor_row
                if ref.entity_type == "doctor"
                else self.get(tenant, "doctor_authority", ref.id)
            )
        return self.get(scope, ref.entity_type, ref.id)

    def commit_account(self, request: CommitRequest) -> CommitResult:
        command, scope = request.command, request.command.scope
        worker = command.worker
        if worker is not None and worker.service_subject == "auth":
            if (
                not isinstance(scope, AccountScope)
                or worker.resolved_scope != scope
                or "account" not in worker.permitted_lanes
                or worker.auth_expiry <= utc_instant(self._clock())
                or command.principal.bot_id != scope.bot_id
            ):
                return Forbidden()
            return self._commit(request, account=True, identity=True)
        if (
            not isinstance(scope, AccountScope)
            or worker is None
            or worker.service_subject != "accounts"
            or worker.resolved_scope != scope
            or "account" not in worker.permitted_lanes
            or worker.auth_expiry <= utc_instant(self._clock())
            or command.principal.bot_id != scope.bot_id
        ):
            return Forbidden()
        allowed = {
            "application",
            "doctor",
            "doctor_authority",
            "subject_binding",
            "callback_token",
            "account_ack",
            "operational_issue",
            "inbound_receipt",
        }
        if any(r.entity_type not in allowed for r in request.puts):
            return Forbidden()
        actor = command.principal
        admin = self.authorize(scope.bot_id, actor.subject).principal
        is_admin = "admin" in actor.verified_roles and "admin" in admin.verified_roles
        for row in request.puts:
            if row.entity_type == "subject_binding":
                binding = from_record(row, SubjectBinding)
                if binding.role_set != frozenset({"doctor"}) or not is_admin:
                    return Forbidden()
            if row.entity_type in {"doctor", "doctor_authority", "callback_token"} and not is_admin:
                # Applicants may create only admin-bound action tokens for their own application.
                if row.entity_type != "callback_token" or row.version != 1:
                    return Forbidden()
                token = from_record(row, CallbackToken)
                if self._identity is None or token.actor_subject != self._identity.admin_user_id:
                    return Forbidden()
            if row.entity_type == "application" and not is_admin:
                application = from_record(row, Application)
                old = self.get(scope, "application", application.id)
                if (
                    application.telegram_user_id != actor.subject
                    or application.status != "pending"
                    or (old is not None and old.body.get("status") != "rejected")
                ):
                    return Forbidden()
        return self._commit(request, account=True)

    def acquire_intake(
        self,
        scope: TenantScope,
        intake_id: str,
        expected_version: int,
        owner: str,
        now: datetime,
        ttl: timedelta,
    ) -> Claim | None:
        record = self.get(scope, "intake_draft", intake_id)
        if (
            record is None
            or record.version != expected_version
            or ttl <= timedelta()
            or not owner.strip()
        ):
            return None
        draft = from_record(record, IntakeDraft)
        if draft.state != "pending" or (
            draft.processing_claim and draft.processing_claim.expires_at > now
        ):
            return None
        token = ProcessingClaim(
            owner=owner,
            generation=(draft.processing_claim.generation if draft.processing_claim else 0) + 1,
            claimed_at=now,
            expires_at=now + ttl,
        )
        changed = self._revision(record, now, processing_claim=token)
        if not self._update(record_item(changed), record.version):
            return None
        return Claim(
            **token.model_dump(), record_key=record.scoped_key(scope), version=changed.version
        )

    def raise_intake_concern(self, request: CommitRequest) -> CommitResult:
        if request.command.payload.get("type") != "IntakeDanger":
            return Forbidden()
        return self.commit(request)

    def raise_incident(self) -> None:
        """Deferred to slices 03/04."""
        raise NotImplementedError("raise_incident belongs to slices 03/04")

    def confirm_claim(self, request: CommitRequest) -> CommitResult:
        """The released confirmation uses the same audited account transaction."""
        if request.command.payload.get("type") != "ConfirmPatientClaim":
            return Forbidden()
        return self.commit_account(request)


INDEX_FIELDS = {
    "GSI_DUE": ("due_lane_shard", "due_sort"),
    "GSI_REVIEW": ("review_pk", "review_sort"),
    "GSI_DOCTOR_PATIENTS": ("patients_pk", "patients_sort"),
}


def query_identity(pk: str, index: str | None, prefix: str, through: str | None) -> str:
    return keys.digest(json.dumps([pk, index, prefix, through]))
