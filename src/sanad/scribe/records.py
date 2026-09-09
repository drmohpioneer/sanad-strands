"""Accepted immutable clinical records; operational order authority is separate."""

from typing import Literal, Self

from pydantic import ConfigDict, model_validator

from sanad.concierge.records import ReportFactPayload
from sanad.domain import PatientScope, Provenance, VersionRef
from sanad.domain.boundaries import NonblankStr, PositiveVersion, UtcInstant, _BoundaryValue
from sanad.scribe.extract import (
    ClinicalKind,
    FactTerm,
    LabRowCandidate,
    OrderCandidate,
    ProposalIssue,
)
from sanad.scribe.proposal import ScribeRecord


class FactPayload(_BoundaryValue):
    text: NonblankStr
    clinical_en: str | None = None
    clinical_kind: ClinicalKind = "History"
    terms: tuple[FactTerm, ...] = ()
    tags: tuple[Literal["alert_text"], ...] = ()


class LabFactPayload(LabRowCandidate):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text: NonblankStr


class ClinicalFact(ScribeRecord):
    entity_type: Literal["clinical_fact"] = "clinical_fact"
    scope: PatientScope
    category: Literal[
        "condition",
        "allergy",
        "history",
        "medication_history",
        "demographic",
        "patient_report",
        "finding",
        "complaint",
    ]
    payload: FactPayload | LabFactPayload | ReportFactPayload
    provenance: Provenance
    visibility: Literal["doctor_private", "patient_released"] = "doctor_private"
    supersedes_fact_id: str | None = None
    correction_id: str | None = None
    root_fact_id: str | None = None


class ValueAlert(_BoundaryValue):
    text: NonblankStr
    metric: NonblankStr
    comparator: Literal["gt", "ge", "lt", "le"]
    threshold: NonblankStr
    unit: str | None = None
    floor_mode: Literal["add_only"] = "add_only"


class CareOrderVersion(ScribeRecord):
    entity_type: Literal["care_order_version"] = "care_order_version"
    scope: PatientScope
    order_id: NonblankStr
    order_version: PositiveVersion
    type: Literal["medication", "value_alert"]
    structured_instruction: OrderCandidate | ValueAlert
    provenance: Provenance
    confirmed_by: NonblankStr
    confirmed_at: UtcInstant
    effective_from: UtcInstant | None = None
    effective_to: UtcInstant | None = None
    supersedes_version: PositiveVersion | None = None

    @model_validator(mode="after")
    def instruction_type(self) -> Self:
        if (self.type == "medication") != isinstance(self.structured_instruction, OrderCandidate):
            raise ValueError("order type must match its structured instruction")
        if self.id != f"{self.order_id}:{self.order_version}":
            raise ValueError("immutable order identity must name its clinical version")
        return self


class CareOrderHead(ScribeRecord):
    entity_type: Literal["care_order_head"] = "care_order_head"
    scope: PatientScope
    order_id: NonblankStr
    current_order_version: PositiveVersion
    current_version_id: NonblankStr
    type: Literal["medication", "value_alert"]
    name: NonblankStr
    status: Literal["active", "stopped", "superseded"]
    delivery_epoch: int = 0
    changed_by: NonblankStr
    changed_at: UtcInstant


class CarePlan(ScribeRecord):
    entity_type: Literal["care_plan"] = "care_plan"
    scope: PatientScope
    plan_id: NonblankStr
    plan_version: PositiveVersion = 1
    order_refs: tuple[VersionRef, ...] = ()
    mission_ids: tuple[str, ...] = ()
    followup_ids: tuple[str, ...] = ()
    status: Literal["confirmed"] = "confirmed"
    confirmed_by: NonblankStr
    confirmed_at: UtcInstant
    source_proposal_id: NonblankStr
    accepted_items: tuple[str, ...]
    blocked_items: tuple[ProposalIssue, ...]
