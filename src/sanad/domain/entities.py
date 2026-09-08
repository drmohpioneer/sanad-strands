"""Immutable contract 01 aggregates and the explicitly supplied timing policy."""

import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, StrictBool, model_validator

from sanad.domain.boundaries import (
    AcceptedFactRef,
    IanaZone,
    NonblankStr,
    NonnegativeInt,
    ObservationRef,
    PositiveVersion,
    UtcInstant,
    VersionRef,
    _BoundaryValue,
)
from sanad.domain.predicates import ObjectivePredicate


class MissionKind(StrEnum):
    TEST = "TEST"
    MONITOR = "MONITOR"
    MEDICATION = "MEDICATION"
    SEND_RECORDS = "SEND_RECORDS"
    VISIT = "VISIT"
    QUESTION = "QUESTION"
    TASK = "TASK"


class MissionState(StrEnum):
    proposed = "proposed"
    awaiting_link = "awaiting_link"
    open = "open"
    waiting_patient = "waiting_patient"
    blocked = "blocked"
    unreachable = "unreachable"
    overdue = "overdue"
    fulfilled = "fulfilled"
    cancelled = "cancelled"
    closed_unfulfilled = "closed_unfulfilled"
    superseded = "superseded"


TERMINAL_STATES = frozenset(
    {
        MissionState.fulfilled,
        MissionState.cancelled,
        MissionState.closed_unfulfilled,
        MissionState.superseded,
    }
)


class FulfillmentValidity(StrEnum):
    not_fulfilled = "not_fulfilled"
    valid = "valid"
    invalidated_pending_review = "invalidated_pending_review"


class Timeliness(StrEnum):
    undetermined = "undetermined"
    on_time = "on_time"
    late = "late"


class DueSource(StrEnum):
    doctor = "doctor"
    scribe = "scribe"
    default = "default"


class WorkClock(_BoundaryValue):
    next_action_at: UtcInstant
    work_lane: Literal["mission", "followup", "review"]
    work_shard: NonblankStr = "0"
    work_generation: PositiveVersion = 1
    attempt_count: NonnegativeInt = 0
    last_error_code: NonblankStr | None = None


def _evidence_ref(value: AcceptedFactRef) -> AcceptedFactRef:
    if value.fact_kind != "evidence":
        raise ValueError("evidence references require fact_kind=evidence")
    return value


type EvidenceRef = Annotated[AcceptedFactRef, AfterValidator(_evidence_ref)]
type AnchorKind = Literal[
    "observation_received",
    "doctor_reference_time",
    "schedule_end",
    "reported_effective_start",
    "reported_effective_change",
    "confirmation",
    "extension",
    "reopen",
    "ticket_created",
]
type FollowUpAnchorKind = Literal[
    "reported_effective_start", "reported_effective_change", "doctor_specified_date"
]


class TimingAnchor(_BoundaryValue):
    kind: AnchorKind
    instant: UtcInstant


def _ordered_window(start: datetime | None, end: datetime | None) -> None:
    if start is not None and end is not None and start > end:
        raise ValueError("window start must not follow its end")


class TestDetails(_BoundaryValue):
    kind: Literal["TEST"] = "TEST"
    analytes: Annotated[tuple[NonblankStr, ...], Field(min_length=1)]
    collection_window_start: UtcInstant | None = None
    collection_window_end: UtcInstant | None = None
    completeness: Literal["all", "any"]

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        _ordered_window(self.collection_window_start, self.collection_window_end)
        return self


class MonitorReading(_BoundaryValue):
    source_ref: VersionRef
    reading_index: NonnegativeInt
    observed_at: UtcInstant
    received_at: UtcInstant
    slot: NonnegativeInt | None
    value: NonblankStr


class MonitorDetails(_BoundaryValue):
    kind: Literal["MONITOR"] = "MONITOR"
    metric: NonblankStr
    unit: NonblankStr
    slots: Annotated[tuple[UtcInstant, ...], Field(min_length=1)]
    required_coverage: PositiveVersion
    readings: tuple[MonitorReading, ...] = ()


class MedicationDetails(_BoundaryValue):
    kind: Literal["MEDICATION"] = "MEDICATION"
    action: Literal["START", "STOP", "CHANGE"]
    order_ref: VersionRef


class SendRecordsDetails(_BoundaryValue):
    kind: Literal["SEND_RECORDS"] = "SEND_RECORDS"
    categories: Annotated[tuple[NonblankStr, ...], Field(min_length=1)]
    period_start: UtcInstant | None = None
    period_end: UtcInstant | None = None
    required_count: PositiveVersion

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        _ordered_window(self.period_start, self.period_end)
        return self


class VisitDetails(_BoundaryValue):
    kind: Literal["VISIT"] = "VISIT"
    objective: Literal["arrange", "booking_reported", "attendance_reported", "report_received"]
    window_start: UtcInstant | None = None
    window_end: UtcInstant | None = None

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        _ordered_window(self.window_start, self.window_end)
        return self


class QuestionDetails(_BoundaryValue):
    kind: Literal["QUESTION"] = "QUESTION"
    question_text: NonblankStr
    source_observation_ref: ObservationRef

    held_answer: str | None = Field(default=None, repr=False)
    held_answer_ready: StrictBool = False
    held_answer_by: str | None = None
    held_answer_amendment_ref: VersionRef | None = None
    held_answer_consumed_at: UtcInstant | None = None


class TaskDetails(_BoundaryValue):
    kind: Literal["TASK"] = "TASK"
    category: NonblankStr
    instruction: NonblankStr
    completion_rule: NonblankStr


type MissionDetails = Annotated[
    TestDetails
    | MonitorDetails
    | MedicationDetails
    | SendRecordsDetails
    | VisitDetails
    | QuestionDetails
    | TaskDetails,
    Field(discriminator="kind"),
]


class _Aggregate(_BoundaryValue):
    id: NonblankStr
    version: PositiveVersion = 1
    created_at: UtcInstant
    updated_at: UtcInstant
    last_work_generation: NonnegativeInt = 0
    work_clock: WorkClock | None = None

    @model_validator(mode="after")
    def validate_metadata(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.work_clock and self.work_clock.work_generation != self.last_work_generation:
            raise ValueError("last_work_generation must match the current work clock")
        return self

    def _validate_clock(self, terminal: bool, lane: str) -> None:
        if terminal != (self.work_clock is None):
            raise ValueError("terminal records forbid work_clock; unfinished records require it")
        if self.work_clock and self.work_clock.work_lane != lane:
            raise ValueError("work_clock has the wrong work_lane")


class DeadlineHistory(_BoundaryValue):
    due_at: UtcInstant
    due_source: DueSource
    due_reason: NonblankStr
    timing_anchor: TimingAnchor
    original_time_expression: NonblankStr | None
    timezone: IanaZone
    grace_seconds: NonnegativeInt
    escalation_at: UtcInstant
    policy_version: NonblankStr


class BarrierPlace(_BoundaryValue):
    """Only source-backed map attributes; no service/stock/price assertions."""

    source_id: NonblankStr
    name: str = Field(min_length=1, max_length=60)
    address: str | None = Field(default=None, max_length=100)
    distance_m: int = Field(ge=0, le=5000)
    opening_hours: str | None = Field(default=None, max_length=60)
    phone: str | None = Field(default=None, max_length=40)


class BarrierStep(_BoundaryValue):
    receipt_id: NonblankStr
    at: UtcInstant
    action: Literal["reason", "ask_patient", "find_places", "patient_reply", "finish"]
    outcome: NonblankStr


class BarrierAttempt(_BoundaryValue):
    doctor_id: NonblankStr
    patient_id: NonblankStr
    mission_id: NonblankStr
    sequence: PositiveVersion
    version: PositiveVersion = 1
    receipt_id: NonblankStr
    created_at: UtcInstant
    updated_at: UtcInstant
    expires_at: UtcInstant
    barrier_type: Literal[
        "cost", "availability", "forgot", "confusion", "side_effect_experience", "other"
    ]
    patient_words: tuple[NonblankStr, ...]
    receipt_ids: tuple[NonblankStr, ...]
    area: str | None = Field(default=None, max_length=120)
    area_receipt_id: NonblankStr | None = None
    requested_fact: Literal["area", "detail"] | None = None
    answered: bool = False
    reasoning_spent: int = Field(default=0, ge=0, le=1)
    questions_spent: int = Field(default=0, ge=0, le=1)
    searches_spent: int = Field(default=0, ge=0, le=2)
    phase: Literal["reserved", "chosen", "search_reserved", "complete"] = "reserved"
    choice: Literal["ask_patient", "find_places", "hand_to_doctor", "resume_chase"] | None = None
    question: str = Field(default="", max_length=180)
    steps: tuple[BarrierStep, ...] = ()
    places: tuple[BarrierPlace, ...] = Field(default=(), max_length=10)
    outcome: NonblankStr = "reasoning_reserved"
    state: Literal["resolved", "unresolved", "handed_to_doctor"] = "unresolved"

    @model_validator(mode="after")
    def validate_attempt(self) -> Self:
        if self.expires_at <= self.created_at or self.updated_at < self.created_at:
            raise ValueError("barrier_attempt_times")
        if (self.area is None) != (self.area_receipt_id is None):
            raise ValueError("area_source_required")
        if self.area_receipt_id and self.area_receipt_id not in self.receipt_ids:
            raise ValueError("area_source_outside_attempt")
        return self
class CoordinatorChoice(_BoundaryValue):
    """Presentation only, bound to the exact committed mission/contact version."""

    source_version: PositiveVersion
    slot_id: NonblankStr
    contact_at: UtcInstant
    window_end: UtcInstant
    template_id: NonblankStr
    move: NonblankStr
    fact_ids: tuple[NonblankStr, ...]
    bundle_digest: NonblankStr


class Mission(_Aggregate):
    entity_type: Literal["mission"] = "mission"
    doctor_id: NonblankStr
    patient_id: NonblankStr
    kind: MissionKind
    title: NonblankStr
    clinical_en: str | None = Field(default=None, max_length=120)
    details: MissionDetails
    objective_predicate: ObjectivePredicate
    order_refs: tuple[VersionRef, ...] = ()
    state: MissionState
    fulfillment_validity: FulfillmentValidity = FulfillmentValidity.not_fulfilled
    fulfillment_event_id: NonblankStr | None = None
    prior_fulfillment_event_ids: tuple[NonblankStr, ...] = ()
    fulfilled_at: UtcInstant | None = None
    objective_received_at: UtcInstant | None = None
    timeliness: Timeliness = Timeliness.undetermined
    evidence_refs: tuple[EvidenceRef, ...] = ()
    confirmed_at: UtcInstant | None = None
    confirmed_by: NonblankStr | None = None
    source_proposal_id: NonblankStr | None = None
    due_at: UtcInstant
    due_source: DueSource
    due_reason: NonblankStr
    timing_anchor: TimingAnchor
    original_time_expression: NonblankStr | None = None
    timezone: IanaZone
    grace_seconds: NonnegativeInt
    escalation_at: UtcInstant
    review_at: UtcInstant
    policy_version: NonblankStr
    resume_at: UtcInstant | None = None
    next_contact_at: UtcInstant | None = None
    timing_history: tuple[DeadlineHistory, ...] = ()
    deadline_generation: PositiveVersion = 1
    handled_deadline_generation: NonnegativeInt = 0
    last_patient_reply_at: UtcInstant | None = None
    first_chase_accepted_at: UtcInstant | None = None
    last_chase_accepted_at: UtcInstant | None = None
    contact_count: NonnegativeInt = 0
    unanswered_delivered_count: NonnegativeInt = 0
    evidence_request_count: NonnegativeInt = 0
    barrier_type: NonblankStr | None = None
    barrier_reason: NonblankStr | None = None
    danger_history: StrictBool = False
    latest_deadline_notice_event_id: NonblankStr | None = None
    cancellation_reason: NonblankStr | None = None
    barrier_attempts: tuple[BarrierAttempt, ...] = ()

    @model_validator(mode="after")
    def validate_barrier_attempts(self) -> Self:
        for index, attempt in enumerate(self.barrier_attempts, 1):
            if (attempt.doctor_id, attempt.patient_id, attempt.mission_id, attempt.sequence) != (
                self.doctor_id,
                self.patient_id,
                self.id,
                index,
            ):
                raise ValueError("barrier_attempt_scope_or_sequence")
        return self
    coordinator_choice: CoordinatorChoice | None = None

    @model_validator(mode="after")
    def validate_mission(self) -> Self:
        self._validate_clock(self.state in TERMINAL_STATES, "mission")
        if self.details.kind != self.kind:
            raise ValueError("mission kind and details kind must match")
        if (
            self.kind == MissionKind.MEDICATION
            and self.objective_predicate.kind != "patient_report"
        ):
            raise ValueError("MEDICATION requires a patient_report predicate")
        if self.kind == MissionKind.QUESTION and self.objective_predicate.kind != "doctor_answer":
            raise ValueError("QUESTION requires a doctor_answer predicate")
        if self.escalation_at != self.due_at + timedelta(seconds=self.grace_seconds):
            raise ValueError("escalation_at must equal due_at plus grace_seconds")
        if (self.confirmed_at is None) != (self.confirmed_by is None):
            raise ValueError("confirmation time and actor must be supplied together")
        if (
            self.state not in TERMINAL_STATES | {MissionState.proposed}
            and self.confirmed_at is None
        ):
            raise ValueError("active missions require confirmation")
        if self.state == MissionState.blocked and (
            self.resume_at is None or self.barrier_reason is None
        ):
            raise ValueError("blocked missions require resume_at and barrier_reason")
        if self.state == MissionState.fulfilled and (
            self.fulfilled_at is None
            or self.fulfillment_validity == FulfillmentValidity.not_fulfilled
        ):
            raise ValueError("fulfilled missions require fulfilled_at and fulfillment validity")
        if self.state in {MissionState.cancelled, MissionState.closed_unfulfilled} and (
            self.fulfillment_validity == FulfillmentValidity.valid
        ):
            raise ValueError(
                "cancelled/closed_unfulfilled missions cannot assert valid fulfillment"
            )
        if self.handled_deadline_generation > self.deadline_generation:
            raise ValueError("handled deadline generation cannot exceed the current generation")
        return self


class FollowUpKind(StrEnum):
    MEDICATION_DAY3 = "MEDICATION_DAY3"
    CLINICAL_CHECKIN = "CLINICAL_CHECKIN"


class FollowUpState(StrEnum):
    awaiting_anchor = "awaiting_anchor"
    scheduled = "scheduled"
    waiting_response = "waiting_response"
    fulfilled = "fulfilled"
    overdue = "overdue"
    contact_suppressed = "contact_suppressed"
    cancelled = "cancelled"


class FollowUpTask(_Aggregate):
    entity_type: Literal["followup"] = "followup"
    doctor_id: NonblankStr
    patient_id: NonblankStr
    kind: FollowUpKind
    parent_mission_id: NonblankStr
    order_refs: tuple[VersionRef, ...] = ()
    confirmed_by: NonblankStr
    confirmed_at: UtcInstant
    anchor_kind: FollowUpAnchorKind
    anchor_time: UtcInstant | None = None
    anchor_source_ref: ObservationRef | None = None
    prompt_at: UtcInstant | None = None
    due_at: UtcInstant | None = None
    review_at: UtcInstant
    response_predicate: ObjectivePredicate
    source_report_ids: tuple[NonblankStr, ...] = ()
    state: FollowUpState
    suppression_reason: NonblankStr | None = None
    consent_slot_id: NonblankStr | None = None
    review_obligation_id: NonblankStr | None = None
    done_intent_id: NonblankStr | None = None
    deadline_handled: StrictBool = False
    timeliness: Timeliness = Timeliness.undetermined

    @model_validator(mode="after")
    def validate_followup(self) -> Self:
        self._validate_clock(
            self.state in {FollowUpState.fulfilled, FollowUpState.cancelled}, "followup"
        )
        if (self.prompt_at is None) != (self.due_at is None):
            raise ValueError("prompt_at and due_at must be supplied together")
        # A20 explicitly permits the unknown-anchor deadline to become overdue.
        unknown_anchor_overdue = (
            self.state == FollowUpState.overdue
            and self.deadline_handled
            and self.anchor_time is None
        )
        if (
            self.prompt_at is None
            and self.state
            not in {
                FollowUpState.awaiting_anchor,
                FollowUpState.contact_suppressed,
                FollowUpState.cancelled,
            }
            and not unknown_anchor_overdue
        ):
            raise ValueError("this follow-up state requires prompt_at and due_at")
        _ordered_window(self.prompt_at, self.due_at)
        return self


class ReviewKind(StrEnum):
    result_review = "result_review"
    correction_disposition = "correction_disposition"
    incident_response = "incident_response"
    unmet_objective = "unmet_objective"
    question_answer = "question_answer"
    media_failure = "media_failure"
    delivery_failure = "delivery_failure"
    binding_review = "binding_review"
    coverage_review = "coverage_review"
    followup_disposition = "followup_disposition"
    evidence_association = "evidence_association"
    intake_clarification = "intake_clarification"


class ReviewAction(StrEnum):
    review = "review"
    answer = "answer"
    extend = "extend"
    close = "close"
    cancel = "cancel"
    resolve_incident = "resolve_incident"
    associate = "associate"
    clarify = "clarify"
    dispose = "dispose"
    restore_coverage = "restore_coverage"


class ReviewState(StrEnum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"


class CoverageStatus(StrEnum):
    covered = "covered"
    blocked = "blocked"


def review_source_key(
    doctor_id: str,
    source_type: str,
    source_id: str,
    source_version: int,
    review_kind: ReviewKind,
) -> str:
    """Unambiguous tuple encoding; delimiters inside opaque IDs cannot collide."""
    return json.dumps(
        [doctor_id, source_type, source_id, source_version, review_kind],
        ensure_ascii=True,
        separators=(",", ":"),
    )


class ReviewObligation(_Aggregate):
    entity_type: Literal["review"] = "review"
    source_type: NonblankStr
    source_id: NonblankStr
    source_version: PositiveVersion
    review_kind: ReviewKind
    unique_source_key: NonblankStr
    owner_doctor_id: NonblankStr
    patient_id: NonblankStr | None = None
    source_mission_id: NonblankStr | None = None
    state: ReviewState = ReviewState.open
    review_at: UtcInstant
    coverage_status: CoverageStatus = CoverageStatus.covered
    acknowledged_by: NonblankStr | None = None
    acknowledged_at: UtcInstant | None = None
    resolved_by: NonblankStr | None = None
    resolved_at: UtcInstant | None = None
    resolved_reason: NonblankStr | None = None
    resolved_action_event_id: NonblankStr | None = None
    first_notice_at: UtcInstant | None = None
    last_material_change_version: PositiveVersion

    @property
    def next_action_at(self) -> datetime | None:
        return self.work_clock.next_action_at if self.work_clock else None

    @model_validator(mode="after")
    def validate_review(self) -> Self:
        self._validate_clock(self.state == ReviewState.resolved, "review")
        if self.unique_source_key != review_source_key(
            self.owner_doctor_id,
            self.source_type,
            self.source_id,
            self.source_version,
            self.review_kind,
        ):
            raise ValueError("unique_source_key does not match the review source")
        if (self.acknowledged_at is None) != (self.acknowledged_by is None):
            raise ValueError("acknowledgment time and actor must be supplied together")
        if self.state == ReviewState.acknowledged and self.acknowledged_at is None:
            raise ValueError("acknowledged review requires acknowledgment metadata")
        if self.state == ReviewState.resolved and any(
            value is None
            for value in (
                self.resolved_by,
                self.resolved_at,
                self.resolved_reason,
                self.resolved_action_event_id,
            )
        ):
            raise ValueError("resolved review requires explicit resolution metadata")
        return self


class DefaultDeadline(_BoundaryValue):
    offset: Annotated[timedelta, Field(gt=timedelta())]
    relative_to: Literal["anchor", "schedule_end"]


class DoctorTimingPolicy(_BoundaryValue):
    """All numerical defaults live in the single draft instance below."""

    policy_version: NonblankStr
    timezone: IanaZone
    default_deadlines: dict[MissionKind, DefaultDeadline]
    # Contract 11b draft policy: OWNER_REVIEW_PENDING.
    default_deadline_local_time: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")] = (
        "10:00"
    )
    default_grace_seconds: NonnegativeInt
    inferred_min_days: NonnegativeInt
    inferred_max_days: PositiveVersion
    draft_review_interval: Annotated[timedelta, Field(gt=timedelta())]
    result_review_interval: Annotated[timedelta, Field(gt=timedelta())]
    pause_max_interval: Annotated[timedelta, Field(gt=timedelta())]
    followup_response_window: Annotated[timedelta, Field(gt=timedelta())]
    medication_day3_offset: Annotated[timedelta, Field(gt=timedelta())]
    explicit_horizon: Annotated[timedelta, Field(gt=timedelta())]
    overdue_review_interval: Annotated[timedelta, Field(gt=timedelta())]
    material_change_review_interval: Annotated[timedelta, Field(gt=timedelta())]

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if set(self.default_deadlines) != set(MissionKind):
            raise ValueError("default_deadlines must cover every mission kind")
        if self.inferred_min_days > self.inferred_max_days:
            raise ValueError("inferred bounds are reversed")
        if self.default_deadlines[MissionKind.QUESTION].relative_to != "anchor":
            raise ValueError("QUESTION default must be relative to its creation anchor")
        return self


DRAFT_POLICY_2026_09 = DoctorTimingPolicy(
    policy_version="draft-2026-09",
    timezone="Africa/Cairo",
    default_deadlines={
        MissionKind.TEST: DefaultDeadline(offset=timedelta(days=14), relative_to="anchor"),
        MissionKind.MONITOR: DefaultDeadline(offset=timedelta(days=1), relative_to="schedule_end"),
        MissionKind.MEDICATION: DefaultDeadline(offset=timedelta(days=3), relative_to="anchor"),
        MissionKind.SEND_RECORDS: DefaultDeadline(offset=timedelta(days=3), relative_to="anchor"),
        MissionKind.VISIT: DefaultDeadline(offset=timedelta(days=30), relative_to="anchor"),
        MissionKind.QUESTION: DefaultDeadline(offset=timedelta(hours=48), relative_to="anchor"),
        MissionKind.TASK: DefaultDeadline(offset=timedelta(days=7), relative_to="anchor"),
    },
    default_grace_seconds=0,
    inferred_min_days=1,
    inferred_max_days=180,
    draft_review_interval=timedelta(days=2),
    result_review_interval=timedelta(days=3),
    pause_max_interval=timedelta(days=14),
    followup_response_window=timedelta(days=2),
    medication_day3_offset=timedelta(days=3),
    explicit_horizon=timedelta(days=365),
    overdue_review_interval=timedelta(days=7),
    material_change_review_interval=timedelta(days=3),
)
