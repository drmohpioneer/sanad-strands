"""Durable, doctor-owned proposals and single-use action references."""

from typing import Literal, Self

from pydantic import Field, model_validator

from sanad.domain import Provenance, TenantScope, VersionRef
from sanad.domain.boundaries import NonblankStr, PositiveVersion, UtcInstant, _BoundaryValue
from sanad.domain.deadlines import ResolvedTiming
from sanad.domain.operations import OperationalClock
from sanad.scribe.amend import OrderChange
from sanad.scribe.crosscheck import PhotoReview
from sanad.scribe.extract import PROMPT_VERSION, DictationCandidate, ProposalIssue, ScribeIntent
from sanad.scribe.names import NameReading
from sanad.store import keys


class ScribeRecord(_BoundaryValue):
    id: NonblankStr
    version: PositiveVersion = 1
    created_at: UtcInstant
    updated_at: UtcInstant


class PatientChoice(_BoundaryValue):
    patient_id: NonblankStr
    display_name: NonblankStr
    age: str | None = None
    sex: Literal["male", "female"] | None = None
    updated_at: UtcInstant


class ItemTiming(_BoundaryValue):
    item: str
    resolved: ResolvedTiming


class PendingReply(_BoundaryValue):
    text: str = Field(repr=False)
    source: Provenance
    disputed: tuple[str, ...] = ()
    heard: tuple[str, ...] = ()
    transcript_ref: str | None = None


class Proposal(ScribeRecord):
    entity_type: Literal["scribe_proposal"] = "scribe_proposal"
    scope: TenantScope
    doctor_id: NonblankStr
    timezone: str = "Africa/Cairo"
    selected_patient_id: NonblankStr | None = None
    selected_display_name: str | None = None
    creating_patient: bool = False
    candidate: DictationCandidate = Field(repr=False)
    intent: ScribeIntent = "unclear"
    issues: tuple[ProposalIssue, ...] = ()
    base_versions: tuple[VersionRef, ...] = ()
    choices: tuple[PatientChoice, ...] = Field(default=(), repr=False)
    timings: tuple[ItemTiming, ...] = ()
    source_receipt_id: NonblankStr
    source_transcript_ref: NonblankStr | None = None
    source_text: str = Field(repr=False)
    source_provenance: tuple[Provenance, ...] = ()
    disputed_numbers: tuple[str, ...] = Field(default=(), repr=False)
    heard_numbers: tuple[str, ...] = Field(default=(), repr=False)
    expires_at: UtcInstant
    confirmation_nonce_hash: NonblankStr
    status: Literal["pending", "confirmed", "rejected", "expired", "superseded"] = "pending"
    reason: str | None = None
    review_at: UtcInstant
    work_clock: OperationalClock | None
    editing: bool = False
    supersedes_id: str | None = None
    invitation_requested: bool = False
    prompt_version: str = PROMPT_VERSION
    photo: PhotoReview | None = None
    amendments: tuple[OrderChange, ...] = ()
    names: tuple[NameReading, ...] = Field(default=(), repr=False)
    rxnorm_calls: int = Field(default=0, ge=0, le=6)
    corrected: bool = False
    resolved_numbers: tuple[str, ...] = ()
    pending_reply: PendingReply | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def lifecycle(self) -> Self:
        if type(self.scope) is not TenantScope or self.scope.doctor_id != self.doctor_id:
            raise ValueError("proposal requires the exact owning doctor scope")
        if self.creating_patient and self.selected_patient_id:
            raise ValueError("creation cannot select an existing patient")
        if (self.status == "pending") != (self.work_clock is not None):
            raise ValueError("only pending proposals have work")
        if self.work_clock and self.work_clock.work_lane != "scribe":
            raise ValueError("proposal requires scribe work")
        if self.review_at != self.expires_at or self.expires_at <= self.created_at:
            raise ValueError("proposal review must expose its expiry")
        keys.token("scribe", self.confirmation_nonce_hash)
        return self

    def blocked(self, item: str) -> bool:
        from sanad.scribe.crosscheck import unreadable_read

        if self.photo and unreadable_read(self.photo.reads):
            return True
        return any(i.blocked and i.item in {"all", item} for i in self.issues)


class ScribeState(ScribeRecord):
    entity_type: Literal["scribe_state"] = "scribe_state"
    scope: TenantScope
    pending_proposal_id: str | None = None


class InvitationWork(ScribeRecord):
    entity_type: Literal["scribe_invitation_work"] = "scribe_invitation_work"
    scope: TenantScope
    patient_id: NonblankStr
    proposal_id: NonblankStr
    status: Literal["pending", "issued", "suppressed"] = "pending"
    work_clock: OperationalClock | None

    @model_validator(mode="after")
    def lifecycle(self) -> Self:
        if (self.status == "pending") != (self.work_clock is not None):
            raise ValueError("pending invitation requests retain work")
        return self


class ScribeCallback(ScribeRecord):
    entity_type: Literal["scribe_callback"] = "scribe_callback"
    scope: TenantScope
    proposal_id: NonblankStr
    proposal_version: PositiveVersion
    actor_subject: NonblankStr
    action: Literal[
        "confirm", "edit", "reject", "select", "new", "reading", "correct_reply", "new_reply"
    ]
    patient_id: str | None = None
    field: str | None = None
    reading: Literal[0, 1] | None = None
    expires_at: UtcInstant
    consumed_at: UtcInstant | None = None

    @model_validator(mode="after")
    def binding(self) -> Self:
        keys.token("scribe", self.id)
        if (self.action == "select") != (self.patient_id is not None):
            raise ValueError("only selection buttons specify a server-selected patient")
        return self
