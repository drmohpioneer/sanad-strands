"""Hand-written evaluator and classification specifications for contract 12."""

from datetime import date, timedelta
from typing import cast

from domain_fixtures import NOW, mission

from sanad.domain import EvidencePredicate, Mission, PatientScope, Provenance
from sanad.domain.entities import (
    MonitorDetails,
    SendRecordsDetails,
    TaskDetails,
    TestDetails,
    VisitDetails,
)
from sanad.media.vision import (
    DocumentItem,
    DocumentRead,
    ItemRead,
    PrintedIdentityHint,
    ReaderResult,
    diff,
)
from sanad.models.io import CallMetadata
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY
from sanad.scribe.crosscheck import grade_row
from sanad.scribe.extract import LabRowCandidate
from sanad.store import keys
from sanad.store.records import Evidence


def read(
    kind: str = "lab", items: tuple[DocumentItem, ...] | None = None, **changes: object
) -> DocumentRead:
    source = Provenance(
        source_observation_id="synthetic-receipt",
        actor_kind="patient",
        actor_id="synthetic-subject",
        source_kind="document_observation",
        received_at=NOW,
    )
    first = ReaderResult.model_validate(
        {
            "document_type": kind,
            "printed_identity_hint": PrintedIdentityHint(text="Synthetic Patient"),
            "printed_date": "2026-09-06",
            "items": tuple(
                ItemRead(item=i, judgment="cannot_judge")
                for i in (items or (DocumentItem(name="Potassium", value="5.0", unit="mmol/L"),))
            ),
            "unreadable": False,
            "notes": (),
            "provenance": source,
            "metadata": CallMetadata(
                model_id="synthetic-lite",
                policy_version=POLICY.policy_version,
                latency_ms=1,
                input_tokens=1,
                output_tokens=1,
            ),
            **changes,
        }
    )
    second = first.model_copy(
        update={"metadata": first.metadata.model_copy(update={"model_id": "synthetic-pro"})}
    )
    return DocumentRead(first=first, second=second, disagreements=diff(first, second))


def evidence(
    category: str = "lab_result",
    names: tuple[str, ...] = ("Potassium",),
    *,
    unit: str | None = "mmol/L",
    id: str = "synthetic-evidence",
    **changes: object,
) -> Evidence:
    values = tuple(
        grade_row(
            LabRowCandidate(
                analyte=n,
                value="5.0",
                unit="mg/dL" if n == "Creatinine" and unit == "mmol/L" else unit,
            ),
            POLICY,
        )
        for n in names
    )
    document = read(
        "lab"
        if category == "lab_result"
        else "prescription"
        if category in {"prescription", "medication_list"}
        else "other"
    )
    return Evidence.model_validate(
        {
            "id": id + ":1",
            "evidence_id": id,
            "version": 1,
            "scope": PatientScope(doctor_id="synthetic-doctor", patient_id="synthetic-patient"),
            "created_at": NOW,
            "updated_at": NOW,
            "observation_id": "synthetic-receipt",
            "media_id": "synthetic-media",
            "content_hash": keys.digest(id),
            "category": category,
            "printed_date": date(2026, 9, 6),
            "association_state": "candidate",
            "extracted_values": values
            if category == "lab_result"
            else (DocumentItem(name="Synthetic document"),),
            "readers": (document.first, document.second),
            "provenance": document.first.provenance,
            **changes,
        }
    )


def objective(kind: str = "test", **changes: object) -> Mission:
    details: TestDetails | SendRecordsDetails | TaskDetails | MonitorDetails | VisitDetails = cast(
        TestDetails | SendRecordsDetails | TaskDetails | MonitorDetails | VisitDetails,
        {
            "test": TestDetails(analytes=("Potassium", "Creatinine"), completeness="all"),
            "send_records": SendRecordsDetails(categories=("روشتة",), required_count=1),
            "task_evidence": TaskDetails(
                category="document",
                instruction="Send requested proof",
                completion_rule="doctor_acceptance",
            ),
            "monitor": MonitorDetails(metric="BP", unit="mmHg", slots=(NOW,), required_coverage=1),
            "visit": VisitDetails(objective="report_received"),
        }[kind],
    )
    return Mission.model_validate(
        mission().model_dump()
        | {
            "kind": details.kind,
            "details": details,
            "objective_predicate": EvidencePredicate.model_validate({"evaluator": kind}),
            **changes,
        }
    )


# label, mission, candidate, previously accepted, satisfied, exact missing
EVALUATOR_ROWS: list[tuple[str, Mission, Evidence, tuple[Evidence, ...], bool, tuple[str, ...]]] = [
    ("01 all analytes", objective(), evidence(names=("Potassium", "Creatinine")), (), True, ()),
    ("02 partial potassium", objective(), evidence(), (), False, ("Creatinine",)),
    (
        "03 any analyte",
        objective(details=TestDetails(analytes=("K", "Creatinine"), completeness="any")),
        evidence(),
        (),
        True,
        (),
    ),
    ("04 missing unit", objective(), evidence(unit=None), (), False, ("K", "Creatinine")),
    ("05 unknown unit", objective(), evidence(unit="bananas"), (), False, ("K", "Creatinine")),
    (
        "06 before collection window",
        objective(
            details=TestDetails(analytes=("K",), completeness="all", collection_window_start=NOW)
        ),
        evidence(printed_date=date(2026, 9, 5)),
        (),
        False,
        ("collection_date",),
    ),
    (
        "07 date absent with window",
        objective(
            details=TestDetails(analytes=("K",), completeness="all", collection_window_start=NOW)
        ),
        evidence(printed_date=None),
        (),
        False,
        ("printed_date",),
    ),
    ("08 wrong category", objective(), evidence("prescription"), (), False, ("category",)),
    (
        "09 joint complete",
        objective(),
        evidence(names=("Creatinine",), unit="mg/dL"),
        (
            evidence(
                id="earlier",
                association_state="accepted",
                mission_id="synthetic-mission",
                accepted_at=NOW,
                accepted_by="system",
            ),
        ),
        True,
        (),
    ),
    (
        "10 old prescription valid",
        objective("send_records"),
        evidence("prescription", printed_date=date(2018, 1, 1)),
        (),
        True,
        (),
    ),
    (
        "11 history outside period",
        objective(
            "send_records",
            details=SendRecordsDetails(
                categories=("prescription",),
                required_count=1,
                period_start=NOW - timedelta(days=365),
            ),
        ),
        evidence("prescription", printed_date=date(2018, 1, 1)),
        (),
        False,
        ("period",),
    ),
    (
        "12 history count two",
        objective(
            "send_records",
            details=SendRecordsDetails(categories=("prescription",), required_count=2),
        ),
        evidence("prescription"),
        (),
        False,
        ("documents:1",),
    ),
    (
        "13 task needs doctor",
        objective("task_evidence"),
        evidence("other"),
        (),
        False,
        ("doctor_acceptance",),
    ),
    (
        "14 task doctor acceptance",
        objective("task_evidence"),
        evidence("other", flags=("doctor_accepted",)),
        (),
        True,
        (),
    ),
    (
        "15 monitor awaits slots",
        objective("monitor"),
        evidence("monitor_screen"),
        (),
        False,
        ("slot_assignment",),
    ),
    ("16 visit report", objective("visit"), evidence("discharge_summary"), (), True, ()),
    (
        "17 separate papers",
        objective(),
        evidence(flags=("one_document_per_photo",)),
        (),
        False,
        ("one_document_per_photo",),
    ),
    (
        "18 mismatched name",
        objective(),
        evidence(flags=("identity_mismatch",)),
        (),
        False,
        ("identity",),
    ),
    (
        "19 shifted columns",
        objective(),
        evidence(shift_guard_fired=True),
        (),
        False,
        ("verification",),
    ),
    (
        "20 untrusted instructions",
        objective(),
        evidence(flags=("document_instructions",)),
        (),
        False,
        ("verification",),
    ),
    (
        "21 unverifiable identity content complete; doctor tap gates commit",
        objective(),
        evidence(names=("Potassium", "Creatinine"), flags=("identity_unverifiable",)),
        (),
        True,
        (),
    ),
    (
        "22 corroborated Latin mismatch blocks",
        objective(),
        evidence(names=("Potassium", "Creatinine"), flags=("identity_mismatch",)),
        (),
        False,
        ("identity",),
    ),
]

# label, coarse kind, observed item, caption, expected category (hand written)
CLASSIFICATION_ROWS: list[tuple[str, str, DocumentItem, str, str]] = [
    ("01 lab", "lab", DocumentItem(name="Potassium", value="5", unit="mmol/L"), "", "lab_result"),
    (
        "02 K abbreviation",
        "lab",
        DocumentItem(name="K", value="5", unit="mmol/L"),
        "تحليل",
        "lab_result",
    ),
    (
        "03 prescription",
        "prescription",
        DocumentItem(name="Aspirin", dose="81 mg"),
        "روشتة قديمة",
        "prescription",
    ),
    (
        "04 medication history",
        "prescription",
        DocumentItem(name="Aspirin", dose="81 mg"),
        "medication list",
        "medication_list",
    ),
    ("05 imaging", "other", DocumentItem(name="CT imaging report"), "", "imaging_report"),
    ("06 discharge", "other", DocumentItem(name="Discharge summary"), "", "discharge_summary"),
    (
        "07 glucose device",
        "lab",
        DocumentItem(name="Glucose", value="150", unit="mg/dL"),
        "glucometer",
        "monitor_screen",
    ),
    (
        "08 BP device",
        "other",
        DocumentItem(name="BP", value="120/80", unit="mmHg"),
        "bp monitor",
        "monitor_screen",
    ),
    ("09 ambiguous item", "other", DocumentItem(name="Unidentified note"), "", "other"),
    (
        "10 contradictory kind",
        "prescription",
        DocumentItem(name="Potassium", value="5", unit="mmol/L"),
        "روشتة",
        "other",
    ),
    ("11 ambiguous report", "other", DocumentItem(name="CT imaging discharge"), "", "other"),
    (
        "12 no drug information",
        "prescription",
        DocumentItem(name="[unreadable]"),
        "prescription",
        "other",
    ),
    ("13 Arabic imaging", "other", DocumentItem(name="تقرير أشعة"), "", "imaging_report"),
    ("14 Arabic discharge", "other", DocumentItem(name="تقرير خروج"), "", "discharge_summary"),
]
