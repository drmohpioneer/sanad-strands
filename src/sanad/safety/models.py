"""Safety boundary values, not clinical acceptance or diagnosis.

Unknown is never normal; a missing protocol is not a safe result. Screening
covers readable text and captions only, never hidden unprocessed media content;
the caller owns media_failure. The original cardiology cohort awaits v2 approval.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, StrictBool, StrictStr, TypeAdapter, model_validator

from sanad.domain import NonblankStr, ObservationRef, TextSpan, VersionRef
from sanad.domain.boundaries import _BoundaryValue
from sanad.domain.language import default_language

type RuleFamily = Literal["phrase", "concept", "vital", "lab"]
type Severity = Literal["danger", "concern"]
type FiniteNumber = Annotated[float, Field(allow_inf_nan=False)]
type Reason = Literal[
    "unknown_unit",
    "unknown_value",
    "not_in_table",
    "missing_cutoff",
    "missing_baseline",
    "missing_second_fact",
    "slip_flag",
    "threshold",
    "implausible",
    "resolved_tense",
    "no_rule_matched",
    "within_table",
    "missing_protocol",
    "bounded_value",
]


class Quantity(_BoundaryValue):
    """Raw printed quantity; optional upstream normalization confers no authority.

    The kernel recomputes conversions from raw_value/raw_unit and the copied table.
    An explicit empty unit denotes a dimensionless result; None means missing.
    """

    raw_value: StrictStr
    raw_unit: StrictStr | None = None
    normalized_value: FiniteNumber | None = None
    normalized_unit: StrictStr | None = None
    conversion_rule_id: NonblankStr | None = None

    @model_validator(mode="after")
    def paired_normalization(self) -> Self:
        if (self.normalized_value is None) != (self.normalized_unit is None):
            raise ValueError("normalized value and unit must be supplied together")
        if self.conversion_rule_id is not None and self.normalized_value is None:
            raise ValueError("conversion rule requires a normalized quantity")
        return self


class LabCandidate(_BoundaryValue):
    analyte_raw: NonblankStr
    value: Quantity
    slip_flag: StrictStr | None = None
    slip_cutoff: Quantity | None = None


class ScreenVerdict(_BoundaryValue):
    kind: Literal["screen"] = "screen"
    level: Literal["none", "concern", "danger"]
    concept: NonblankStr | None = None
    rule_family: RuleFamily
    rule_id: NonblankStr
    matched_span: TextSpan | None = None
    reason: Reason
    policy_version: NonblankStr


class VitalVerdict(_BoundaryValue):
    kind: Literal["vital"] = "vital"
    level: Literal["crisis", "low", "normal", "implausible"]
    systolic: Annotated[int, Field(strict=True)]
    diastolic: Annotated[int, Field(strict=True)]
    concept: NonblankStr | None = None
    decided_by: NonblankStr
    thresholds_version: NonblankStr
    policy_version: NonblankStr
    rule_family: Literal["vital"] = "vital"
    rule_id: NonblankStr


class LabVerdict(_BoundaryValue):
    kind: Literal["lab"] = "lab"
    level: Literal["critical", "flagged", "normal", "cannot_judge", "not_in_table"]
    analyte_canonical: NonblankStr
    rule_id: NonblankStr | None
    note: NonblankStr
    needs_second_fact: StrictBool = False
    reason: Reason
    policy_version: NonblankStr
    rule_family: Literal["lab"] = "lab"


class OrderSummary(_BoundaryValue):
    """Caller-supplied active order names; the caller revalidates order authority."""

    order_ref: VersionRef
    drug_names: tuple[NonblankStr, ...]


class OutputContext(_BoundaryValue):
    active_orders: tuple[OrderSummary, ...] = ()
    allowed_numbers: tuple[NonblankStr, ...] = ()
    mode: Literal["plan_explanation", "general_education", "safety_response", "barrier_help"]
    language: Literal["ar", "en"] = default_language
    doctor_notified: StrictBool = False


type ViolationReason = Literal[
    "unsupported_reassurance",
    "drug_not_in_active_orders",
    "unsupported_clinical_number",
    "imperative_dose_or_frequency",
    "treatment_change",
    "wrong_emergency_number",
    "unsupported_doctor_awareness",
]


class Violation(_BoundaryValue):
    reason: ViolationReason
    detail: NonblankStr


class ValidationVerdict(_BoundaryValue):
    ok: StrictBool
    violations: tuple[Violation, ...]
    policy_version: NonblankStr

    @model_validator(mode="after")
    def consistent_result(self) -> Self:
        if self.ok != (not self.violations):
            raise ValueError("ok must reflect every violation")
        return self


type IncidentVerdict = Annotated[
    ScreenVerdict | VitalVerdict | LabVerdict, Field(discriminator="kind")
]


class IncidentFacts(_BoundaryValue):
    source: ObservationRef
    unique_source_key: NonblankStr
    rule_family: RuleFamily
    rule_id: NonblankStr
    policy_version: NonblankStr
    verdict: IncidentVerdict
    context: dict[str, JsonValue] = Field(default_factory=dict)
    patient_alerts: tuple[dict[str, JsonValue], ...] = ()
    observed_row: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent_source(self) -> Self:
        if (
            self.unique_source_key
            != f"{self.source.observation_id}#{self.rule_family}#{self.rule_id}"
        ):
            raise ValueError("incident source key must match source and rule")
        if self.verdict.policy_version != self.policy_version:
            raise ValueError("incident and verdict policy versions must match")
        if self.verdict.rule_family != self.rule_family:
            raise ValueError("incident and verdict rule families must match")
        return self

    def as_payload(self) -> dict[str, JsonValue]:
        """The exact JSON dictionary accepted by slice 03's raise_incident."""
        return TypeAdapter(dict[str, JsonValue]).validate_python(self.model_dump(mode="json"))
