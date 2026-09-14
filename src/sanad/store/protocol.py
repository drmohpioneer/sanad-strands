"""Store boundary. Scopes/capabilities must be established by trusted dispatch.

Identity is resolved from stored bindings and explicit admin settings.
Bare IDs never carry authority: delivery uses
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
from sanad.store.keys import AccountScope, Scope, ScopedKey
from sanad.store.records import (
    AnyWebSession,
    Authorization,
    Claim,
    CommandEnvelope,
    CommitRequest,
    CommitResult,
    Cursor,
    DeliveryAttempt,
    DeliveryOutcome,
    DeliveryResolution,
    DuePage,
    IdentityConfig,
    InboundAccept,
    InboundReceiptRecord,
    Lease,
    OutboundIntent,
    PatientProfile,
    ReconcileReport,
    RecordPage,
    ReviewCreation,
    SessionSnapshot,
    StoredRecord,
    SweepPosition,
    UploadStage,
    WebSession,
    WorkerCapability,
)


class Store(Protocol):
    def web_session_snapshot(self, session: AnyWebSession) -> AnyWebSession | None: ...

    def patient_receipts(
        self,
        scope: PatientScope,
        cursor: Cursor | None = None,
        limit: int = 50,
        *,
        kinds: tuple[str, ...] = (),
    ) -> RecordPage: ...

    def patient_timeline(
        self,
        scope: PatientScope,
        prefix: str,
        cursor: Cursor | None = None,
        limit: int = 50,
        *,
        kinds: tuple[str, ...] = (),
    ) -> RecordPage: ...

    def reserve_browser_command(self, session: WebSession, command_id: str, digest: str) -> str: ...

    def save_question_listing(
        self, intent: OutboundIntent, basis: tuple[VersionRef, ...], now: datetime
    ) -> StoredRecord | None: ...

    def lookup_command(self, command: CommandEnvelope) -> CommitResult | None: ...
    def list_records(
        self, scope: Scope, entity_type: str, cursor: Cursor | None = None, limit: int = 100
    ) -> RecordPage:
        """Strongly read one scoped base partition, including terminal records."""
        ...

    def commit_incident(self, request: CommitRequest) -> CommitResult:
        """Conditional urgent transaction, restricted to incident records and safety epoch."""
        ...

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
        self, transport_key: str, receipt: InboundReceiptRecord, *, upload_id: str | None = None
    ) -> InboundAccept: ...
    def upload_authorized(self, stage: UploadStage) -> bool: ...
    def reserve_upload(self, stage: UploadStage) -> bool: ...
    def discard_upload(self, scope: PatientScope, id: str, version: int) -> UploadStage | None: ...
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
    ) -> Claim | None: ...
    def acquire_patient(
        self,
        scope: PatientScope,
        owner: str,
        now: datetime,
        ttl: timedelta,
        *,
        expected_version: int | None = None,
    ) -> Lease | None: ...
    def release_patient(self, lease: Lease) -> None: ...

    def defer_media(self, claim: Claim, now: datetime, error: str) -> bool: ...

    def defer_inbound(self, claim: Claim, now: datetime) -> bool: ...
    def read_sweep_position(
        self, scope: AccountScope, lane: str, shard: str
    ) -> SweepPosition | None: ...
    def save_sweep_position(
        self, position: SweepPosition, expected_version: int | None
    ) -> bool: ...
    def due_resume_cursor(
        self, lane: str, shard: str, through: datetime, position: SweepPosition
    ) -> Cursor | None: ...

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

    def complete_notice_feedback(
        self, scope: Scope, intent_id: str, resolution: DeliveryResolution
    ) -> StoredRecord | None: ...

    def mark_contact_feedback(self, scope: PatientScope, intent_id: str) -> StoredRecord | None: ...

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
        freshness_versions: tuple[VersionRef, ...] = (),
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
        resolution: DeliveryResolution | None = None,
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

    # Identity operations are shared by both backends.
    def configure_identity(self, config: IdentityConfig) -> None: ...
    def authorize(self, bot_id: str, telegram_user_id: str) -> Authorization: ...
    def get_account_source(self, scope: AccountScope, ref: VersionRef) -> StoredRecord | None: ...
    def commit_account(self, request: CommitRequest) -> CommitResult: ...

    # Later slices own the remaining named operations.
    def acquire_intake(
        self,
        scope: TenantScope,
        intake_id: str,
        expected_version: int,
        owner: str,
        now: datetime,
        ttl: timedelta,
    ) -> Claim | None:
        """Conditionally claim a pending doctor-private draft, fencing stale associations."""
        ...

    def raise_intake_concern(self, request: CommitRequest) -> CommitResult:
        """Atomically persist intake danger through the guarded doctor gateway."""
        ...

    def raise_incident(self) -> None:
        """Deferred to slices 03/04: urgent transaction and safety epoch."""

    def confirm_claim(self, request: CommitRequest) -> CommitResult:
        """Conditional cross-partition claim confirmation through commit_account."""
        ...
