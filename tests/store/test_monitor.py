"""MONITOR transaction outcomes, negative guard cases and Local parity."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from harness import FakeClock, SimulatedCrash

from sanad.auth.service import revise
from sanad.contact.delivery import doctor_payload
from sanad.domain import EvidencePredicate, Mission, PatientScope, TaskDetails, WorkClock
from sanad.domain.entities import MonitorDetails
from sanad.monitor.report import doctor_table
from sanad.monitor.slots import filled
from sanad.scribe.records import ClinicalFact
from sanad.store._base import StoreBase
from sanad.store.records import (
    CommitRequest,
    CommitResult,
    OutboundIntent,
    Patient,
    from_record,
    to_record,
)
from store import evidence_fixtures as f
from store.account_fixtures import PATIENT, update
from store.concierge_fixtures import PatientWorld


@pytest.fixture
def world(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> Iterator[PatientWorld]:
    value = f.world(store, clock)
    patient = store.get(value.patient_scope, "patient", value.patient_scope.patient_id)
    assert patient
    value.seed(revise(from_record(patient, Patient), clock(), language="en"))
    value.seed(revise(value.doctor, clock(), language="en"))
    calls = []

    def forbidden(*args: object, **kwargs: object) -> None:
        calls.append(True)
        raise AssertionError("MONITOR must not invoke a model")

    monkeypatch.setattr("sanad.concierge.turn.make_agent", forbidden)
    yield value
    assert not calls


def monitor(
    world: PatientWorld,
    id: str = "monitor",
    *,
    count: int = 2,
    name: str = "blood pressure",
    unit: str = "mmHg",
    **changes: object,
) -> Mission:
    now = world.clock()
    return f.mission(
        world,
        id,
        kind="MONITOR",
        title="Monitor " + name,
        details=MonitorDetails(
            metric=name,
            unit=unit,
            slots=tuple(now + timedelta(hours=6 * i) for i in range(count)),
            required_coverage=count,
        ),
        objective_predicate=EvidencePredicate(evaluator="monitor"),
        order_refs=(),
        **changes,
    )


def current(world: PatientWorld, id: str = "monitor") -> Mission:
    value = world.store.get_mission(world.patient_scope, id)
    assert value
    return value


def test_text_duplicate_extra_completion_and_replay(world: PatientWorld) -> None:
    m = monitor(world)
    model, reply = world.send("BP 120/80", id=1100)
    assert not model.script.calls and "1 readings left" in str(reply.payload)
    assert current(world).state == "open"
    world.clock.advance(timedelta(hours=1))
    world.send("BP 130/85", id=1101)
    details = current(world).details
    assert isinstance(details, MonitorDetails)
    assert len(filled(details)) == 1 and filled(details)[0].value == "130/85"
    world.clock.advance(timedelta(hours=9))
    _, extra = world.send("BP 140/90", id=1102)
    assert "outside the schedule windows" in str(extra.payload)
    # The report supplies its own exact time, within slot 1, after a delayed receipt.
    stamp = m.details.slots[1].isoformat() if isinstance(m.details, MonitorDetails) else ""
    _, last_reply = world.send("BP 150/95 " + stamp, id=1103)
    completed = current(world)
    assert completed.state == "fulfilled" and completed.objective_received_at == world.clock(), (
        last_reply.payload,
        completed.details,
    )
    before = world.rows("clinical_fact"), world.rows("mission"), world.rows("review")
    world.post(update(PATIENT, "BP 150/95 " + stamp, 1103))
    assert before == (world.rows("clinical_fact"), world.rows("mission"), world.rows("review"))
    assert len([r for r in world.rows("review") if r.body["review_kind"] == "result_review"]) == 1
    done = [
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body.get("notification_purpose") == "DONE:FULFILLMENT"
    ]
    assert len(done) == 1
    text = str(doctor_payload(world.store, done[0]))
    assert "130/85" in text and "150/95" in text and "Extra readings: 1" in text


@pytest.mark.parametrize(
    "name,unit,text,judgment",
    [
        ("blood glucose", "mg/dL", "glucose 100 mg/dL", "normal"),
        ("weight", "kg", "weight 70 kg", "cannot_judge"),
        ("pulse", "bpm", "pulse 75 bpm", "cannot_judge"),
    ],
)
def test_supported_metrics_preserve_judgment(
    world: PatientWorld, name: str, unit: str, text: str, judgment: str
) -> None:
    monitor(world, count=1, name=name, unit=unit)
    model, _ = world.send(text)
    assert not model.script.calls and current(world).state == "fulfilled"
    facts = [
        from_record(r, ClinicalFact)
        for r in world.rows("clinical_fact")
        if r.body["category"] == "patient_report"
    ]
    assert judgment in str(facts[-1].payload)


@pytest.mark.parametrize(
    "text",
    [
        "BP 300/200",
        "BP 130/85 kg",
        "glucose 100",
        "glucose 100 kg",
        "glucose >100 mg/dL",
        "glucose 100 mg/dL at 25:00",
    ],
)
def test_unverified_readings_leave_coverage_open(world: PatientWorld, text: str) -> None:
    monitor(
        world,
        count=1,
        name="blood glucose" if "glucose" in text else "blood pressure",
        unit="mg/dL" if "glucose" in text else "mmHg",
    )
    model, _ = world.send(text)
    assert not model.script.calls and current(world).state == "open"
    assert world.rows("clinical_fact")
    details = current(world).details
    assert isinstance(details, MonitorDetails) and not details.readings


def test_two_missions_use_single_use_choice(world: PatientWorld) -> None:
    monitor(world, "first", count=1)
    monitor(world, "second", count=1)
    _, choice = world.send("BP 125/85")
    assert "Which monitoring schedule" in str(choice.payload)
    assert all(current(world, id).state == "open" for id in ("first", "second"))
    world.press(choice)
    assert sorted(current(world, id).state for id in ("first", "second")) == ["fulfilled", "open"]
    before = world.rows("mission"), world.rows("clinical_fact")
    world.press(choice, id=2001)
    assert before[1] == world.rows("clinical_fact")
    assert sorted(current(world, id).state for id in ("first", "second")) == ["fulfilled", "open"]


@pytest.mark.parametrize("state", ["cancelled", "superseded", "closed_unfulfilled"])
def test_terminal_and_foreign_missions_receive_no_slot(world: PatientWorld, state: str) -> None:
    m = monitor(world, state=state, count=1, work_clock=None)
    other_scope = PatientScope(doctor_id=world.patient_scope.doctor_id, patient_id="other-patient")
    foreign = Mission.model_validate(
        m.model_dump()
        | {
            "id": "foreign",
            "patient_id": other_scope.patient_id,
            "state": "open",
            "work_clock": WorkClock(next_action_at=world.clock(), work_lane="mission"),
        }
    )
    world.seed(foreign)
    world.send("BP 125/85")
    assert current(world) == m
    assert world.store.get_mission(other_scope, foreign.id) == foreign


def test_unknown_metric_becomes_review_without_safety_claim(world: PatientWorld) -> None:
    f.mission(
        world,
        kind="TASK",
        title="Measure oxygen 2 times a day for 5 days",
        details=TaskDetails(
            category="doctor_request",
            instruction="Measure oxygen 2 times a day for 5 days",
            completion_rule="patient_report_of_requested_action",
        ),
        objective_predicate={"kind": "patient_report", "report_kind": "doctor_task"},
    )
    model, reply = world.send("oxygen 95%")
    assert not model.script.calls and "doctor" in str(reply.payload)
    assert any(r.body["kind"] == "QUESTION" for r in world.rows("mission"))
    assert not any(r.body["state"] == "fulfilled" for r in world.rows("mission"))


@pytest.mark.parametrize("critical", [False, True])
def test_photo_and_text_share_slots(world: PatientWorld, critical: bool) -> None:
    monitor(world)
    world.send("BP 120/80")
    doc = f.lab(
        items=[{"name": "BP", "value": "190/125" if critical else "135/85", "unit": "mmHg"}],
        document_type="other",
    )
    vision, _, _ = f.providers(world, doc, doc)
    world.clock.advance(timedelta(hours=1))
    assert f.upload(world, caption="BP monitor screen") == "accepted"
    assert len(vision.calls) == 2
    evidence = f.current(world)
    assert evidence.category == "monitor_screen" and evidence.association_state == "accepted"
    d = current(world).details
    assert isinstance(d, MonitorDetails) and len(filled(d)) == 1 and len(d.readings) == 2
    world.clock.advance(timedelta(hours=5))
    world.send("BP 140/90")
    assert current(world).state == "fulfilled"
    if critical:
        assert world.rows("incident")
        assert "code (sanad.safety.kernel; copied core/vitals.py table)" in doctor_table(
            world.store, world.patient_scope, current(world), "en"
        )


@pytest.mark.parametrize("case", ["positive", "slot", "coverage", "unrelated", "source"])
def test_store_recomputes_exact_revision(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monitor(world, count=1 if case != "coverage" else 2)
    commit = world.store.commit
    outcomes = []

    def checked(request: CommitRequest) -> CommitResult:
        if request.command.payload.get("type") == "RecordPatientReply":
            rows = list(request.puts)
            index = next(
                i for i, r in enumerate(rows) if r.entity_type == "mission" and r.id == "monitor"
            )
            m = from_record(rows[index], Mission)
            assert isinstance(m.details, MonitorDetails)
            if case == "slot":
                d = m.details.model_copy(
                    update={"readings": (m.details.readings[0].model_copy(update={"slot": None}),)}
                )
                m = m.model_copy(update={"details": d})
            elif case == "source":
                assert isinstance(m.details, MonitorDetails)
                d = m.details.model_copy(
                    update={
                        "readings": (m.details.readings[0].model_copy(update={"value": "170/100"}),)
                    }
                )
                m = m.model_copy(update={"details": d})
            elif case == "coverage":
                m = Mission.model_validate(
                    m.model_dump()
                    | {
                        "state": "fulfilled",
                        "fulfillment_validity": "valid",
                        "fulfilled_at": world.clock(),
                        "fulfillment_event_id": "forged",
                        "objective_received_at": world.clock(),
                        "timeliness": "on_time",
                        "work_clock": None,
                    }
                )
            elif case == "unrelated":
                m = m.model_copy(update={"title": "forged title"})
            rows[index] = to_record(m, world.patient_scope)
            request = request.model_copy(update={"puts": tuple(rows)})
        result = commit(request)
        if request.command.payload.get("type") == "RecordPatientReply":
            outcomes.append(result.status)
        return result

    monkeypatch.setattr(world.store, "commit", checked)
    world.post(update(PATIENT, "BP 120/80", 1100))
    assert outcomes == ["accepted" if case == "positive" else "forbidden"]
    assert bool(current(world).details.readings) == (case == "positive")  # type: ignore[union-attr]
    if case != "positive":
        assert not world.rows("clinical_fact") and world.receipt(1100).state == "processing"


def test_crash_before_commit_is_atomic_and_recovers(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor(world, count=1)
    commit = world.store.commit
    broken = True

    def crash(request: CommitRequest) -> CommitResult:
        nonlocal broken
        if broken and request.command.payload.get("type") == "RecordPatientReply":
            assert {"mission", "clinical_fact", "review"} <= {r.entity_type for r in request.puts}
            assert request.receipt_completion and request.intents
            broken = False
            raise SimulatedCrash("synthetic precommit crash")
        return commit(request)

    monkeypatch.setattr(world.store, "commit", crash)
    with pytest.raises(SimulatedCrash, match="synthetic precommit crash"):
        world.post(update(PATIENT, "BP 120/80", 1100))
    assert current(world).state == "open" and not world.rows("clinical_fact")
    world.clock.advance(world.runtime.accounts.policy.operations.claim_ttl + timedelta(seconds=1))
    world.post(update(PATIENT, "BP 120/80", 1100))
    from sanad.ops.sweep import sweep_due

    assert not sweep_due(world.runtime, world.store, elapsed_clock=lambda: 0)["errors"]
    assert current(world).state == "fulfilled" and len(world.rows("clinical_fact")) == 1


def test_danger_precedes_fulfillment_transaction(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor(world, count=1)
    commit = world.store.commit
    observed = []

    def inspect(request: CommitRequest) -> CommitResult:
        if request.command.payload.get("type") == "RecordPatientReply":
            assert world.rows("incident")
            assert {"mission", "clinical_fact", "review"} <= {r.entity_type for r in request.puts}
            observed.append(True)
        return commit(request)

    monkeypatch.setattr(world.store, "commit", inspect)
    world.post(update(PATIENT, "BP 190/125", 1100))
    assert observed and current(world).state == "fulfilled" and current(world).danger_history
    assert len(world.rows("incident")) == 1
    assert not any(
        r.body.get("notification_purpose") == "DONE:FULFILLMENT"
        for r in world.rows("outbound_intent")
    )
    assert world.receipt(1100).state == "completed"


def test_danger_keeps_the_ambiguous_schedule_choice(world: PatientWorld) -> None:
    monitor(world, "first", count=1)
    monitor(world, "second", count=1)
    _, choice = world.send("BP 190/125")
    assert world.rows("incident") and "Which monitoring schedule" in str(choice.payload)
    world.press(choice)
    assert sorted(current(world, id).state for id in ("first", "second")) == ["fulfilled", "open"]
    assert len(world.rows("incident")) == 1


@pytest.mark.parametrize("action", ["confirm_identity", "reject"])
def test_photo_identity_does_not_fill_until_doctor_confirms(
    world: PatientWorld, action: str
) -> None:
    from sanad.evidence.doctor import decide

    monitor(world, count=1)
    doc = f.lab(
        printed_name=None,
        items=[{"name": "BP", "value": "130/85", "unit": "mmHg"}],
        document_type="other",
    )
    vision, _, _ = f.providers(world, doc, doc)
    assert f.upload(world, caption="BP monitor screen") == "accepted"
    e = f.current(world)
    assert e.identity_pending and e.association_state == "accepted_pending_identity"
    assert current(world).state == "open" and not current(world).details.readings  # type: ignore[union-attr]
    result = decide(
        world.runtime.steward,
        world.owner,
        e,
        action,
        "paper-decision",
        mission_id=e.mission_id,
        reason="Synthetic identity check",
    )
    assert result.status == "accepted"
    m = current(world)
    assert (m.state == "fulfilled") == (action == "confirm_identity")
    assert bool(m.evidence_refs) == (action == "confirm_identity")
    assert len(vision.calls) == 2


@pytest.mark.parametrize("case", ["disagreement", "bad_unit", "missing_unit", "implausible"])
def test_photo_uncertainty_keeps_slots_missing(world: PatientWorld, case: str) -> None:
    monitor(world, count=1)
    doc = f.lab(
        items=[
            {
                "name": "BP",
                "value": "300/280" if case == "implausible" else "130/85",
                "unit": "kg" if case == "bad_unit" else None if case == "missing_unit" else "mmHg",
            }
        ],
        document_type="other",
    )
    f.providers(world, doc, doc.replace("130/85", "140/90") if case == "disagreement" else doc)
    assert f.upload(world, caption="BP monitor screen") == "accepted"
    assert current(world).state == "open" and not current(world).details.readings  # type: ignore[union-attr]
    assert any(r.body["review_kind"] == "evidence_association" for r in world.rows("review"))


def test_photo_choice_matches_metric_and_is_single_use(world: PatientWorld) -> None:
    monitor(world, "first", count=1)
    monitor(world, "second", count=1)
    monitor(world, "weight", count=1, name="weight", unit="kg")
    doc = f.lab(items=[{"name": "BP", "value": "130/85", "unit": "mmHg"}], document_type="other")
    f.providers(world, doc, doc)
    assert f.upload(world, caption="BP monitor screen") == "accepted"
    choice = next(i for i in world.patient_intents() if i.template_id == "patient_evidence_which")
    assert "Monitor weight" not in str(choice.payload)
    world.press(choice)
    assert sorted(current(world, id).state for id in ("first", "second")) == ["fulfilled", "open"]
    assert current(world, "weight").state == "open"
    before = f.current(world)
    world.press(choice, id=2001)
    assert f.current(world) == before


@pytest.mark.parametrize("completed", [False, True])
def test_explicit_wrong_metric_photo_choice_is_not_redirected(
    world: PatientWorld, completed: bool
) -> None:
    from sanad.evidence.doctor import decide

    monitor(world, "bp", count=1 if completed else 2)
    monitor(world, "weight", name="weight", unit="kg")
    doc = f.lab(items=[{"name": "BP", "value": "130/85", "unit": "mmHg"}], document_type="other")
    f.providers(world, doc, doc)
    assert f.upload(world, caption="BP monitor screen") == "accepted"
    e = f.current(world)
    assert e.mission_id == "bp"
    assert current(world, "bp").state == ("fulfilled" if completed else "open")
    before = world.rows("mission")
    result = decide(
        world.runtime.steward,
        world.owner,
        e,
        "associate",
        "wrong-metric-choice",
        mission_id="weight",
    )
    if completed:
        assert result.reason_code == "correction_requires_slice19"
        assert f.current(world) == e and world.rows("mission") == before
        return
    assert result.status == "accepted"
    changed = f.current(world)
    assert changed.mission_id == "weight" and changed.association_state == "candidate"
    assert changed.required_predicate_results[0].missing == ("monitor_metric",)
    assert world.rows("mission") == before
    assert "130/85" not in doctor_table(
        world.store, world.patient_scope, current(world, "bp"), "en"
    )


def test_rejected_partial_photo_cannot_complete_later_coverage(world: PatientWorld) -> None:
    from sanad.evidence.doctor import decide

    monitor(world)
    doc = f.lab(items=[{"name": "BP", "value": "130/85", "unit": "mmHg"}], document_type="other")
    f.providers(world, doc, doc)
    assert f.upload(world, caption="BP monitor screen") == "accepted"
    e = f.current(world)
    assert (
        decide(
            world.runtime.steward,
            world.owner,
            e,
            "reject",
            "reject-partial",
            reason="Wrong patient",
        ).status
        == "accepted"
    )
    world.clock.advance(timedelta(hours=6))
    world.send("BP 135/85")
    m = current(world)
    assert m.state == "open" and isinstance(m.details, MonitorDetails)
    assert set(filled(m.details)) == {1}
    assert "missing" in doctor_table(world.store, world.patient_scope, m, "en")


@pytest.mark.parametrize("photo", [False, True])
def test_patient_alert_stricter_than_floor_keeps_provenance(
    world: PatientWorld, photo: bool
) -> None:
    from sanad.scribe.records import CareOrderVersion, ValueAlert

    monitor(world, count=1)
    f.alert(world)
    old = from_record(world.rows("care_order_version")[0], CareOrderVersion)
    world.seed(
        old.model_copy(
            update={
                "structured_instruction": ValueAlert(
                    text="Notify at systolic 150 mmHg",
                    metric="systolic",
                    comparator="ge",
                    threshold="150",
                    unit="mmHg",
                )
            }
        )
    )
    if photo:
        doc = f.lab(
            items=[{"name": "BP", "value": "150/90", "unit": "mmHg"}], document_type="other"
        )
        f.providers(world, doc, doc)
        assert f.upload(world, caption="BP monitor screen") == "accepted"
    else:
        world.post(update(PATIENT, "BP 150/90", 1100))
    assert current(world).state == "fulfilled" and current(world).danger_history
    assert len(world.rows("incident")) == 1
    text = doctor_table(world.store, world.patient_scope, current(world), "en")
    assert "patient_alert:synthetic-alert" in text and "systolic ge 150 mmHg" in text


@pytest.mark.parametrize("photo", [False, True])
def test_late_partial_reading_handles_deadline_before_final_completion(
    world: PatientWorld, photo: bool
) -> None:
    now = world.clock()
    due = now + timedelta(hours=1)
    monitor(
        world,
        due_at=due,
        escalation_at=due,
        work_clock=WorkClock(next_action_at=due, work_lane="mission"),
    )
    world.clock.advance(timedelta(hours=2))
    if photo:
        doc = f.lab(
            items=[{"name": "BP", "value": "125/85", "unit": "mmHg"}], document_type="other"
        )
        f.providers(world, doc, doc)
        assert f.upload(world, caption="BP monitor screen") == "accepted"
    else:
        world.send("BP 125/85")
    m = current(world)
    assert m.state != "fulfilled" and m.handled_deadline_generation == m.deadline_generation
    deadline = next(
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body.get("notification_purpose") == "DEADLINE"
    )
    text = str(doctor_payload(world.store, deadline))
    assert "125/85" in text and "missing" in text and "stable" not in text
    world.clock.advance(timedelta(hours=4))
    world.send("BP 130/85")
    assert current(world).state == "fulfilled" and current(world).timeliness == "late"
    assert current(world).due_at == due


def test_stopped_patient_suppresses_prompts_but_not_missing_deadline(world: PatientWorld) -> None:
    from sanad.contact.scheduler import schedule
    from sanad.steward.service import system_command

    due = world.clock() + timedelta(hours=8)
    monitor(world, due_at=due, escalation_at=due)
    world.send("BP 120/80")
    assert (
        schedule(world.runtime.steward, to_record(current(world), world.patient_scope)).status
        == "accepted"
    )
    assert not any(i.slot_id == "monitor:monitor:0" for i in world.patient_intents())
    world.send("stop messages")
    assert not world.profile.routine_contact_enabled
    world.clock.advance(timedelta(hours=6))
    assert (
        schedule(world.runtime.steward, to_record(current(world), world.patient_scope)).status
        == "accepted"
    )
    assert not any(i.slot_id == "monitor:monitor:1" for i in world.patient_intents())
    world.clock.advance(timedelta(hours=2))
    result = world.runtime.steward.handle(
        system_command(
            world.patient_scope,
            "stopped-deadline",
            {"type": "_Deadline", "mission_id": "monitor"},
            world.clock(),
            lane="mission",
        )
    )
    assert result.status == "accepted" and current(world).state == "overdue"
    notice = next(
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body.get("notification_purpose") == "DEADLINE"
    )
    assert "120/80" in str(doctor_payload(world.store, notice))
    assert "missing" in str(doctor_payload(world.store, notice))


def test_five_day_fixture_card_prompt_reply_and_done(world: PatientWorld, tmp_path: "Path") -> None:
    import json
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from sanad.contact.scheduler import schedule
    from sanad.scribe.card import render_card

    world.clock.now = datetime(2026, 9, 6, 20, 50, tzinfo=UTC)
    spoken = "Blood pressure chart, three times a day for five days"
    proposal = world.dictate(
        "Synthetic Patient: " + spoken,
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "missions": [{"kind": "TASK", "text": spoken}],
        },
    )
    assert not proposal.issues
    card = next(
        line for part in render_card(proposal) for line in part.splitlines() if "MONITOR:" in line
    )
    assert card.startswith(
        "MONITOR: blood pressure, 3 times a day for 5 days (15 readings, first Mon 08:00)"
    )
    world.tap("✅ Confirm")
    m = from_record(world.rows("mission")[0], Mission)
    assert isinstance(m.details, MonitorDetails) and len(m.details.slots) == 15
    world.send("BP 110/70")  # outside every confirmed slot, retained as an extra
    slots = m.details.slots
    world.clock.now = slots[0]
    current_m = current(world, m.id)
    assert (
        schedule(world.runtime.steward, to_record(current_m, world.patient_scope)).status
        == "accepted"
    )
    prompt = next(i for i in world.patient_intents() if i.template_id == "patient_monitor_prompt")
    from sanad.contact.delivery import payload

    prompt_text = payload(world.store, prompt)["text"]
    replies = []
    for i, at in enumerate(slots):
        world.clock.now = at
        _, reply = world.send(f"BP {120 + i}/80")
        replies.append(str((reply.payload or {})["text"]))
    completed = current(world, m.id)
    assert completed.state == "fulfilled" and completed.timeliness == "on_time"
    notice = next(
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body.get("notification_purpose") == "DONE:FULFILLMENT"
    )
    done = str(doctor_payload(world.store, notice)["text"])
    assert "Extra readings: 1" in done and "range 120–134 mmHg" in done
    assert "14 readings left" in replies[0] and "0 readings left" in replies[-1]
    (tmp_path / "monitor-example.json").write_text(
        json.dumps(
            {
                "confirmed_at": proposal.created_at.astimezone(
                    ZoneInfo("Africa/Cairo")
                ).isoformat(),
                "card": card,
                "prompt": prompt_text,
                "reply": replies[0],
                "done": done,
                "slots": [
                    [at.astimezone(ZoneInfo("Africa/Cairo")).isoformat(), at.isoformat()]
                    for at in slots
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@pytest.mark.parametrize("language", ["en", "ar"])
def test_maximum_schedule_record_and_full_notice(world: PatientWorld, language: str) -> None:
    from sanad.concierge.records import ReportFactPayload
    from sanad.concierge.reports import reading
    from sanad.domain import Provenance
    from sanad.monitor.slots import attach
    from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as safety
    from sanad.scribe.monitoring import compile_schedule

    world.seed(revise(world.doctor, world.clock(), language=language))
    compiled = compile_schedule("Measure blood pressure 4 times a day for 30 days")
    assert compiled
    d = compiled.details(world.clock(), "Africa/Cairo")
    original = monitor(world)
    world.clock.now = d.slots[-1]
    for i, at in enumerate(d.slots[:-1]):
        fact = ClinicalFact(
            id=f"retained-monitor-reading-{i}",
            scope=world.patient_scope,
            created_at=at,
            updated_at=at,
            category="patient_report",
            visibility="patient_released",
            payload=ReportFactPayload(
                report_kind="reading",
                text="BP 120/80",
                readings=reading("BP 120/80", safety).values,
            ),
            provenance=Provenance(
                source_observation_id=f"synthetic-receipt-{i}",
                actor_kind="patient",
                actor_id=PATIENT,
                source_kind="patient_report",
                received_at=at,
            ),
        )
        world.seed(fact)
        d = attach(
            d, reading("BP 120/80", safety).values, to_record(fact, world.patient_scope).ref, at, at
        )
    due = world.clock() + timedelta(days=1)
    world.seed(original.model_copy(update={"details": d, "due_at": due, "escalation_at": due}))
    world.send("BP 130/85")
    assert current(world).state == "fulfilled" and len(current(world).details.readings) == 120  # type: ignore[union-attr]
    notice = next(
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body.get("notification_purpose") == "DONE:FULFILLMENT"
    )
    text = str(doctor_payload(world.store, notice)["text"])
    assert len(text) < 4096 and sum(" | " in line for line in text.splitlines()) == 120
    assert "130/85" in text and "120–130 mmHg" in text


@pytest.mark.parametrize("case", ["midnight", "timezone", "explicit_past"])
def test_confirmed_schedule_cannot_move_from_the_card(world: PatientWorld, case: str) -> None:
    from datetime import UTC, datetime

    world.clock.now = datetime(2026, 9, 6, 20, 50, tzinfo=UTC)
    if case == "timezone":
        patient = from_record(world.rows("patient")[0], Patient)
        world.seed(revise(patient, world.clock(), timezone="America/New_York"))
    text = "Measure pulse 2 times a day for 5 days" + (
        " starting today" if case == "explicit_past" else ""
    )
    proposal = world.dictate(
        "Synthetic Patient: " + text,
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "missions": [{"kind": "TASK", "text": text}],
        },
    )
    assert not proposal.issues
    if case == "midnight":
        world.clock.advance(timedelta(minutes=15))
    world.tap("✅ Confirm")
    missions = world.rows("mission")
    if case == "explicit_past":
        assert len(missions) == 1
        m = from_record(missions[0], Mission)
        assert isinstance(m.details, MonitorDetails) and m.details.slots[0] < world.clock()
        assert not m.details.readings
    else:
        persisted = world.scribe.repo.load(
            proposal.scope, "scribe_proposal", proposal.id, type(proposal)
        )
        assert not missions and persisted and persisted.status == "rejected"
        assert any(i.template_id == "scribe_stale" for i in world.cards())
