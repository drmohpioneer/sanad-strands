"""Durable, doctor-owned proposals and single-use action references."""

from typing import Literal, Self

from pydantic import Field, model_validator

from sanad.domain import Provenance, TenantScope, VersionRef
from sanad.domain.boundaries import NonblankStr, PositiveVersion, UtcInstant, _BoundaryValue
from sanad.domain.deadlines import ResolvedTiming
from sanad.domain.language import Language, default_language
from sanad.domain.operations import OperationalClock
from sanad.scribe.amend import OrderChange
from sanad.scribe.change_binding import SourcePartition
from sanad.scribe.crosscheck import PhotoReview
from sanad.scribe.extract import PROMPT_VERSION, DictationCandidate, ProposalIssue, ScribeIntent
from sanad.scribe.grounding import FieldEvidence
from sanad.scribe.names import NameReading
from sanad.store import keys


class ScribeRecord(_BoundaryValue):
    id: NonblankStr
    version: PositiveVersion = 1
    created_at: UtcInstant
    updated_at: UtcInstant


class PatientChoice(_BoundaryValue):
    score: int = Field(default=0, ge=0, le=4)
    headline: str | None = None
    patient_id: NonblankStr
    display_name: NonblankStr
    age: str | None = None
    sex: Literal["male", "female"] | None = None
    updated_at: UtcInstant


class ItemTiming(_BoundaryValue):
    item: str
    resolved: ResolvedTiming


class DisplayedSchedule(_BoundaryValue):
    item: str
    slots: tuple[UtcInstant, ...]
    slot_rule: Literal["tolerance-3h", "window-next-v1"]


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
    language: Language = default_language
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
    source_partition: SourcePartition | None = None
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
    single_source: tuple[str, ...] = ()
    pending_reply: PendingReply | None = Field(default=None, repr=False)
    evidence: tuple[FieldEvidence, ...] = Field(default=(), repr=False)
    evidence_fingerprint: str = ""
    displayed_schedules: tuple[DisplayedSchedule, ...] = ()

    def with_displayed_schedules(self) -> "Proposal":
        from sanad.scribe.monitoring import compile_schedule

        previews = []
        for i, mission in enumerate(self.candidate.missions):
            if mission.kind != "MONITOR":
                continue
            schedule = compile_schedule(mission.text)
            if schedule is None:
                continue
            try:
                details = schedule.details(self.created_at, self.timezone)
            except ValueError:
                continue
            previews.append(
                DisplayedSchedule(
                    item=f"mission:{i}", slots=details.slots, slot_rule=details.slot_rule
                )
            )
        return self.model_copy(update={"displayed_schedules": tuple(previews)})

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
        from sanad.scribe.grounding import invalid_claims

        if self.evidence_fingerprint and any(c.item in {"all", item} for c in invalid_claims(self)):
            return True

        if self.photo and unreadable_read(self.photo.reads):
            return True
        return any(i.blocked and i.item in {"all", item} for i in self.issues)


def card_actions(proposal: Proposal) -> tuple[tuple[str, str | None], ...]:
    """One issue/identity predicate supplies both the footer and callback actions."""
    from sanad.scribe.crosscheck import unreadable_read

    if proposal.photo and unreadable_read(proposal.photo.reads):
        return ()
    if proposal.pending_reply:
        return (("correct_reply", None), ("new_reply", None), ("reject", None))
    if proposal.choices and not proposal.selected_patient_id and not proposal.creating_patient:
        return (
            *(("select", c.patient_id) for c in proposal.choices),
            ("new", None),
            ("reject", None),
        )
    photo_confirmable = not proposal.photo or any(
        not proposal.blocked(f"{family}:{i}")
        for family, values in (
            ("order", proposal.candidate.orders),
            ("fact", proposal.candidate.facts),
        )
        for i, _ in enumerate(values)
    )
    confirm = not proposal.blocked("all") and not proposal.blocked("patient") and photo_confirmable
    return (*((("confirm", None),) if confirm else ()), ("edit", None), ("reject", None))


def card_footer(proposal: Proposal, language: str) -> str:
    from sanad.channels.telegram import wording

    return " | ".join(
        next(c.display_name for c in proposal.choices if c.patient_id == patient_id)
        if action == "select"
        else wording.button(action, language)
        for action, patient_id in card_actions(proposal)
    )


class ScribeState(ScribeRecord):
    entity_type: Literal["scribe_state"] = "scribe_state"
    scope: TenantScope
    pending_proposal_id: str | None = None


class InvitationWork(ScribeRecord):
    entity_type: Literal["scribe_invitation_work"] = "scribe_invitation_work"
    scope: TenantScope
    patient_id: NonblankStr
    proposal_id: NonblankStr
    generation: int = 0
    status: Literal["pending", "issued", "suppressed", "superseded"] = "pending"
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
