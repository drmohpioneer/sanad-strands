"""Typed self-reports and single-use patient choices; no authority from models."""

from typing import Literal

from pydantic import Field

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
