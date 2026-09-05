"""Store boundary. Scopes/capabilities must be established by trusted dispatch.

Authentication is deferred to 05. Bare IDs never carry authority: delivery uses
an explicit scope keyword; work keys embed scope; due queries require a scoped
service capability. Missing/foreign scope returns no record. Snapshot time is
provided by an injectable store clock wherever the operation has no now argument.
"""

from datetime import datetime, timedelta
from typing import Literal, Protocol

from sanad.domain import (
    FollowUpTask,
    Mission,
    PatientScope,
    ReviewObligation,
    TenantScope,
    VersionRef,
)
from sanad.store.keys import Scope, ScopedKey
from sanad.store.records import (
    Claim,
    CommitRequest,
    CommitResult,
    Cursor,
    DeliveryAttempt,
    DeliveryOutcome,
    DuePage,
    InboundAccept,
    InboundReceiptRecord,
    Lease,
    PatientProfile,
    ReconcileReport,
    RecordPage,
    ReviewCreation,
    SessionSnapshot,
    StoredRecord,
    WorkerCapability,
)


class Store(Protocol):
    def get(self, scope: Scope, entity_type: str, id: str) -> StoredRecord | None: ...
    def get_mission(self, scope: PatientScope, id: str) -> Mission | None: ...
    def get_followup(self, scope: PatientScope, id: str) -> FollowUpTask | None: ...
    def get_review(self, scope: Scope, id: str) -> ReviewObligation | None: ...
    def get_patient_profile(self, scope: PatientScope) -> PatientProfile | None: ...
    def list_patients(
        self, tenant_scope: TenantScope, cursor: Cursor | None = None, limit: int = 100
    ) -> RecordPage: ...
    def list_reviews(
        self, tenant_scope: TenantScope, cursor: Cursor | None = None, limit: int = 100
    ) -> RecordPage: ...
    def list_events(
        self, patient_scope: PatientScope, cursor: Cursor | None = None, limit: int = 100
    ) -> RecordPage: ...
    def commit(self, request: CommitRequest) -> CommitResult:
        """expected names target write versions; command.expected_versions names read versions."""
        ...

    def accept_inbound(
        self, transport_key: str, receipt: InboundReceiptRecord
    ) -> InboundAccept: ...
    def claim_work(
        self,
        record_key: ScopedKey,
        expected_version: int,
        owner: str,
        now: datetime,
        ttl: timedelta,
    ) -> Claim | None: ...
    def acquire_patient(
        self, scope: PatientScope, owner: str, now: datetime, ttl: timedelta
    ) -> Lease | None: ...
    def release_patient(self, lease: Lease) -> None: ...
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
        """Service-only hints: re-read each base record and check clock/version before acting."""
        ...

    def reserve_contact(
        self, scope: PatientScope, slot_key: str, intent_id: str, expected_fence: Lease
    ) -> Literal["reserved", "already_taken"]: ...
    def start_delivery(
        self,
        intent_id: str,
        expected_versions: tuple[VersionRef, ...],
        owner: str,
        now: datetime,
        *,
        scope: Scope,
    ) -> DeliveryAttempt | None:
        """Persist attempt and check source versions; dispatcher owns send-time policy in 03."""
        ...

    def complete_delivery(
        self,
        attempt_id: str,
        outcome: DeliveryOutcome,
        provider_message_id: str | None,
        *,
        scope: Scope,
    ) -> StoredRecord | None: ...
    def create_or_get_review(
        self, payload: ReviewCreation, now: datetime
    ) -> tuple[StoredRecord | None, bool]: ...
    def load_session(self, scope: PatientScope, key: str) -> SessionSnapshot | None: ...
    def commit_session(
        self,
        scope: PatientScope,
        key: str,
        expected_session_version: int,
        fence: Lease,
        *,
        snapshot: SessionSnapshot,
    ) -> bool: ...
    def reconcile_partition(
        self, scope: Scope, cursor: Cursor | None = None, limit: int = 100
    ) -> ReconcileReport: ...

    # These named operations deliberately have no implementation in slice 02.
    def authorize(self) -> None:
        """Deferred to slice 05: current identity and authority checks."""

    def acquire_intake(self) -> None:
        """Deferred to slice 09: intake processing fence."""

    def raise_intake_concern(self) -> None:
        """Deferred to slice 09: independently deduplicated intake danger."""

    def raise_incident(self) -> None:
        """Deferred to slices 03/04: urgent transaction and safety epoch."""

    def confirm_claim(self) -> None:
        """Deferred to slice 06: consent and intended-person confirmation."""
