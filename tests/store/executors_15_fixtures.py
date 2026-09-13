"""Synthetic executor world: real receipts, authorization and commits; no providers."""

from datetime import timedelta
from typing import Any

from domain_fixtures import mission
from harness import FakeClock

from sanad.auth.service import revise
from sanad.domain import Mission, PatientReportPredicate, WorkClock
from sanad.domain.entities import TaskDetails, VisitDetails
from sanad.store._base import StoreBase
from sanad.store.records import OutboundIntent, Patient, from_record
from store.account_fixtures import APPLICANT, update
from store.concierge_fixtures import PatientWorld, no_problem_readers


def no_provider(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("Contract 15 must make zero provider calls")


def world(store: StoreBase, clock: FakeClock) -> PatientWorld:
    w = PatientWorld.create(store, clock)
    assert isinstance(w, PatientWorld)
    w.enroll(medication=False)
    w.seed(revise(w.doctor, clock(), language="en"))
    row = store.get(w.patient_scope, "patient", w.patient_scope.patient_id)
    assert row
    w.seed(revise(from_record(row, Patient), clock(), language="en"))
    w.scribe.model_factory = no_provider
    w.concierge.model_factory = no_provider
    w.concierge.barrier_model_factory = no_problem_readers
    return w


def add(
    w: PatientWorld,
    kind: str = "VISIT",
    *,
    id: str = "visit",
    title: str = "Cardiology",
    objective: str = "attendance_reported",
    **changes: Any,
) -> Mission:
    now = w.clock()
    due = changes.pop("due_at", now + timedelta(days=7))
    value = mission(
        id=id,
        kind=kind,
        title=title,
        doctor_id=w.doctor.id,
        patient_id=w.patient_scope.patient_id,
        created_at=now,
        updated_at=now,
        confirmed_at=now,
        confirmed_by=w.doctor.id,
        order_refs=(),
        details=VisitDetails.model_validate({"objective": objective})
        if kind == "VISIT"
        else TaskDetails(
            category="doctor_request",
            instruction=title,
            completion_rule="patient_report",
        ),
        objective_predicate=PatientReportPredicate(
            report_kind="visit_" + objective if kind == "VISIT" else "doctor_task"
        ),
        due_at=due,
        escalation_at=due,
        review_at=due,
        work_clock=WorkClock(work_lane="mission", next_action_at=now),
        **changes,
    )
    w.seed(value)
    return value


def get(w: PatientWorld, value: Mission) -> Mission:
    row = w.store.get(w.patient_scope, "mission", value.id)
    assert row
    return from_record(row, Mission)


def doctor(w: PatientWorld, text: str, id: int = 3000) -> OutboundIntent:
    before = {i.id for i in w.cards()}
    assert w.post(update(APPLICANT, text, id)).status_code == 200
    assert w.receipt(id).state == "completed"
    new = [i for i in w.cards() if i.id not in before]
    assert len(new) == 1
    return new[0]


def question(w: PatientWorld) -> Mission:
    model, reply = w.send("talk to the doctor")
    assert not model.script.calls and reply.template_id == "patient_question_forwarded"
    return from_record(next(r for r in w.rows("mission") if r.body["kind"] == "QUESTION"), Mission)


def task_button(intent: OutboundIntent, *, accept: bool) -> str:
    assert intent.payload
    markup = intent.payload["reply_markup"]
    assert isinstance(markup, dict)
    rows = markup["inline_keyboard"]
    assert isinstance(rows, list) and isinstance(rows[0], list)
    button = rows[0][0 if accept else 1]
    assert isinstance(button, dict) and isinstance(button["callback_data"], str)
    return button["callback_data"]
