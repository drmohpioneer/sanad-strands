"""Confirmed objective specifications; document evaluation belongs to executors."""

from typing import Annotated, Literal

from pydantic import Field, StrictBool

from sanad.domain.boundaries import NonblankStr, UtcInstant, _BoundaryValue


class PatientReportPredicate(_BoundaryValue):
    kind: Literal["patient_report"] = "patient_report"
    report_kind: NonblankStr


class DoctorAnswerPredicate(_BoundaryValue):
    kind: Literal["doctor_answer"] = "doctor_answer"


class EvidencePredicate(_BoundaryValue):
    kind: Literal["evidence"] = "evidence"
    evaluator: Literal["test", "monitor", "send_records", "visit", "task_evidence"]


type ObjectivePredicate = Annotated[
    PatientReportPredicate | DoctorAnswerPredicate | EvidencePredicate,
    Field(discriminator="kind"),
]


class PredicateResult(_BoundaryValue):
    satisfied: StrictBool
    missing: tuple[NonblankStr, ...] = ()
    detail: NonblankStr
    evaluated_at: UtcInstant
