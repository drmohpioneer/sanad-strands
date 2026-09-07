"""Pure confirmed-predicate evaluation; dates never imply a new prescription."""

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from sanad.domain import Mission
from sanad.domain.entities import SendRecordsDetails, TestDetails, VisitDetails
from sanad.domain.predicates import PredicateResult
from sanad.evidence.classify import analyte, category_alias, expected
from sanad.evidence.grading import meaningful_disagreements
from sanad.safety.alerts import quantity
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY
from sanad.scribe.extract import LabRowCandidate
from sanad.store.records import Evidence


def material_disagreement(candidate: Evidence) -> bool:
    fields = {"document_type", "printed_date", "unreadable"}
    if candidate.category in {"lab_result", "monitor_screen"}:
        return any(
            d.field in fields or d.field.endswith((".name", ".value", ".unit"))
            for d in meaningful_disagreements(candidate.disagreements)
        )
    return any(d.field in fields for d in candidate.disagreements)


def evaluate(
    mission: Mission,
    candidate: Evidence,
    accepted_evidence_for_mission: Sequence[Evidence],
    now: datetime,
) -> PredicateResult:
    def result(missing: tuple[str, ...], detail: str) -> PredicateResult:
        return PredicateResult(
            satisfied=not missing, missing=missing, detail=detail, evaluated_at=now
        )

    if (
        candidate.scope.doctor_id != mission.doctor_id
        or candidate.scope.patient_id != mission.patient_id
    ):
        return result(("patient_scope",), "Evidence belongs to another patient.")
    predicate = mission.objective_predicate
    if predicate.kind != "evidence":
        return result(("patient_report",), "This objective requires its specific patient report.")
    if "one_document_per_photo" in candidate.flags:
        return result(("one_document_per_photo",), "Photograph each document separately.")
    if (
        "identity_mismatch" in candidate.flags
        and candidate.patient_match_provenance != "doctor_choice"
    ):
        return result(("identity",), "Printed name requires the doctor's association decision.")
    if any(r.unreadable for r in candidate.readers) or not candidate.extracted_values:
        return result(("readable_document",), "The document is not readable.")
    if "document_instructions" in candidate.flags:
        return result(("verification",), "Document instructions remain untrusted observations.")
    if candidate.shift_guard_fired or material_disagreement(candidate):
        return result(
            ("verification",), "Reader disagreement or shifted rows require verification."
        )
    if candidate.category not in expected(mission):
        return result(("category",), "Document category does not meet the confirmed objective.")
    if predicate.evaluator == "monitor":
        return result(
            ("slot_assignment",), "Reading retained; slot assignment belongs to slice 13."
        )
    if predicate.evaluator == "task_evidence":
        return result(
            () if "doctor_accepted" in candidate.flags else ("doctor_acceptance",),
            "The doctor must explicitly accept the evidence for this task.",
        )
    if predicate.evaluator == "visit":
        if (
            not isinstance(mission.details, VisitDetails)
            or mission.details.objective != "report_received"
        ):
            return result(("patient_report",), "Booking and attendance require their own reports.")
        return result((), "The requested visit report was received; no clinical judgment.")

    documents = {
        e.evidence_id: e
        for e in accepted_evidence_for_mission
        if e.scope == candidate.scope
        and e.mission_id == mission.id
        and e.association_state in {"accepted", "accepted_pending_identity"}
        and e.evidence_id != candidate.evidence_id
        and not e.shift_guard_fired
        and not material_disagreement(e)
        and "document_instructions" not in e.flags
        and not any(r.unreadable for r in e.readers)
        and "one_document_per_photo" not in e.flags
    }
    documents[candidate.evidence_id] = candidate
    details = mission.details
    start = end = None
    if isinstance(details, TestDetails):
        start, end = details.collection_window_start, details.collection_window_end
    elif isinstance(details, SendRecordsDetails):
        start, end = details.period_start, details.period_end
    zone = ZoneInfo(mission.timezone)

    def date_missing(e: Evidence) -> str | None:
        if not start and not end:
            return None
        if not e.printed_date:
            return "printed_date"
        if (start and e.printed_date < start.astimezone(zone).date()) or (
            end and e.printed_date > end.astimezone(zone).date()
        ):
            return "collection_date" if isinstance(details, TestDetails) else "period"
        return None

    missing_date = date_missing(candidate)
    if missing_date:
        return result(
            (missing_date,),
            f"Printed date {candidate.printed_date or 'unknown'} "
            "does not establish the requested window.",
        )
    valid = [e for e in documents.values() if not date_missing(e)]
    if isinstance(details, TestDetails) and predicate.evaluator == "test":
        wanted = tuple(dict.fromkeys(analyte(v) for v in details.analytes))
        covered = {
            analyte(row.analyte)
            for e in valid
            if e.category == "lab_result"
            for row in e.extracted_values
            if isinstance(row, LabRowCandidate)
            and row.unit
            and row.verdict
            and row.value
            and quantity(row.value, row.unit, analyte(row.analyte), POLICY) is not None
        }
        missing = tuple(v for v in wanted if v not in covered)
        if details.completeness == "any" and covered.intersection(wanted):
            missing = ()
        return result(
            missing, "Confirmed analyte coverage with explicit supported units and dates."
        )
    if isinstance(details, SendRecordsDetails) and predicate.evaluator == "send_records":
        wanted_categories = tuple(category_alias(v) for v in details.categories)
        accepted = {e.content_hash: e for e in valid if e.category in wanted_categories}
        missing = tuple(
            f"category:{original}"
            for original, category in zip(details.categories, wanted_categories, strict=True)
            if category is None or category not in {e.category for e in accepted.values()}
        )
        if len(accepted) < details.required_count:
            missing += (f"documents:{details.required_count - len(accepted)}",)
        return result(
            missing,
            "Distinct historical documents meet category, period and count; "
            "active orders are unchanged.",
        )
    return result(("supported_predicate",), "The mission details do not match this evaluator.")


def completing_set(
    mission: Mission,
    candidate: Evidence,
    accepted_evidence_for_mission: Sequence[Evidence],
    now: datetime,
) -> tuple[Evidence, ...]:
    """Retain only needed receipts, preferring the earliest completing set.

    The predicate is monotone over eligible prior documents. Removing the latest
    dispensable receipts cannot invent coverage or move an actual required receipt.
    All observations remain stored regardless of membership in this source set.
    """
    prior = [e for e in accepted_evidence_for_mission if e.evidence_id != candidate.evidence_id]
    if not evaluate(mission, candidate, prior, now).satisfied:
        raise ValueError("evidence_set_incomplete")
    for evidence in sorted(
        prior, key=lambda e: (e.provenance.received_at, e.evidence_id), reverse=True
    ):
        smaller = [e for e in prior if e.evidence_id != evidence.evidence_id]
        if evaluate(mission, candidate, smaller, now).satisfied:
            prior = smaller
    return tuple(
        sorted((*prior, candidate), key=lambda e: (e.provenance.received_at, e.evidence_id))
    )
