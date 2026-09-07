"""Typed commands, prevalidated caller facts, and inert persistence effects.

ResolveReview is a command; ResolveReviewEffect is the mission extension's
source-wide unmet-objective disposition. The distinct names prevent accidental
use of that broader effect as an exact-version review command.
"""

from typing import Annotated, Literal

from pydantic import Field, StrictBool

from sanad.domain.boundaries import (
    ActorKind,
    NonblankStr,
    NonnegativeInt,
    ObservationRef,
    PositiveVersion,
    TimingProposal,
    UtcInstant,
    VersionRef,
    _BoundaryValue,
)
from sanad.domain.deadlines import ExplicitTiming, ResolvedTiming
from sanad.domain.entities import (
    EvidenceRef,
    FollowUpAnchorKind,
    FollowUpKind,
    FollowUpTask,
    Mission,
    MissionDetails,
    MissionKind,
    ReviewAction,
    ReviewKind,
    ReviewObligation,
)
from sanad.domain.predicates import ObjectivePredicate, PredicateResult


class _Event(_BoundaryValue):
    event_id: NonblankStr


class ProposalCreated(_Event):
    event_type: Literal["PROPOSAL_CREATED"] = "PROPOSAL_CREATED"
    mission_id: NonblankStr
    doctor_id: NonblankStr
    patient_id: NonblankStr
    created_at: UtcInstant
    kind: MissionKind
    title: NonblankStr
    details: MissionDetails
    objective_predicate: ObjectivePredicate
    order_refs: tuple[VersionRef, ...] = ()
    source_proposal_id: NonblankStr | None = None
    timing: ResolvedTiming


class ConfirmMission(_Event):
    event_type: Literal["CONFIRM_MISSION"] = "CONFIRM_MISSION"
    consent_active: StrictBool
    actor_id: NonblankStr
    explicit: ExplicitTiming | None = None
    proposal: TimingProposal | None = None
    schedule_end: UtcInstant | None = None
    grace_seconds: NonnegativeInt | None = None
    anchor_kind: FollowUpAnchorKind = "reported_effective_start"
    anchor_time: UtcInstant | None = None


class CreateSupportTicket(_Event):
    event_type: Literal["CREATE_SUPPORT_TICKET"] = "CREATE_SUPPORT_TICKET"
    mission_id: NonblankStr
    doctor_id: NonblankStr
    patient_id: NonblankStr
    title: NonblankStr
    question_text: NonblankStr
    source_observation_ref: ObservationRef


class PatientBound(_Event):
    event_type: Literal["PATIENT_BOUND"] = "PATIENT_BOUND"
    consent_active: StrictBool


class ContactScheduled(_Event):
    event_type: Literal["CONTACT_SCHEDULED"] = "CONTACT_SCHEDULED"
    next_contact_at: UtcInstant
    slot_id: NonblankStr
    template_id: NonblankStr
    kind: Literal["chase", "scheduled"]
    emit: StrictBool = True
    expires_at: UtcInstant | None = None


class ContactAccepted(_Event):
    event_type: Literal["CONTACT_ACCEPTED"] = "CONTACT_ACCEPTED"
    accepted_at: UtcInstant | None = None
    kind: Literal["chase", "scheduled"] = "chase"


class PatientReplied(_Event):
    event_type: Literal["PATIENT_REPLIED"] = "PATIENT_REPLIED"


class BarrierRecorded(_Event):
    event_type: Literal["BARRIER_RECORDED"] = "BARRIER_RECORDED"
    barrier_type: NonblankStr
    reason: NonblankStr
    resume_at: UtcInstant


class BarrierResolved(_Event):
    event_type: Literal["BARRIER_RESOLVED"] = "BARRIER_RESOLVED"


class ContactExhausted(_Event):
    event_type: Literal["CONTACT_EXHAUSTED"] = "CONTACT_EXHAUSTED"
    exhausted: StrictBool


class PauseContact(_Event):
    event_type: Literal["PAUSE_CONTACT"] = "PAUSE_CONTACT"
    reason: NonblankStr
    resume_at: UtcInstant


class ResumeContact(_Event):
    event_type: Literal["RESUME_CONTACT"] = "RESUME_CONTACT"
    consent_active: StrictBool
    order_active: StrictBool
    doctor_active: StrictBool


class DeadlineReached(_Event):
    event_type: Literal["DEADLINE_REACHED"] = "DEADLINE_REACHED"


class ObjectiveFulfilled(_Event):
    event_type: Literal["OBJECTIVE_FULFILLED"] = "OBJECTIVE_FULFILLED"
    predicate_result: PredicateResult
    objective_received_at: UtcInstant
    evidence_refs: tuple[EvidenceRef, ...] = ()
    fulfillment_event_id: NonblankStr
    danger_flag: StrictBool
    actor_kind: ActorKind


class DoctorExtend(_Event):
    event_type: Literal["DOCTOR_EXTEND"] = "DOCTOR_EXTEND"
    actor_id: NonblankStr
    timing: ExplicitTiming
    grace_seconds: NonnegativeInt | None = None
    reason: NonblankStr
    # Missing external binding information must never activate contact.
    consent_active: StrictBool = False


class DoctorCancel(_Event):
    event_type: Literal["DOCTOR_CANCEL"] = "DOCTOR_CANCEL"
    actor_id: NonblankStr
    reason: NonblankStr


class DoctorCloseUnfulfilled(_Event):
    event_type: Literal["DOCTOR_CLOSE_UNFULFILLED"] = "DOCTOR_CLOSE_UNFULFILLED"
    actor_id: NonblankStr
    reason: NonblankStr
    open_incident: StrictBool


class DoctorReopen(_Event):
    event_type: Literal["DOCTOR_REOPEN"] = "DOCTOR_REOPEN"
    actor_id: NonblankStr
    timing: ExplicitTiming
    grace_seconds: NonnegativeInt | None = None
    new_objective_predicate: ObjectivePredicate
    new_order_refs: tuple[VersionRef, ...]
    current_active_order_refs: tuple[VersionRef, ...]
    reason: NonblankStr
    consent_active: StrictBool = False


class OrderSuperseded(_Event):
    event_type: Literal["ORDER_SUPERSEDED"] = "ORDER_SUPERSEDED"
    successor_order_ref: VersionRef | None


class CorrectAcceptedEvidence(_Event):
    event_type: Literal["CORRECT_ACCEPTED_EVIDENCE"] = "CORRECT_ACCEPTED_EVIDENCE"
    superseded_evidence_ref: EvidenceRef
    correcting_evidence_ref: EvidenceRef
    predicate_still_holds: StrictBool


class ValidateCorrection(_Event):
    event_type: Literal["VALIDATE_CORRECTION"] = "VALIDATE_CORRECTION"
    predicate_result: PredicateResult


class LateInputRecorded(_Event):
    event_type: Literal["LATE_INPUT_RECORDED"] = "LATE_INPUT_RECORDED"
    observation_ref: ObservationRef


class EvidenceAssociated(_Event):
    event_type: Literal["EVIDENCE_ASSOCIATED"] = "EVIDENCE_ASSOCIATED"
    evidence_ref: EvidenceRef


class ReviewAcknowledged(_Event):
    event_type: Literal["REVIEW_ACKNOWLEDGED"] = "REVIEW_ACKNOWLEDGED"
    obligation_id: NonblankStr
    actor_id: NonblankStr


class ReviewResolved(_Event):
    event_type: Literal["REVIEW_RESOLVED"] = "REVIEW_RESOLVED"
    obligation_id: NonblankStr
    action: ReviewAction
    expected_source_version: PositiveVersion
    actor_id: NonblankStr
    reason: NonblankStr


class SafetyIncidentRaised(_Event):
    event_type: Literal["SAFETY_INCIDENT_RAISED"] = "SAFETY_INCIDENT_RAISED"


class ContactPreferenceChanged(_Event):
    event_type: Literal["CONTACT_PREFERENCE_CHANGED"] = "CONTACT_PREFERENCE_CHANGED"
    reason: NonblankStr


type MissionEvent = Annotated[
    ProposalCreated
    | ConfirmMission
    | CreateSupportTicket
    | PatientBound
    | ContactScheduled
    | ContactAccepted
    | PatientReplied
    | BarrierRecorded
    | BarrierResolved
    | ContactExhausted
    | PauseContact
    | ResumeContact
    | DeadlineReached
    | ObjectiveFulfilled
    | DoctorExtend
    | DoctorCancel
    | DoctorCloseUnfulfilled
    | DoctorReopen
    | OrderSuperseded
    | CorrectAcceptedEvidence
    | ValidateCorrection
    | LateInputRecorded
    | EvidenceAssociated
    | ReviewAcknowledged
    | ReviewResolved
    | SafetyIncidentRaised
    | ContactPreferenceChanged,
    Field(discriminator="event_type"),
]


class CreateFollowUp(_Event):
    effect_type: Literal["create_followup"] = "create_followup"
    kind: FollowUpKind
    parent_mission_id: NonblankStr
    order_refs: tuple[VersionRef, ...]
    anchor_kind: FollowUpAnchorKind
    anchor_time: UtcInstant | None = None
    prompt_at: UtcInstant | None = None
    response_predicate: ObjectivePredicate
    confirmed_by: NonblankStr
    confirmed_at: UtcInstant
    doctor_id: NonblankStr
    patient_id: NonblankStr


class ConfirmFollowUp(CreateFollowUp):
    event_type: Literal["CONFIRM_FOLLOWUP"] = "CONFIRM_FOLLOWUP"


class AnchorConfirmed(_Event):
    event_type: Literal["ANCHOR_CONFIRMED"] = "ANCHOR_CONFIRMED"
    anchor_kind: FollowUpAnchorKind
    anchor_time: UtcInstant
    anchor_source_ref: ObservationRef | None = None
    replace_existing: StrictBool = False


class PromptScheduled(_Event):
    event_type: Literal["PROMPT_SCHEDULED"] = "PROMPT_SCHEDULED"
    slot_id: NonblankStr
    expires_at: UtcInstant | None = None


class PromptAccepted(_Event):
    event_type: Literal["PROMPT_ACCEPTED"] = "PROMPT_ACCEPTED"


class ResponseReceived(_Event):
    event_type: Literal["RESPONSE_RECEIVED"] = "RESPONSE_RECEIVED"
    predicate_result: PredicateResult
    objective_received_at: UtcInstant
    source_report_ids: tuple[NonblankStr, ...] = ()
    danger_flag: StrictBool


class FollowUpDeadline(_Event):
    event_type: Literal["FOLLOWUP_DEADLINE"] = "FOLLOWUP_DEADLINE"


class SuppressFollowUpContact(_Event):
    event_type: Literal["SUPPRESS_FOLLOWUP_CONTACT"] = "SUPPRESS_FOLLOWUP_CONTACT"
    reason: NonblankStr


class CancelFollowUp(_Event):
    event_type: Literal["CANCEL_FOLLOWUP"] = "CANCEL_FOLLOWUP"
    reason: NonblankStr


type FollowUpEvent = Annotated[
    ConfirmFollowUp
    | AnchorConfirmed
    | PromptScheduled
    | PromptAccepted
    | ResponseReceived
    | FollowUpDeadline
    | SuppressFollowUpContact
    | CancelFollowUp,
    Field(discriminator="event_type"),
]


class CreateReview(_Event):
    event_type: Literal["CREATE_REVIEW"] = "CREATE_REVIEW"
    effect_type: Literal["create_review"] = "create_review"
    review_kind: ReviewKind
    source_type: NonblankStr
    source_id: NonblankStr
    source_version: PositiveVersion
    review_at: UtcInstant
    owner_doctor_id: NonblankStr
    patient_id: NonblankStr | None = None
    source_mission_id: NonblankStr | None = None


class AcknowledgeReview(_Event):
    event_type: Literal["ACKNOWLEDGE_REVIEW"] = "ACKNOWLEDGE_REVIEW"
    actor_id: NonblankStr


class ResolveReview(_Event):
    event_type: Literal["RESOLVE_REVIEW"] = "RESOLVE_REVIEW"
    action: ReviewAction
    expected_source_version: PositiveVersion
    actor_id: NonblankStr
    reason: NonblankStr


class BlockCoverage(_Event):
    event_type: Literal["BLOCK_COVERAGE"] = "BLOCK_COVERAGE"


class RestoreCoverage(_Event):
    event_type: Literal["RESTORE_COVERAGE"] = "RESTORE_COVERAGE"


class MaterialChange(_Event):
    event_type: Literal["MATERIAL_CHANGE"] = "MATERIAL_CHANGE"
    version: PositiveVersion


type ReviewEvent = Annotated[
    CreateReview
    | AcknowledgeReview
    | ResolveReview
    | BlockCoverage
    | RestoreCoverage
    | MaterialChange,
    Field(discriminator="event_type"),
]


class ResolveReviewEffect(_BoundaryValue):
    effect_type: Literal["resolve_review"] = "resolve_review"
    review_kind: ReviewKind
    source_type: NonblankStr
    source_id: NonblankStr
    reason: NonblankStr


class ApplyReviewEvent(_BoundaryValue):
    effect_type: Literal["apply_review_event"] = "apply_review_event"
    obligation_id: NonblankStr
    event: Annotated[AcknowledgeReview | ResolveReview, Field(discriminator="event_type")]


class AnchorFollowUp(_BoundaryValue):
    effect_type: Literal["anchor_followup"] = "anchor_followup"
    parent_mission_id: NonblankStr
    anchor_time: UtcInstant
    anchor_kind: FollowUpAnchorKind


class EmitIntent(_BoundaryValue):
    effect_type: Literal["emit_intent"] = "emit_intent"
    purpose: Literal["DANGER", "DONE:FULFILLMENT", "DONE:CORRECTION", "DEADLINE", "routine_prompt"]
    audience: Literal["doctor", "patient"] = "doctor"
    slot: str = ""
    template_id: NonblankStr | None = None
    contact_kind: Literal["chase", "scheduled"] | None = None
    expires_at: UtcInstant | None = None
    source_event_id: NonblankStr
    source_version: PositiveVersion
    facts_ref: NonblankStr


class SuppressRoutineIntents(_BoundaryValue):
    effect_type: Literal["suppress_routine_intents"] = "suppress_routine_intents"
    reason: NonblankStr


class RecordAudit(_BoundaryValue):
    effect_type: Literal["record_audit"] = "record_audit"
    event_type: NonblankStr
    event_id: NonblankStr
    before_version: NonnegativeInt
    after_version: PositiveVersion


class RetainObservation(_BoundaryValue):
    effect_type: Literal["retain_observation"] = "retain_observation"
    observation_ref: ObservationRef


class RecordEvidenceAssociation(_BoundaryValue):
    effect_type: Literal["record_evidence_association"] = "record_evidence_association"
    evidence_ref: EvidenceRef


class SupersedeEvidence(_BoundaryValue):
    effect_type: Literal["supersede_evidence"] = "supersede_evidence"
    superseded_ref: EvidenceRef
    correcting_ref: EvidenceRef


type Effect = Annotated[
    CreateReview
    | ResolveReviewEffect
    | ApplyReviewEvent
    | CreateFollowUp
    | AnchorFollowUp
    | EmitIntent
    | SuppressRoutineIntents
    | RecordAudit
    | RetainObservation
    | RecordEvidenceAssociation
    | SupersedeEvidence,
    Field(discriminator="effect_type"),
]
type Aggregate = Annotated[
    Mission | FollowUpTask | ReviewObligation, Field(discriminator="entity_type")
]


class TransitionResult(_BoundaryValue):
    aggregate: Aggregate
    effects: tuple[Effect, ...] = ()
    noop: StrictBool = False


class TransitionRejected(_BoundaryValue):
    reason_code: NonblankStr
    message: NonblankStr
    state: NonblankStr
    event_type: NonblankStr
    effects: tuple[Effect, ...] = ()


class IllegalTransition(ValueError):
    def __init__(self, rejection: TransitionRejected):
        self.rejection = rejection
        super().__init__(rejection.message)
