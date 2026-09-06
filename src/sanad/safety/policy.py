"""Explicit, immutable policy for the original adult cardiology cohort.

Unknown is never normal; a missing protocol is not a safe result. Screening
covers readable text/captions, never hidden content of unprocessed media (the
caller's media_failure route). A verdict is not a diagnosis. V2 approval pending.
"""

from dataclasses import asdict
from datetime import date
from typing import Annotated, Self

from pydantic import Field, StrictBool, StrictStr, model_validator

from sanad.domain import NonblankStr
from sanad.domain.boundaries import _BoundaryValue
from sanad.safety import labs, validator, vitals
from sanad.safety.models import FiniteNumber

PHRASE_TABLE_VERSION = "google-b65f569-phrases"
CONCEPT_RULES_VERSION = "google-b65f569-concepts"
VALIDATOR_LEXICON_VERSION = "google-b65f569-validator"


class BpThresholds(_BoundaryValue):
    systolic_crisis: Annotated[int, Field(strict=True, gt=0)]
    diastolic_crisis: Annotated[int, Field(strict=True, gt=0)]
    systolic_low: Annotated[int, Field(strict=True, gt=0)]
    plausible_systolic: tuple[int, int]
    plausible_diastolic: tuple[int, int]
    upper_bounds_exclusive: StrictBool
    thresholds_version: NonblankStr

    @model_validator(mode="after")
    def valid_ranges(self) -> Self:
        for low, high in (self.plausible_systolic, self.plausible_diastolic):
            if not 0 < low < high:
                raise ValueError("plausibility bounds must be positive and ordered")
        if (
            not self.plausible_systolic[0]
            <= self.systolic_low
            < self.systolic_crisis
            < self.plausible_systolic[1]
        ):
            raise ValueError("systolic thresholds must lie within plausibility bounds")
        if not self.plausible_diastolic[0] < self.diastolic_crisis < self.plausible_diastolic[1]:
            raise ValueError("diastolic crisis threshold must lie within plausibility bounds")
        return self


class LabRule(_BoundaryValue):
    analyte: NonblankStr
    unit: StrictStr
    low: FiniteNumber | None = None
    high: FiniteNumber | None = None
    needs_slip_cutoff: StrictBool = False
    baseline_multiple: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    two_factor: StrictBool = False
    note: StrictStr = ""

    @model_validator(mode="after")
    def valid_bounds(self) -> Self:
        if self.low is not None and self.high is not None and self.low >= self.high:
            raise ValueError("lab lower threshold must precede upper threshold")
        if self.two_factor and not self.needs_slip_cutoff:
            raise ValueError("two-factor rules require a slip-based first fact")
        return self


class SafetyPolicy(_BoundaryValue):
    policy_version: NonblankStr
    cohort: NonblankStr
    approved_by: NonblankStr
    approved_on: date
    emergency_number: Annotated[str, Field(strict=True, pattern=r"^[0-9]+$")] = "123"
    bp_thresholds: BpThresholds
    lab_rules: tuple[LabRule, ...]
    phrase_table_version: NonblankStr
    concept_rules_version: NonblankStr
    validator_lexicon_version: NonblankStr
    notes: tuple[NonblankStr, ...]

    @model_validator(mode="after")
    def unique_rules(self) -> Self:
        keys = [labs._key(rule.analyte) for rule in self.lab_rules]
        if len(keys) != len(set(keys)):
            raise ValueError("lab rules must have unique analytes")
        return self

    def require_supported_tables(self) -> None:
        if (
            self.phrase_table_version != PHRASE_TABLE_VERSION
            or self.concept_rules_version != CONCEPT_RULES_VERSION
            or self.validator_lexicon_version != VALIDATOR_LEXICON_VERSION
        ):
            raise ValueError("missing protocol: this policy names unsupported safety tables")


SAFETY_POLICY_V1_CARDIOLOGY_DRAFT = SafetyPolicy(
    policy_version="safety-v1-cardiology-draft-2026-09-06",
    approved_by="Clinical approver of record (original Sanad build)",
    approved_on=date(2026, 8, 29),
    cohort="adult cardiology outpatients, original Sanad build; re-approval for Sanad v2 pending",
    emergency_number=validator.AMBULANCE,
    bp_thresholds=BpThresholds(
        systolic_crisis=vitals.SYSTOLIC_CRISIS,
        diastolic_crisis=vitals.DIASTOLIC_CRISIS,
        systolic_low=vitals.SYSTOLIC_LOW,
        plausible_systolic=vitals.PLAUSIBLE_SYSTOLIC,
        plausible_diastolic=vitals.PLAUSIBLE_DIASTOLIC,
        upper_bounds_exclusive=True,
        thresholds_version="google-b65f569-bp-contract04-bounds",
    ),
    lab_rules=tuple(LabRule.model_validate(asdict(rule)) for rule in labs.CRITICAL_LABS),
    phrase_table_version=PHRASE_TABLE_VERSION,
    concept_rules_version=CONCEPT_RULES_VERSION,
    validator_lexicon_version=VALIDATOR_LEXICON_VERSION,
    notes=(
        "All copied thresholds and conversions are unchanged; no clinical re-approval is implied.",
        "Contract 04 treats the copied upper plausibility bounds as exclusive "
        "(300/200 is implausible).",
        "Missing units, cutoffs and protocols never establish normality; normal means "
        "no table threshold hit, not clinical clearance.",
        "The source contains other cohort rows; retention is provenance, "
        "not approval to activate care for those cohorts.",
    ),
)
