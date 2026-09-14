"""Typed self-reports and single-use patient choices; no authority from models."""

from datetime import date
from typing import Literal

from pydantic import Field, StrictBool

from sanad.concierge.policy import BarrierType
from sanad.domain import PatientScope, VersionRef
from sanad.domain.boundaries import NonblankStr, PositiveVersion, UtcInstant, _BoundaryValue


class Reading(_BoundaryValue):
    analyte: NonblankStr
    quoted: NonblankStr
    raw_value: NonblankStr
    raw_unit: str | None = None
    judgment: Literal["reported", "cannot_judge", "normal", "flagged", "critical", "implausible"]
    rule_id: str | None = None


class ReportFactPayload(_BoundaryValue):
    report_kind: Literal[
        "medication_start",
        "day3",
        "reading",
        "question_attachment",
        "medication_stop",
        "medication_change",
        "start_date",
        "barrier",
        "visit_booking",
        "visit_attendance",
        "visit_report_pending",
        "task_done",
    ]
    text: NonblankStr
    target_ref: VersionRef | None = None
    readings: tuple[Reading, ...] = ()
    barrier_type: (
        Literal["cost", "availability", "forgot", "confusion", "side_effect_experience", "other"]
        | None
    ) = None
    effective_start: UtcInstant | None = None
    anchor_unknown: bool = False
    detail: str | None = None
    original_receipt_id: str | None = None


class ScheduleOffer(_BoundaryValue):
    times: tuple[str, ...] = Field(min_length=1, max_length=4)
    effective_date: date


class ScheduleTime(_BoundaryValue):
    value: str
    quote: str = Field(min_length=1, max_length=256, repr=False)


class ScheduleReading(_BoundaryValue):
    mission_id: str | None = None
    times: tuple[ScheduleTime, ...] = Field(default=(), max_length=4)
    asserted: StrictBool = False


class PatientAction(_BoundaryValue):
    entity_type: Literal["patient_action"] = "patient_action"
    id: NonblankStr
    scope: PatientScope
    version: PositiveVersion = 1
    created_at: UtcInstant
    updated_at: UtcInstant
    expires_at: UtcInstant
    actor_subject: NonblankStr = Field(repr=False)
    action: Literal[
        "resume",
        "start",
        "quiet_slot",
        "evidence_choose",
        "evidence_yes",
        "evidence_no",
        "evidence_other",
        "visit_report",
        "task_report",
        "barrier_category",
        "barrier_target",
        "schedule_yes",
        "schedule_no",
        "schedule_choose",
    ]
    evidence_id: str | None = None
    evidence_version: PositiveVersion | None = None
    target_ref: VersionRef | None = None
    slot_id: str | None = None
    source_receipt_id: NonblankStr
    delivery_epoch: int
    binding_epoch: int
    consent_version: int
    consumed_at: UtcInstant | None = None
    medication_report_text: NonblankStr | None = Field(default=None, repr=False)
    report_text: str | None = Field(default=None, repr=False)
    monitor_reading_text: NonblankStr | None = Field(default=None, repr=False)
    monitor_observed_at: UtcInstant | None = None
    monitor_received_at: UtcInstant | None = None
    barrier_category: BarrierType | None = None
    choice_number: int | None = None
    schedule: ScheduleOffer | None = None
    quiet_schedule_generation: PositiveVersion | None = None


class ProblemReading(_BoundaryValue):
    category: BarrierType | Literal["uncertain"]
    quote: str = Field(max_length=4096, repr=False)
    asserted: StrictBool
    subject: Literal["patient", "someone_else"]


class BarrierReading(_BoundaryValue):
    problems: tuple[ProblemReading, ...] = Field(max_length=3)


class BarrierCitation(_BoundaryValue):
    quote: str = Field(repr=False)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    asserted: Literal[True] = True
    subject: Literal["patient"] = "patient"


class BarrierOutcome(_BoundaryValue):
    status: Literal["accepted", "none", "uncertain", "failure"]
    category: BarrierType | None = None
    citations: tuple[BarrierCitation, ...] = ()
    readers: tuple[BarrierReading, ...] = Field(default=(), repr=False)
    model_ids: tuple[str, ...] = ()
    prompt_version: str = "barrier-meaning-v1"
    provenance: Literal["model", "patient_choice"] = "model"
    choice_id: str | None = None
    source_receipt_id: str | None = None


class BarrierReservation(_BoundaryValue):
    attempts: int = Field(ge=1, le=2)
    text: str = Field(repr=False)
    text_version: str
    transcript_ref: str | None = None
