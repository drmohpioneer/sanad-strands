"""Synthetic missions and the captured HTTP adapter, shared by parity tests."""

from datetime import timedelta

from domain_fixtures import mission
from harness import FakeClock
from resolver.fixtures import Capture

from sanad.domain import EvidencePredicate, Mission
from sanad.domain.entities import MissionDetails
from sanad.store._base import StoreBase
from sanad.store.records import from_record
from store.concierge_fixtures import PatientWorld
from store.medication_fixtures import confirm, world

QUESTION = "Which area or neighbourhood should I search near?"


def setup(store: StoreBase, clock: FakeClock) -> tuple[PatientWorld, Capture]:
    w = world(store, clock)
    capture = Capture()
    w.concierge.places_provider = capture.provider()
    return w, capture


def add(
    w: PatientWorld, kind: str = "TEST", id: str = "synthetic-test", title: str = "CBC"
) -> Mission:
    if kind == "MEDICATION":
        confirm(w, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
        return from_record(
            next(r for r in w.rows("mission") if r.body["kind"] == "MEDICATION"), Mission
        )
    from pydantic import TypeAdapter

    details: dict[str, dict[str, object]] = {
        "TEST": {"analytes": (title,), "completeness": "all"},
        "MONITOR": {
            "metric": "weight",
            "unit": "kg",
            "slots": (w.clock() + timedelta(days=1),),
            "required_coverage": 1,
        },
        "VISIT": {"objective": "attendance_reported"},
        "TASK": {
            "category": "doctor_request",
            "instruction": title,
            "completion_rule": "patient_report",
        },
        "SEND_RECORDS": {"categories": ("lab_result",), "required_count": 1},
    }
    value = mission(
        id=id,
        created_at=w.clock(),
        updated_at=w.clock(),
        confirmed_at=w.clock(),
        kind=kind,
        title=title,
        doctor_id=w.doctor.id,
        patient_id=w.patient_scope.patient_id,
        order_refs=(),
        details=TypeAdapter(MissionDetails).validate_python({"kind": kind, **details[kind]}),
        objective_predicate=EvidencePredicate(evaluator="test"),
        due_at=w.clock() + timedelta(days=5),
        escalation_at=w.clock() + timedelta(days=5),
    )
    w.seed(value)
    return value


def get(w: PatientWorld, id: str = "synthetic-test") -> Mission:
    row = w.store.get(w.patient_scope, "mission", id)
    assert row
    return from_record(row, Mission)
