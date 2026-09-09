"""16b outcomes through the actual scheduler, Steward, store and delivery gateway."""

import json
from datetime import timedelta
from threading import Event
from time import monotonic
from typing import Any

import pytest
from domain_fixtures import NOW
from harness import FakeClock, SimulatedCrash
from providers.fixtures import ScriptedModel, response

from sanad.auth.service import revise
from sanad.contact.delivery import payload
from sanad.contact.ladder import ContactPlan
from sanad.coordinator import agent, policy, templates
from sanad.coordinator.permitted import compute
from sanad.domain import (
    EvidencePredicate,
    Mission,
    MissionKind,
    MissionState,
    PatientReportPredicate,
)
from sanad.domain.entities import MonitorDetails, SendRecordsDetails, TaskDetails, VisitDetails
from sanad.store._base import StoreBase
from sanad.store.records import CommitRequest, CommitResult, Patient, from_record
from store import contact_fixtures as f
from store.concierge_fixtures import PatientWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> PatientWorld:
    clock.now = NOW.replace(hour=11)
    w = f.world(store, clock)
    patient = from_record(
        f.required(store.get(w.patient_scope, "patient", w.patient_scope.patient_id)), Patient
    )
    w.seed(revise(patient, clock(), language="en"))
    return w


def monitoring(w: PatientWorld, count: int = 2) -> Mission:
    m = f.add_mission(
        w,
        kind=MissionKind.MONITOR,
        due=w.clock() + timedelta(days=6),
        details=MonitorDetails(
            metric="blood pressure",
            unit="mmHg",
            slots=tuple(w.clock() + timedelta(hours=6 * i) for i in range(count)),
            required_coverage=count,
        ),
        objective_predicate=EvidencePredicate(evaluator="monitor"),
    )
    m = revise(m, w.clock(), title="Blood pressure chart")
    w.seed(m)
    return m


def patient(w: PatientWorld) -> Patient:
    return from_record(
        f.required(w.store.get(w.patient_scope, "patient", w.patient_scope.patient_id)), Patient
    )


def chosen(kwargs: dict[str, Any]) -> dict[str, Any]:
    prompt = json.loads(kwargs["messages"][-1]["content"][0]["text"])
    move = prompt["moves"][-1]
    return {"move": move["id"], "fact_ids": move["fact_ids"]}


def provider(monkeypatch: pytest.MonkeyPatch, script: Any) -> ScriptedModel:
    model = ScriptedModel(script)
    monkeypatch.setattr(agent, "model_factory", lambda registry, role: model)
    return model


def reasons(w: PatientWorld) -> list[str]:
    return [
        str(r.body["event_type"]).removeprefix("COORDINATOR_REFUSED:")
        for r in w.rows("audit_event")
        if str(r.body["event_type"]).startswith("COORDINATOR_REFUSED:")
    ]


@pytest.mark.parametrize("language", ["en", "ar"])
def test_monitor_names_two_slots_and_delivers(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    w = world
    w.seed(revise(patient(w), w.clock(), language=language))
    m = monitoring(w)
    if language == "ar":
        m = revise(m, w.clock(), title="جدول قياس الضغط")
        w.seed(m)
    model = provider(monkeypatch, lambda kwargs: response(json.dumps(chosen(kwargs))))
    intents = f.plan(w, m)
    assert len(intents) == 1
    intent = intents[0]
    text = str(payload(w.store, intent)["text"])
    assert not reasons(w), reasons(w)
    assert "2026-09-06 14:00" in text and "2026-09-06 20:00" in text
    assert ("Readings not yet recorded" if language == "en" else "قراءات لسه مش مسجلة") in text
    assert len(model.script.calls) == 1 and not reasons(w)
    assert f.current(w, m).coordinator_choice is not None
    assert intent.slot_id == f"monitor:{m.id}:0" and intent.contact_kind == "scheduled"
    assert intent.expires_at == w.clock() + timedelta(hours=2)
    assert f.dispatch(w, intent).status == "provider_accepted"


@pytest.mark.parametrize(
    "case,expected",
    [
        ("unknown_move", "unknown_move"),
        ("barrier", "unknown_move"),
        ("foreign_fact", "unknown_fact"),
        ("invented_instant", "invalid_shape"),
        ("clinical_sentence", "invalid_shape"),
        ("prose", "invalid_shape"),
        ("duplicate_json", "invalid_shape"),
        ("empty", "empty_proposal"),
        ("empty_text", "empty_proposal"),
        ("pause", "pause_redundant"),
        ("duplicate", "duplicate_fact"),
        ("missing", "incomplete_facts"),
        ("provider_refusal", "provider_refusal"),
        ("provider_error", "provider_error"),
    ],
)
def test_refusal_is_durable_and_original_template_delivers(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch, case: str, expected: str
) -> None:
    w, m = world, monitoring(world)

    def script(kwargs: dict[str, Any]) -> dict[str, Any]:
        raw = chosen(kwargs)
        if case in {"unknown_move", "barrier", "pause"}:
            raw["move"] = {
                "unknown_move": "send_message",
                "barrier": "classify_barrier",
                "pause": "pause_mission",
            }[case]
        elif case == "foreign_fact":
            raw["fact_ids"][0] = "another-mission:slot:0"
        elif case == "invented_instant":
            raw["at"] = "2026-09-06T12:00:00Z"
        elif case == "clinical_sentence":
            raw["sentence"] = "Your blood pressure is normal; double the dose."
        elif case == "prose":
            return response("Your pressure is normal. " + json.dumps(raw))
        elif case == "duplicate_json":
            return response(
                '{"move":"pause_mission","move":"request_missing_evidence","fact_ids":[]}'
            )
        elif case == "empty":
            raw = {}
        elif case == "empty_text":
            return response("")
        elif case == "duplicate":
            raw["fact_ids"].append(raw["fact_ids"][0])
        elif case == "missing":
            raw["fact_ids"].pop()
        elif case == "provider_refusal":
            raw = {"refused": True}
        elif case == "provider_error":
            raise RuntimeError("synthetic provider outage")
        return response(json.dumps(raw))

    model = provider(monkeypatch, script)
    intent = f.plan(w, m)[0]
    assert reasons(w) == [expected]
    assert f.current(w, m).coordinator_choice is None
    assert (
        payload(w.store, intent)["text"]
        == "It is time to measure blood pressure; send the number as shown."
    )
    assert len(model.script.calls) == 1
    assert f.dispatch(w, intent).status == "provider_accepted"


def test_fifteen_slots_cap_three_and_twelve_more(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = monitoring(world, 15)
    model = provider(monkeypatch, lambda kwargs: response(json.dumps(chosen(kwargs))))
    intent = f.plan(world, m)[0]
    text = str(payload(world.store, intent)["text"])
    assert "and 12 additional items" in text
    assert "2026-09-06 14:00" in text and "2026-09-07 02:00" in text
    assert "2026-09-07 08:00" not in text
    assert len(model.script.calls) == 1


def test_schedule_move_cannot_adjust_any_instant(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = monitoring(world)

    def script(kwargs: dict[str, Any]) -> dict[str, Any]:
        prompt = json.loads(kwargs["messages"][-1]["content"][0]["text"])
        move = prompt["moves"][0]
        return response(
            json.dumps({"move": move["id"], "fact_ids": list(reversed(move["fact_ids"]))})
        )

    provider(monkeypatch, script)
    intent = f.plan(world, m)[0]
    text = str(payload(world.store, intent)["text"])
    assert text.startswith("Due:") and "Readings not yet recorded" not in text
    current = f.current(world, m)
    assert current.next_contact_at == m.details.slots[0]  # type: ignore[union-attr]
    assert current.due_at == m.due_at and current.escalation_at == m.escalation_at
    assert intent.slot_id == "monitor:test:0"


def test_replay_tick_does_not_reason_or_send_twice(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = monitoring(world)
    model = provider(monkeypatch, lambda kwargs: response(json.dumps(chosen(kwargs))))
    f.tick(world)
    assert len(model.script.calls) == 1
    assert len(f.routine(world)) == 1
    assert f.routine(world)[0].status == "provider_accepted"
    f.tick(world)
    assert len(model.script.calls) == 1
    assert len(f.routine(world)) == 1
    assert f.current(world, m).contact_count == 1


def test_crash_after_proposal_recovers_template_without_second_call(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, m = world, monitoring(world)
    model = provider(monkeypatch, lambda kwargs: response(json.dumps(chosen(kwargs))))
    commit = w.store.commit

    def crash(request: CommitRequest) -> CommitResult:
        if any(r.body["event_type"] == "COORDINATOR_SELECTED" for r in request.events):
            raise SimulatedCrash("before contact commit")
        return commit(request)

    with monkeypatch.context() as patch:
        patch.setattr(w.store, "commit", crash)
        with pytest.raises(SimulatedCrash):
            f.plan(w, m)
    assert f.current(w, m) == m and not f.routine(w)
    assert len(model.script.calls) == 1
    intent = f.plan(w, m)[0]
    assert reasons(w) == ["attempt_already_started"]
    assert len(model.script.calls) == 1
    assert (
        payload(w.store, intent)["text"]
        == "It is time to measure blood pressure; send the number as shown."
    )
    assert f.dispatch(w, intent).status == "provider_accepted"


def test_never_returning_provider_has_six_second_bound(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, m = world, monitoring(world)
    release = Event()

    def blocked(kwargs: dict[str, Any]) -> dict[str, Any]:
        release.wait(20)
        return response(json.dumps(chosen(kwargs)))

    model = provider(monkeypatch, blocked)
    start = monotonic()
    try:
        intent = f.plan(w, m)[0]
        assert 5.5 <= monotonic() - start < 8
        assert reasons(w) == ["provider_timeout"]
        assert f.current(w, m).coordinator_choice is None
        assert f.dispatch(w, intent).status == "provider_accepted"
        assert len(model.script.calls) == 1
    finally:
        release.set()


@pytest.mark.parametrize("gate", ["output_validation", "safety_kernel"])
def test_output_gate_refusal_preserves_contact(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    w, m = world, monitoring(world)
    provider(monkeypatch, lambda kwargs: response(json.dumps(chosen(kwargs))))
    if gate == "output_validation":
        monkeypatch.setattr(templates, "patient_failure", lambda *args: "unsafe_output")
    else:
        from sanad.safety import screen_text as real_screen

        monkeypatch.setattr(
            templates,
            "screen_text",
            lambda *args, **kwargs: real_screen("chest pain", policy=kwargs["policy"]),
        )
    intent = f.plan(w, m)[0]
    assert reasons(w) == [gate]
    assert f.dispatch(w, intent).status == "provider_accepted"


@pytest.mark.parametrize("change", ["stop", "terminal"])
def test_send_time_authority_still_refuses(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    w, m = world, monitoring(world)
    provider(monkeypatch, lambda kwargs: response(json.dumps(chosen(kwargs))))
    intent = f.plan(w, m)[0]
    if change == "stop":
        w.send("stop reminders")
    else:
        w.seed(revise(f.current(w, m), w.clock(), state=MissionState.cancelled, work_clock=None))
    assert f.dispatch(w, intent).status == "suppressed"


def test_patient_language_is_fresh_and_doctor_change_does_not_rebind(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, m = world, monitoring(world)
    m = revise(m, w.clock(), title="جدول الضغط")
    w.seed(m)
    # Select with an Arabic title and Arabic patient so the initial gate passes.
    w.seed(revise(patient(w), w.clock(), language="ar"))
    provider(monkeypatch, lambda kwargs: response(json.dumps(chosen(kwargs))))
    intent = f.plan(w, m)[0]
    assert "قراءات" in str(payload(w.store, intent)["text"])
    w.seed(revise(w.doctor, w.clock(), language="en"))
    assert "قراءات" in str(payload(w.store, intent)["text"])
    w.seed(revise(patient(w), w.clock(), language="en"))
    assert "Readings" in str(payload(w.store, intent)["text"])


@pytest.mark.parametrize("kind", ["TEST", "SEND_RECORDS", "VISIT", "TASK", "MONITOR"])
def test_permitted_choices_use_each_executor_requirements(world: PatientWorld, kind: str) -> None:
    w = world
    m = monitoring(w)
    if kind == "TEST":
        m = f.add_mission(w, id="labs")
    elif kind == "SEND_RECORDS":
        m = f.add_mission(
            w,
            id="records",
            kind=MissionKind.SEND_RECORDS,
            details=SendRecordsDetails(categories=("lab_result", "prescription"), required_count=2),
            objective_predicate=EvidencePredicate(evaluator="send_records"),
        )
    elif kind == "VISIT":
        m = f.add_mission(
            w,
            id="visit",
            kind=MissionKind.VISIT,
            details=VisitDetails(objective="attendance_reported"),
            objective_predicate=PatientReportPredicate(report_kind="visit_attendance"),
        )
    elif kind == "TASK":
        m = f.add_mission(
            w,
            id="task",
            kind=MissionKind.TASK,
            details=TaskDetails(
                category="administrative",
                instruction="Send your appointment details",
                completion_rule="patient_report",
            ),
            objective_predicate=PatientReportPredicate(report_kind="task_done"),
        )
    plan = ContactPlan(w.clock(), "fixture-slot", w.clock() + timedelta(hours=3))
    bundle = compute(
        w.store,
        m,
        patient(w),
        plan,
        "patient_monitor_prompt" if kind == "MONITOR" else "patient_chase_" + kind.lower(),
    )
    assert [move.id for move in bundle.moves] == [
        "schedule_next_contact",
        "request_missing_evidence",
    ]
    assert bundle.first_contact
    assert [f.kind for f in bundle.facts[:2]] == ["title", "due"]
    expected = {
        "TEST": "category",
        "SEND_RECORDS": "category",
        "VISIT": "visit",
        "TASK": "task",
        "MONITOR": "slot",
    }
    assert bundle.facts[2].kind == expected[kind]


def test_single_move_short_circuit_and_empty_set(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, m = world, monitoring(world)
    model = provider(monkeypatch, AssertionError("no call permitted"))
    plan = ContactPlan(w.clock(), "fixture", w.clock() + timedelta(hours=3))
    brief = compute(w.store, m, patient(w), plan, "patient_visit_brief")
    assert brief.pre_visit_brief and len(brief.moves) == 1
    empty = compute(w.store, m, patient(w), None, "patient_monitor_prompt")
    assert not empty.moves and empty.reason == "no_contact_plan"
    assert not model.script.calls


def test_tool_request_cannot_start_a_loop(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, m = world, monitoring(world)
    model = provider(monkeypatch, response(calls=[("request_missing_evidence", {})]))
    intent = f.plan(w, m)[0]
    assert reasons(w) == ["tool_loop"]
    assert len(model.script.calls) == 1
    assert f.dispatch(w, intent).status == "provider_accepted"


def test_single_choice_medication_and_day_three_never_call_model(
    store: StoreBase,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sanad.domain import FollowUpTask

    clock.now = NOW.replace(hour=7)
    w = f.world(store, clock, medication=True)
    model = provider(monkeypatch, AssertionError("single choice must not call"))
    w.send("بدأت الدوا")
    task = from_record(w.rows("followup")[0], FollowUpTask)
    assert task.prompt_at is not None
    clock.now = task.prompt_at
    f.tick(w)
    assert any(
        i.template_id == "patient_day3_prompt" and i.status == "provider_accepted"
        for i in f.routine(w)
    )
    assert model.script.calls == []


def test_previsit_brief_has_one_move_and_keeps_template(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    m = f.add_mission(
        w,
        kind=MissionKind.VISIT,
        due=w.clock() + timedelta(days=1),
        details=VisitDetails(objective="attendance_reported"),
        objective_predicate=PatientReportPredicate(report_kind="visit_attendance"),
    )
    model = provider(monkeypatch, AssertionError("brief has no choice"))
    intent = f.plan(w, m)[0]
    assert intent.template_id == "patient_visit_brief"
    assert "requested visit" in str(payload(w.store, intent)["text"])
    assert model.script.calls == []


def test_empty_set_is_durable_no_call_and_no_send(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, m = world, monitoring(world)
    assert isinstance(m.details, MonitorDetails)
    m = revise(
        m,
        w.clock(),
        details=m.details.model_copy(
            update={"slots": tuple(t + timedelta(days=1) for t in m.details.slots)}
        ),
    )
    w.seed(m)
    model = provider(monkeypatch, AssertionError("no plan may not call"))
    assert f.plan(w, m) == []
    assert "no_contact_plan" in reasons(w)
    assert model.script.calls == []


def test_stale_proposal_after_terminal_is_a_durable_refusal(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, m = world, monitoring(world)

    def terminal(kwargs: dict[str, Any]) -> dict[str, Any]:
        w.seed(revise(m, w.clock(), state=MissionState.cancelled, work_clock=None))
        return response(json.dumps(chosen(kwargs)))

    model = provider(monkeypatch, terminal)
    from sanad.contact.scheduler import schedule
    from sanad.store.records import to_record

    result = schedule(w.runtime.steward, to_record(m, w.patient_scope))
    assert result.status == "stale_version"
    assert f.current(w, m).state == "cancelled" and not f.routine(w)
    assert "stale_source" in reasons(w)
    assert len(model.script.calls) == 1


def test_first_and_chase_metadata_are_code_owned(world: PatientWorld) -> None:
    w, m = world, monitoring(world)
    plan = ContactPlan(w.clock(), "monitor:test:0", w.clock() + timedelta(hours=2))
    assert compute(w.store, m, patient(w), plan, "patient_monitor_prompt").first_contact
    chase = revise(m, w.clock(), contact_count=1, state=MissionState.waiting_patient)
    assert not compute(w.store, chase, patient(w), plan, "patient_monitor_prompt").first_contact
    assert policy.OWNER_REVIEW_PENDING
    assert (
        policy.call_timeout_s,
        policy.max_turns,
        policy.max_named_items,
        policy.min_choice_size,
    ) == (6, 1, 3, 2)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("foreign", "scope_mismatch"),
        ("terminal", "mission_ineligible"),
        ("unsupported", "unsupported_contact"),
    ],
)
def test_closed_permitted_set_defensive_refusals(
    world: PatientWorld, case: str, reason: str
) -> None:
    from sanad.domain import PatientScope

    w, m = world, monitoring(world)
    p = patient(w)
    if case == "foreign":
        p = p.model_copy(update={"scope": PatientScope(doctor_id="other", patient_id="other")})
    elif case == "terminal":
        m = revise(m, w.clock(), state=MissionState.cancelled, work_clock=None)
    else:
        m = revise(
            m,
            w.clock(),
            kind=MissionKind.TASK,
            details=TaskDetails(
                category="other", instruction="Book for me", completion_rule="unsupported_action"
            ),
        )
    bundle = compute(
        w.store,
        m,
        p,
        ContactPlan(w.clock(), "slot", w.clock() + timedelta(hours=2)),
        "patient_monitor_prompt",
    )
    assert not bundle.moves and bundle.reason == reason


def test_unanchored_day_three_has_no_permitted_move(store: StoreBase, clock: FakeClock) -> None:
    from sanad.domain import FollowUpTask

    clock.now = NOW.replace(hour=7)
    w = f.world(store, clock, medication=True)
    task = from_record(w.rows("followup")[0], FollowUpTask)
    bundle = compute(
        w.store,
        task,
        patient(w),
        ContactPlan(w.clock(), task.id, w.clock() + timedelta(hours=2)),
        "patient_day3_prompt",
    )
    assert not bundle.moves and bundle.reason == "followup_ineligible"


@pytest.mark.parametrize("language", ["ar", "en"])
def test_all_document_categories_render_from_code(world: PatientWorld, language: str) -> None:
    w = world
    p = patient(w).model_copy(update={"language": language})
    m = f.add_mission(
        w,
        kind=MissionKind.SEND_RECORDS,
        details=SendRecordsDetails(
            categories=("lab_result", "prescription", "medication_list", "imaging_report"),
            required_count=4,
        ),
        objective_predicate=EvidencePredicate(evaluator="send_records"),
    )
    m = revise(m, w.clock(), title="الورق المطلوب" if language == "ar" else "Requested papers")
    bundle = compute(
        w.store,
        m,
        p,
        ContactPlan(w.clock(), "slot", w.clock() + timedelta(hours=2)),
        "patient_chase_send_records",
    )
    text = templates.render(bundle, bundle.moves[-1].fact_ids, p)
    assert ("و1 كمان" if language == "ar" else "and 1 additional item") in text
    assert ("قائمة الأدوية" if language == "ar" else "medication list") in text
    assert ("تقرير الأشعة" if language == "ar" else "imaging report") not in text


def test_actual_clinical_fragment_fails_output_validator(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, m = world, monitoring(world)
    bundle = compute(
        w.store,
        m,
        patient(w),
        ContactPlan(w.clock(), "slot", w.clock() + timedelta(hours=2)),
        "patient_monitor_prompt",
    )
    from sanad.presentation.coordinator import CATALOG

    monkeypatch.setitem(
        CATALOG,
        "coordinator.title",
        {"ar": "كل حاجة طبيعية {value}", "en": "You are fine. {value}"},
    )
    with pytest.raises(ValueError, match="output_validation"):
        templates.render(bundle, bundle.moves[-1].fact_ids, patient(w))


def test_received_category_is_removed_using_accepted_predicate(world: PatientWorld) -> None:
    from store import evidence_fixtures as ef

    w = world
    m = f.add_mission(
        w,
        kind=MissionKind.SEND_RECORDS,
        details=SendRecordsDetails(categories=("lab_result", "prescription"), required_count=2),
        objective_predicate=EvidencePredicate(evaluator="send_records"),
    )
    ef.providers(w, ef.lab(), ef.lab())
    ef.upload(w)
    m = f.current(w, m)
    bundle = compute(
        w.store,
        m,
        patient(w),
        ContactPlan(w.clock(), "slot", w.clock() + timedelta(hours=2)),
        "patient_chase_send_records",
    )
    assert [fact.value for fact in bundle.facts if fact.kind == "category"] == ["prescription"]


def test_short_contact_window_uses_template_without_waiting(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, m = world, monitoring(world)
    w.clock.advance(timedelta(hours=2) - timedelta(seconds=3))
    model = provider(monkeypatch, AssertionError("budget cannot fit this window"))
    intent = f.plan(w, m)[0]
    assert "contact_window_short" in reasons(w)
    assert model.script.calls == []
    assert f.dispatch(w, intent).status == "provider_accepted"


def test_attempt_checkpoint_is_audit_only(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, m = world, monitoring(world)
    provider(monkeypatch, lambda kwargs: response(json.dumps(chosen(kwargs))))
    original = w.store.commit
    seen = []

    def inspect(request: CommitRequest) -> CommitResult:
        if any(r.body["event_type"] == "COORDINATOR_ATTEMPT_STARTED" for r in request.events):
            seen.append(request)
            assert not request.puts and not request.intents and request.receipt_completion is None
        return original(request)

    monkeypatch.setattr(w.store, "commit", inspect)
    f.plan(w, m)
    assert len(seen) == 1
