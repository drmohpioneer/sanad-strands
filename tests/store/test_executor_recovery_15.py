"""Atomicity, recovery and stale authority using real clinical/tenant transactions."""

from datetime import timedelta
from typing import Any

import pytest
from harness import FakeClock, SimulatedCrash

from sanad.auth.service import revise
from sanad.concierge import answer_command
from sanad.domain import Mission
from sanad.domain.deadlines import ExplicitTiming
from sanad.domain.entities import QuestionDetails
from sanad.ops.sweep import sweep_due
from sanad.store._base import StoreBase
from sanad.store.records import (
    CommandEnvelope,
    CommitRequest,
    OutboundIntent,
    Patient,
    from_record,
    to_record,
)
from store.account_fixtures import APPLICANT, PATIENT, callback, update
from store.concierge_fixtures import PatientWorld
from store.executors_15_fixtures import add, doctor, get, no_provider, question, task_button
from store.executors_15_fixtures import world as create_world


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> PatientWorld:
    return create_world(store, clock)


def command(w: PatientWorld, m: Mission, kind: str, **payload: Any) -> Any:
    return w.runtime.steward.handle(
        CommandEnvelope(
            command_id=kind + ":" + str(m.version),
            scope=w.patient_scope,
            principal=w.actor(APPLICANT),
            requested_at=w.clock(),
            payload={"type": kind, "mission_id": m.id, **payload},
        )
    )


def sweep(w: PatientWorld) -> None:
    result = sweep_due(w.runtime, w.store, elapsed_clock=lambda: 0)
    assert not result["errors"], result


@pytest.mark.parametrize("operation", ["answer", "close", "accept", "reopen"])
def test_doctor_clinical_commit_survives_receipt_gap(world: PatientWorld, operation: str) -> None:
    if operation in {"answer", "close"}:
        m = question(world)
        doctor(world, "/questions")
        request = update(
            APPLICANT, "/answer 1 Bring your diary." if operation == "answer" else "/close 1", 3001
        )
    else:
        m = add(world, "TASK", title="Bring diary")
        world.send("finished diary")
        done = next(r for r in world.rows("outbound_intent") if r.body["audience"] == "doctor")
        request = callback(
            task_button(from_record(done, OutboundIntent), accept=operation == "accept"),
            APPLICANT,
            3001,
        )

    def crash(name: str) -> None:
        if name == "doctor_clinical_committed":
            raise SimulatedCrash()

    world.scribe.checkpoint = crash
    with pytest.raises(SimulatedCrash):
        world.post(request)
    expected = get(world, m)
    assert (
        expected.state
        == {
            "answer": "fulfilled",
            "close": "closed_unfulfilled",
            "accept": "fulfilled",
            "reopen": "open",
        }[operation]
    )
    before = {r.id for r in world.rows("audit_event")}
    intents = {i.id for i in world.patient_intents()}
    assert world.receipt(3001).state != "completed"
    # Recovery must find the original target even after the listing has expired.
    world.clock.advance(timedelta(hours=1, minutes=1))
    world.scribe.checkpoint = lambda name: None
    sweep(world)
    sweep(world)
    assert world.receipt(3001).state == "completed"
    assert before <= {r.id for r in world.rows("audit_event")}
    assert intents <= {i.id for i in world.patient_intents()}
    assert len(
        [r for r in world.rows("audit_event") if r.body["event_type"] == "DoctorAccepted"]
    ) == int(operation == "accept")
    assert (
        len(
            [
                i
                for i in world.cards()
                if i.template_id
                == {
                    "answer": "doctor_question_recorded",
                    "close": "doctor_question_recorded",
                    "accept": "doctor_task_accepted",
                    "reopen": "doctor_task_reopened",
                }[operation]
            ]
        )
        == 1
    )
    assert len(
        [i for i in world.patient_intents() if i.template_id == "patient_question_answered"]
    ) == int(operation == "answer")


def test_failure_between_question_fulfillment_and_intent_is_atomic(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = question(world)
    before = {k: world.rows(k) for k in ("mission", "review", "outbound_intent", "audit_event")}

    def crash(*args: Any, **kwargs: Any) -> bool:
        raise SimulatedCrash()

    with monkeypatch.context() as patch:
        patch.setattr(answer_command, "patient_intent", crash)
        with pytest.raises(SimulatedCrash):
            command(world, m, "AnswerQuestion", answer_text="Bring your diary.")
    assert before == {k: world.rows(k) for k in before}
    assert command(world, m, "AnswerQuestion", answer_text="Bring your diary.").status == "accepted"
    assert get(world, m).state == "fulfilled"


@pytest.mark.parametrize("shape", ["visit_fulfill", "task_fulfill", "booking_window"])
@pytest.mark.parametrize("mutation", ["wrong_command", "unrelated_field"])
def test_patient_guard_rejects_extra_authority(
    world: PatientWorld, shape: str, mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = add(world, "TASK" if shape == "task_fulfill" else "VISIT")
    original = world.store.commit
    outcomes = []

    def corrupt(request: CommitRequest) -> Any:
        if (
            request.command.payload.get("executor") == "concierge-v1"
            and request.command.payload["type"] == "RecordPatientReply"
        ):
            if mutation == "wrong_command":
                request = request.model_copy(
                    update={
                        "command": request.command.model_copy(
                            update={
                                "payload": request.command.payload
                                | {"type": "CreateSupportTicket"},
                            }
                        )
                    }
                )
            else:
                request = request.model_copy(
                    update={
                        "puts": tuple(
                            to_record(
                                from_record(r, Mission).model_copy(
                                    update={"title": "Unauthorized title"}
                                ),
                                world.patient_scope,
                            )
                            if r.entity_type == "mission" and r.id == m.id
                            else r
                            for r in request.puts
                        )
                    }
                )
            result = original(request)
            outcomes.append(result)
            return result
        return original(request)

    monkeypatch.setattr(world.store, "commit", corrupt)
    text = (
        "booked Cardiology tomorrow"
        if shape == "booking_window"
        else "finished Cardiology"
        if shape == "task_fulfill"
        else "attended Cardiology"
    )
    world.post(update(PATIENT, text, 1000))
    assert outcomes and outcomes[0].status == "forbidden"
    assert get(world, m) == m and not world.rows("clinical_fact")


@pytest.mark.parametrize("mode", ["routine_stop", "full_stop", "unreachable"])
def test_question_completion_reports_delivery_state(world: PatientWorld, mode: str) -> None:
    m = question(world)
    if mode == "routine_stop":
        world.send("stop messages")
    elif mode == "full_stop":
        world.seed(revise(world.profile, world.clock(), consent_active=False))
    else:
        row = world.store.get(world.patient_scope, "patient", m.patient_id)
        assert row
        world.seed(revise(from_record(row, Patient), world.clock(), contact_status="unreachable"))
    doctor(world, "/questions")
    result = doctor(world, "/answer 1 Bring your diary.", 3001)
    assert get(world, m).state == "fulfilled"
    assert result.template_id == (
        "doctor_question_recorded" if mode == "routine_stop" else "doctor_question_delivery_pending"
    )
    intent = next(
        i for i in world.patient_intents() if i.template_id == "patient_question_answered"
    )
    from store.contact_fixtures import dispatch

    intent = dispatch(world, intent)
    assert intent.status == ("provider_accepted" if mode == "routine_stop" else "suppressed")


def test_held_answer_is_private_until_real_amendment_then_release_once(world: PatientWorld) -> None:
    world.dictate(
        "Synthetic Patient start Atorvastatin 20 mg at night",
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "orders": [
                {
                    "action": "start",
                    "drug": "Atorvastatin",
                    "dose": "20 mg",
                    "timing": "at night",
                    "action_quote": "start",
                }
            ],
        },
        id=400,
    )
    world.tap("✅ Confirm", id=401)
    world.scribe.model_factory = no_provider
    assert world.rows("care_order_version")
    m = question(world)
    doctor(world, "/questions")
    held_text = "Increase Atorvastatin to 40 mg."
    result = doctor(world, "/answer 1 " + held_text, 3001)
    assert result.template_id == "doctor_question_held"
    held = get(world, m)
    assert isinstance(held.details, QuestionDetails)
    assert (
        held.state == "open"
        and held.details.held_answer == held_text
        and not held.details.held_answer_ready
    )
    timing = ExplicitTiming(
        instant=world.clock() + timedelta(days=4),
        timezone="Africa/Cairo",
        original_expression="in four days",
    )
    result_command = command(
        world,
        held,
        "ExtendMission",
        timing=timing.model_dump(mode="json"),
        reason="Doctor extended",
    )
    assert result_command.status == "accepted", result_command
    extended = get(world, m)
    assert extended.due_at == timing.instant
    assert all(
        r.body["review_at"] == timing.instant.isoformat().replace("+00:00", "Z")
        for r in world.rows("review")
        if r.body["review_kind"] == "question_answer"
    )
    assert command(world, extended, "CancelMission", reason="cancel").status == "needs_confirmation"
    world.dictate(
        "Synthetic Patient change Atorvastatin to 40 mg at night",
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "orders": [
                {
                    "action": "change",
                    "drug": "Atorvastatin",
                    "dose": "40 mg",
                    "timing": "at night",
                    "action_quote": "change",
                }
            ],
        },
        id=402,
    )
    world.tap("✅ Confirm", id=403)
    flagged = get(world, m)
    assert isinstance(flagged.details, QuestionDetails)
    assert flagged.state == "open" and flagged.details.held_answer_ready
    assert not any(
        i.template_id == "patient_question_plan_updated" for i in world.patient_intents()
    )
    # Treat the stopped process between confirmation and next tick as a crash gap.
    # Recreate the runtime on the same store rather than calling a helper directly.
    recovered = PatientWorld.create(world.store, world.clock)
    assert isinstance(recovered, PatientWorld)
    recovered.patient_scope = world.patient_scope
    recovered.scribe.model_factory = no_provider
    recovered.concierge.model_factory = no_provider
    sweep(recovered)
    sweep(recovered)
    assert get(recovered, m).state == "fulfilled"
    released = [
        i for i in recovered.patient_intents() if i.template_id == "patient_question_plan_updated"
    ]
    assert len(released) == 1 and released[0].status == "provider_accepted"
    assert released[0].payload == {
        "text": "Your doctor answered by updating your plan. The change is in your plan; "
        'send "plan" to see it.'
    }
    assert all(held_text not in str(i.payload) for i in recovered.patient_intents())
    answered = get(recovered, m)
    assert (
        isinstance(answered.details, QuestionDetails) and answered.details.held_answer_consumed_at
    )
    reopened = command(
        recovered,
        answered,
        "ReopenMission",
        reason="New question review",
        timing=timing.model_dump(mode="json"),
        new_objective_predicate=answered.objective_predicate.model_dump(mode="json"),
        new_order_refs=[],
    )
    assert reopened.status == "accepted", reopened
    sweep(recovered)
    assert get(recovered, m).state == "open"
    assert (
        len(
            [
                i
                for i in recovered.patient_intents()
                if i.template_id == "patient_question_plan_updated"
            ]
        )
        == 1
    )
