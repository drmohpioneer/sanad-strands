"""Outcome tests for VISIT/TASK/QUESTION, parametrized over the accepted stores."""

from datetime import timedelta

import pytest
from harness import FakeClock

from sanad.concierge.records import ReportFactPayload
from sanad.domain.entities import VisitDetails
from sanad.scribe.records import ClinicalFact
from sanad.store._base import StoreBase
from sanad.store.records import CommandEnvelope, OutboundIntent, from_record
from store.account_fixtures import APPLICANT, callback
from store.concierge_fixtures import PatientWorld
from store.executors_15_fixtures import add, doctor, get, question, task_button
from store.executors_15_fixtures import world as create_world


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> PatientWorld:
    return create_world(store, clock)


@pytest.mark.parametrize(
    "objective,text,state,report,template",
    [
        ("arrange", "booked Cardiology", "fulfilled", "visit_booking", "patient_visit_booked"),
        (
            "booking_reported",
            "booked Cardiology",
            "fulfilled",
            "visit_booking",
            "patient_visit_booked",
        ),
        (
            "attendance_reported",
            "booked Cardiology",
            "open",
            "visit_booking",
            "patient_visit_booked_wait",
        ),
        (
            "attendance_reported",
            "attended Cardiology",
            "fulfilled",
            "visit_attendance",
            "patient_visit_attended",
        ),
        (
            "attendance_reported",
            "booked and attended Cardiology",
            "fulfilled",
            "visit_attendance",
            "patient_visit_attended",
        ),
        (
            "report_received",
            "went to Cardiology",
            "open",
            "visit_report_pending",
            "patient_visit_report_needed",
        ),
        (
            "booking_reported",
            "went to Cardiology",
            "open",
            "visit_attendance",
            "patient_visit_booking_needed",
        ),
        (
            "attendance_reported",
            "couldn't go Cardiology",
            "open",
            "visit_attendance",
            "patient_visit_not_attended",
        ),
    ],
)
def test_visit_objective_matrix(
    world: PatientWorld, objective: str, text: str, state: str, report: str, template: str
) -> None:
    m = add(world, objective=objective)
    model, reply = world.send(text)
    assert not model.script.calls and reply.template_id == template
    changed = get(world, m)
    assert changed.state == state and changed.due_at == m.due_at
    facts = [from_record(r, ClinicalFact) for r in world.rows("clinical_fact")]
    assert len(facts) == 1
    assert (
        isinstance(facts[0].payload, ReportFactPayload) and facts[0].payload.report_kind == report
    )
    assert facts[0].provenance.source_observation_id == world.receipt(1000).id
    done = [
        r
        for r in world.rows("outbound_intent")
        if r.body["notification_purpose"] == "DONE:FULFILLMENT"
    ]
    assert len(done) == int(state == "fulfilled")


def test_booking_local_window_and_early_attendance(world: PatientWorld) -> None:
    m = add(world)
    world.send("booked Cardiology tomorrow")
    booked = get(world, m)
    assert (
        isinstance(booked.details, VisitDetails)
        and booked.details.window_start
        and booked.details.window_end
    )
    assert booked.details.window_start.isoformat() == "2026-09-06T21:00:00+00:00"
    assert booked.details.window_end.isoformat() == "2026-09-07T20:59:59.999999+00:00"
    assert (booked.state, booked.due_at, booked.escalation_at) == (
        m.state,
        m.due_at,
        m.escalation_at,
    )
    world.send("attended Cardiology")
    assert get(world, m).state == "fulfilled"
    assert any(
        "before the recorded visit window" in str(r.body) for r in world.rows("clinical_fact")
    )


def test_late_booking_keeps_deadline_and_opens_unmet_without_doctor_message(
    world: PatientWorld,
) -> None:
    m = add(world, objective="booking_reported")
    model, reply = world.send("booked Cardiology 2026-10-01")
    assert not model.script.calls and reply.template_id == "patient_visit_late_booking"
    changed = get(world, m)
    assert isinstance(changed.details, VisitDetails)
    assert changed.state == "open" and changed.details.window_start is None
    assert changed.escalation_at == m.escalation_at
    assert [r.body["review_kind"] for r in world.rows("review")] == ["unmet_objective"]
    assert not [r for r in world.rows("outbound_intent") if r.body["audience"] == "doctor"]


@pytest.mark.parametrize(
    "kind,text,template",
    [
        ("VISIT", "attended Cardiology", "patient_visit_choose"),
        ("TASK", "finished Cardiology", "patient_task_choose"),
    ],
)
def test_ambiguous_report_selects_once_with_original_report_time(
    world: PatientWorld, kind: str, text: str, template: str
) -> None:
    first = add(world, kind, id="first")
    second = add(world, kind, id="second")
    model, reply = world.send(text)
    assert not model.script.calls and reply.template_id == template
    original_time = world.clock()
    world.clock.advance(timedelta(minutes=2))
    world.press(reply)
    values = [get(world, m) for m in (first, second)]
    assert sum(m.state == "fulfilled" for m in values) == 1
    assert next(m for m in values if m.state == "fulfilled").objective_received_at == original_time
    world.press(reply, id=2001)
    assert len(world.rows("clinical_fact")) == 1


@pytest.mark.parametrize(
    "kind,text,template",
    [
        ("VISIT", "attended John's Cardiology", "patient_visit_missing"),
        ("TASK", "finished another patient's Cardiology", "patient_task_missing"),
    ],
)
def test_other_patient_name_never_completes_own_mission(
    world: PatientWorld, kind: str, text: str, template: str
) -> None:
    m = add(world, kind)
    model, reply = world.send(text)
    assert not model.script.calls and reply.template_id == template
    assert get(world, m).state == "open"
    assert len([r for r in world.rows("mission") if r.body["kind"] == "QUESTION"]) == 1


@pytest.mark.parametrize("accept", [True, False])
def test_task_done_doctor_accept_or_reopen(world: PatientWorld, accept: bool) -> None:
    question(world)
    m = add(world, "TASK", title="Bring diary")
    model, reply = world.send("finished diary")
    assert not model.script.calls and reply.template_id == "patient_task_recorded"
    assert get(world, m).state == "fulfilled"
    fact = from_record(world.rows("clinical_fact")[0], ClinicalFact)
    assert isinstance(fact.payload, ReportFactPayload)
    assert fact.payload.detail == "self-reported; pending doctor acceptance"
    done = from_record(
        next(r for r in world.rows("outbound_intent") if r.body["audience"] == "doctor"),
        OutboundIntent,
    )
    assert done.payload and "self-reported" in str(done.payload["text"]).lower()
    assert "Synthetic Patient" in str(done.payload["text"])
    token = task_button(done, accept=accept)
    reviews_before = world.rows("review")
    acknowledgments = len(world.transport.callback_calls)
    assert world.post(callback(token, APPLICANT, 3000)).status_code == 200
    assert len(world.transport.callback_calls) == acknowledgments + 1
    assert world.receipt(3000).state == "completed"
    from sanad.store import keys

    replay = world.runtime.steward.handle(
        CommandEnvelope(
            command_id="task-doctor:" + world.receipt(3000).id,
            principal=world.actor(APPLICANT),
            scope=world.patient_scope,
            requested_at=world.clock(),
            payload={
                "type": "AcceptTask" if accept else "ReopenTask",
                "intent_id": done.id,
                "token_hash": keys.digest(token),
            },
        )
    )
    assert replay.status == "accepted", replay
    changed = get(world, m)
    if accept:
        assert changed.state == "fulfilled"
        assert world.rows("review") == reviews_before
        assert (
            len([r for r in world.rows("audit_event") if r.body["event_type"] == "DoctorAccepted"])
            == 1
        )
    else:
        assert changed.state == "open" and changed.due_at == world.clock() + timedelta(days=3)
        assert any(
            i.template_id == "patient_task_reopened" and "Bring diary" in str(i.payload)
            for i in world.patient_intents()
        )
    world.post(callback(token, APPLICANT, 3001))
    assert get(world, m) == changed


@pytest.mark.parametrize("close", [False, True])
def test_questions_list_and_answer_or_close(world: PatientWorld, close: bool) -> None:
    m = question(world)
    assert m.title == "Question for review"
    assert not [r for r in world.rows("outbound_intent") if r.body["audience"] == "doctor"]
    listing = doctor(world, "/questions")
    assert (
        listing.payload
        and "Synthetic Patient" in str(listing.payload)
        and "talk to the doctor" in str(listing.payload)
    )
    assert listing.question_listing_targets == ((m.patient_id, m.id),)
    result = doctor(
        world, "/close 1" if close else "/answer 1 Please bring your diary to the visit.", 3001
    )
    assert result.template_id == "doctor_question_recorded", result.payload
    assert get(world, m).state == ("closed_unfulfilled" if close else "fulfilled")
    assert all(r.body["state"] == "resolved" for r in world.rows("review"))
    assert (
        len(
            [
                i
                for i in world.patient_intents()
                if i.template_id
                == ("patient_question_closed" if close else "patient_question_answered")
            ]
        )
        == 1
    )
    world.send("talk to the doctor")
    assert len([r for r in world.rows("mission") if r.body["kind"] == "QUESTION"]) == 2


@pytest.mark.parametrize("answer", ["Take warfarin 5 mg tonight.", "a" * 701])
def test_bad_doctor_answer_refuses_all_clinical_and_patient_effects(
    world: PatientWorld, answer: str
) -> None:
    m = question(world)
    doctor(world, "/questions")
    before = len(world.patient_intents())
    result = doctor(world, "/answer 1 " + answer, 3001)
    assert result.template_id == "doctor_question_refused"
    assert get(world, m).state == "open" and len(world.patient_intents()) == before
    assert world.rows("review")[0].body["state"] == "open"


def test_question_listing_expires_and_wrong_doctor_command_is_denied(world: PatientWorld) -> None:
    m = question(world)
    doctor(world, "/questions")
    world.clock.advance(timedelta(hours=1))
    result = doctor(world, "/answer 1 Bring your diary.", 3001)
    assert result.template_id == "doctor_question_list_stale" and get(world, m).state == "open"
    actor = world.actor(APPLICANT).model_copy(update={"doctor_id": "another-doctor"})
    result_command = world.runtime.steward.handle(
        CommandEnvelope(
            command_id="wrong-owner",
            principal=actor,
            scope=world.patient_scope,
            requested_at=world.clock(),
            payload={
                "type": "AnswerQuestion",
                "mission_id": m.id,
                "answer_text": "Bring your diary.",
            },
        )
    )
    assert result_command.status == "forbidden" and get(world, m).state == "open"


@pytest.mark.parametrize("accept", [True, False])
def test_task_buttons_expire_at_card_limit(world: PatientWorld, accept: bool) -> None:
    m = add(world, "TASK", title="Bring diary")
    world.send("finished diary")
    done = from_record(
        next(r for r in world.rows("outbound_intent") if r.body["audience"] == "doctor"),
        OutboundIntent,
    )
    token = task_button(done, accept=accept)
    before = get(world, m)
    world.clock.advance(timedelta(minutes=30))
    world.post(callback(token, APPLICANT, 3000))
    assert get(world, m) == before
    assert not [r for r in world.rows("audit_event") if r.body["event_type"] == "DoctorAccepted"]
    assert any(i.template_id == "doctor_task_stale" for i in world.cards())


@pytest.mark.parametrize("text", ["plan", "الخطة"])
def test_plan_update_notice_reply_words_are_deterministic(world: PatientWorld, text: str) -> None:
    model, reply = world.send(text)
    assert not model.script.calls and reply.template_id == "patient_plan_summary"
