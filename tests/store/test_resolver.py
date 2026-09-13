"""Real authenticated receipts, durable attempts and both stores; no live services."""

from datetime import timedelta

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate

from sanad.auth.service import revise
from sanad.contact.delivery import doctor_payload
from sanad.domain import Mission, PatientScope
from sanad.domain.entities import BarrierAttempt
from sanad.resolver import templates
from sanad.resolver.policy import POLICY
from sanad.store._base import StoreBase
from sanad.store.records import (
    CommitRequest,
    CommitResult,
    Patient,
    from_record,
    to_record,
)
from store.account_fixtures import PATIENT, update
from store.medication_fixtures import snapshot
from store.resolver_fixtures import QUESTION, add, get, setup


@pytest.mark.parametrize("kind", ["MEDICATION", "TEST", "VISIT", "TASK", "MONITOR", "SEND_RECORDS"])
def test_each_mission_kind_has_durable_bounded_attempt(
    store: StoreBase, clock: FakeClock, kind: str
) -> None:
    w, capture = setup(store, clock)
    before = add(w, kind)
    observed = []

    def checkpoint(stage: str) -> None:
        if stage == "resolver_begin_persisted":
            attempt = get(w, before.id).barrier_attempts[-1]
            observed.append(
                (attempt.reasoning_spent, attempt.questions_spent, attempt.searches_spent)
            )
            assert w.receipt(1000).state == "processing"
            assert w.receipt(1000).processing_claim and w.receipt(1000).work_clock

    w.concierge.checkpoint = checkpoint
    model, reply = w.send("I cannot afford it", {"step": "ask_patient", "question": QUESTION})
    current = get(w, before.id)
    assert current.state == "blocked" and current.due_at == before.due_at
    assert current.resume_at == clock() + timedelta(days=1)
    assert observed == [(1, 1, 0)] and len(model.script.calls) == 1
    assert current.barrier_attempts[-1].question == QUESTION
    assert QUESTION in str(reply.payload) and not capture.requests


def test_test_barrier_question_three_places_and_doctor_payload(
    store: StoreBase, clock: FakeClock
) -> None:
    w, capture = setup(store, clock)
    before = add(w)
    _, question = w.send("The lab is too expensive", {"step": "ask_patient", "question": QUESTION})
    model, reply = w.send(
        "Synthetic Quarter, Cairo",
        {"step": "find_places", "question": "What practical difficulty do you need help with?"},
    )
    current = get(w)
    assert len(current.barrier_attempts) == 2
    first, second = current.barrier_attempts
    assert first.questions_spent == 1 and first.outcome == "asked"
    assert second.answered and second.searches_spent == 1 and second.questions_spent == 0
    assert second.area == "Synthetic Quarter, Cairo" and len(second.places) == 3
    assert current.state == "blocked" and current.due_at == before.due_at
    assert len(model.script.calls) == 1 and len(capture.requests) == 2
    text = str((reply.payload or {})["text"])
    assert templates.render("disclosure", "en") in text
    assert all(p.name in text for p in second.places)
    assert "Outside Radius" not in text
    from sanad.steward.apply import make_intent

    snap = snapshot(w)
    intent = make_intent(
        w.patient_scope,
        "synthetic-deadline",
        (to_record(current, w.patient_scope).ref,),
        "DEADLINE",
        "synthetic",
        clock(),
        w.runtime.steward.policy_provider(w.patient_scope),
        snap.authority,
        snap.profile,
        audience="doctor",
    )
    payload = doctor_payload(w.store, intent)
    assert payload
    assert 'Barrier: cost - "The lab is too expensive"' in str(payload["text"])
    assert "asked and answered" in str(payload["text"]) and "places offered" in str(payload["text"])
    assert "unresolved" in str(payload["text"]) and "0/1, 1/2" in str(payload["text"])
    assert "Which area" in str(question.payload)
    print(
        "RENDERED EXAMPLE\nBarrier: The lab is too expensive\n"
        + str((question.payload or {})["text"])
        + "\n"
        + text
        + "\nDoctor payload:\n"
        + str(payload["text"])
    )


@pytest.mark.parametrize("mode", ["rate", "empty", "timeout", "ambiguous", "first_fails"])
def test_provider_outcomes_and_one_durable_retry(
    store: StoreBase, clock: FakeClock, mode: str
) -> None:
    w, capture = setup(store, clock)
    add(w)
    if mode == "rate":
        capture.statuses = [429, 429]
    if mode == "empty":
        capture.places = {"elements": []}
    if mode == "timeout":
        capture.timeout = True
    if mode == "ambiguous":
        capture.geocode = [*capture.geocode, *capture.geocode]  # type: ignore[misc]
    if mode == "first_fails":
        capture.statuses = [503]
    spent = []

    def checkpoint(stage: str) -> None:
        if stage == "resolver_before_places_call":
            spent.append(get(w).barrier_attempts[-1].searches_spent)

    w.concierge.checkpoint = checkpoint
    w.send(
        "The lab is too expensive in Synthetic Quarter",
        {"step": "find_places", "question": "What practical difficulty do you need help with?"},
    )
    attempt = get(w).barrier_attempts[-1]
    assert spent == ([1] if mode == "ambiguous" else [1, 2])
    assert attempt.outcome == (
        "places_offered"
        if mode == "first_fails"
        else "area_ambiguous"
        if mode == "ambiguous"
        else "places_unavailable"
    )
    assert get(w).state == "blocked"


def test_repeated_barrier_appends_without_budget_or_pause_reset(
    store: StoreBase, clock: FakeClock
) -> None:
    w, capture = setup(store, clock)
    add(w)
    w.send("The lab is too expensive", {"step": "ask_patient", "question": QUESTION})
    old = get(w)
    clock.now += timedelta(hours=1)
    model, reply = w.send(
        "I still cannot afford the lab", {"step": "ask_patient", "question": "A second question?"}
    )
    current = get(w)
    assert len(current.barrier_attempts) == 1 and current.resume_at == old.resume_at
    assert current.barrier_attempts[-1].questions_spent == 1 and not model.script.calls
    assert current.barrier_attempts[-1].patient_words[-1] == "I still cannot afford the lab"
    assert current.barrier_attempts[-1].steps[-1].outcome == "budget_exhausted"
    assert QUESTION not in str(reply.payload) and not capture.requests


def test_expired_attempt_and_unrelated_reply_cannot_resume_chase(
    store: StoreBase, clock: FakeClock
) -> None:
    w, capture = setup(store, clock)
    add(w)
    w.send("The lab is too expensive", {"step": "ask_patient", "question": QUESTION})
    clock.now += POLICY.attempt_ttl
    model, _ = w.send("The lab is too expensive", {"step": "ask_patient", "question": QUESTION})
    assert not model.script.calls and len(get(w).barrier_attempts) == 1
    assert get(w).barrier_attempts[-1].steps[-1].outcome == "expired"
    w.send("plan")
    assert get(w).state == "blocked" and not capture.requests


def test_new_type_and_reported_resolution(store: StoreBase, clock: FakeClock) -> None:
    w, _ = setup(store, clock)
    add(w)
    w.send("The lab is too expensive", {"step": "ask_patient", "question": QUESTION})
    w.send(
        "I forgot the lab",
        {"step": "hand_to_doctor", "question": "What practical difficulty do you need help with?"},
    )
    assert (
        len(get(w).barrier_attempts) == 2
        and get(w).barrier_attempts[-1].state == "handed_to_doctor"
    )
    model, reply = w.send("The barrier is resolved")
    assert not model.script.calls and get(w).barrier_attempts[-1].state == "resolved"
    assert get(w).state == "open" and get(w).fulfilled_at is None
    assert "does not report completing" in str(reply.payload)


@pytest.mark.parametrize("language", ["en", "ar"])
def test_templates_in_both_languages_use_existing_gates(
    store: StoreBase, clock: FakeClock, language: str
) -> None:
    w, capture = setup(store, clock)
    add(w)
    row = w.store.get(w.patient_scope, "patient", w.patient_scope.patient_id)
    assert row
    w.seed(revise(from_record(row, Patient), clock(), language=language))
    question = templates.render("area", language)
    w.send("The lab is too expensive", {"step": "ask_patient", "question": question})
    assert get(w).barrier_attempts[-1].question == question
    assert templates.question_ok(question, "area", language, w.runtime.safety_policy)
    assert not capture.requests


@pytest.mark.parametrize(
    "value",
    [
        {"step": "ask_patient", "question": "Try Fabricated Pharmacy?"},
        {"step": "ask_patient", "question": "The test costs 50 EGP?"},
        {"step": "ask_patient", "question": "Take aspirin instead?"},
        {"step": "reschedule_visit", "question": "Can you go next week?"},
        {"step": "resume_chase", "question": "All fine now?"},
        {"step": "ask_patient", "question": "What area? What budget?"},
    ],
)
def test_adversarial_model_cannot_add_claim_or_step(
    store: StoreBase, clock: FakeClock, value: dict[str, object]
) -> None:
    w, capture = setup(store, clock)
    add(w)
    _, reply = w.send("The lab is too expensive", value)
    assert get(w).barrier_attempts[-1].questions_spent == 1
    assert QUESTION in str(reply.payload)
    assert str(value["question"]) not in str(reply.payload)
    assert get(w).state == "blocked" and not capture.requests


def test_selection_buttons_and_terminal_named_mission(store: StoreBase, clock: FakeClock) -> None:
    w, _ = setup(store, clock)
    add(w, id="cbc", title="CBC")
    add(w, id="ldl", title="LDL")
    model, choices = w.send("The lab is too expensive")
    assert not model.script.calls and choices.template_id == "patient_barrier_mission_choose"
    w.concierge.model_factory = lambda registry, role: ScriptedModel(
        candidate({"step": "ask_patient", "question": QUESTION})
    )
    w.press(choices)
    blocked = [from_record(r, Mission) for r in w.rows("mission") if r.body["state"] == "blocked"]
    assert len(blocked) == 1 and blocked[0].barrier_attempts
    other = get(w, "ldl" if blocked[0].id == "cbc" else "cbc")
    w.seed(
        revise(other, clock(), state="cancelled", work_clock=None, cancellation_reason="synthetic")
    )
    _, reply = w.send(other.title + " is too expensive")
    assert reply.template_id == "patient_barrier_missing"


def test_no_candidate_and_quoted_barrier(store: StoreBase, clock: FakeClock) -> None:
    w, _ = setup(store, clock)
    problem = {"category": "cost", "quote": "too expensive", "asserted": True, "subject": "patient"}
    readers = ScriptedModel(*(candidate({"problems": [problem]}) for _ in range(2)))
    w.concierge.barrier_model_factory = lambda registry, role: readers
    model, reply = w.send("The lab is too expensive")
    assert reply.template_id == "patient_barrier_missing" and not model.script.calls
    assert len(readers.script.calls) == 2
    add(w)
    no_problem = ScriptedModel(*(candidate({"problems": []}) for _ in range(2)))
    w.concierge.barrier_model_factory = lambda registry, role: no_problem
    w.send("What does 'too expensive' mean?")
    assert not get(w).barrier_attempts


@pytest.mark.parametrize(
    "stage",
    [
        "resolver_begin_persisted",
        "resolver_choose_persisted",
        "resolver_before_places_call",
        "resolver_result_persisted",
        "resolver_finish_persisted",
    ],
)
def test_crash_restart_and_webhook_replay(store: StoreBase, clock: FakeClock, stage: str) -> None:
    w, capture = setup(store, clock)
    add(w)
    text = (
        "The lab is too expensive in Synthetic Quarter"
        if "places" in stage or "result" in stage
        else "The lab is too expensive"
    )
    model = ScriptedModel(
        candidate(
            {
                "step": "find_places" if "in Synthetic" in text else "ask_patient",
                "question": QUESTION
                if "in Synthetic" not in text
                else "What practical difficulty do you need help with?",
            }
        )
    )
    w.concierge.model_factory = lambda registry, role: model

    def crash(at: str) -> None:
        if at == stage:
            raise RuntimeError("synthetic crash")

    w.concierge.checkpoint = crash
    assert w.post(update(PATIENT, text, 1000)).status_code == 200
    assert w.receipt(1000).state == "pending"
    before_calls, before_http = len(model.script.calls), len(capture.requests)
    clock.now += timedelta(minutes=10)
    w.concierge.checkpoint = lambda at: None
    assert w.post(update(PATIENT, text, 1000)).status_code == 200
    assert w.receipt(1000).state == "completed"
    assert len(model.script.calls) == before_calls
    assert len(capture.requests) == before_http + (
        2 if stage == "resolver_before_places_call" else 0
    )
    saved = get(w).barrier_attempts
    assert w.post(update(PATIENT, text, 1000)).status_code == 200
    assert get(w).barrier_attempts == saved
    assert all(a.questions_spent <= 1 and a.searches_spent <= 2 for a in saved)


def test_safety_first_and_stopped_contact_no_resolver_calls(
    store: StoreBase, clock: FakeClock
) -> None:
    w, capture = setup(store, clock)
    add(w)
    model = ScriptedModel(candidate({"step": "ask_patient", "question": QUESTION}))
    w.concierge.model_factory = lambda registry, role: model
    assert w.post(update(PATIENT, "The lab is too expensive, BP 210/150", 1000)).status_code == 200
    assert w.rows("incident") and not model.script.calls and not capture.requests
    w.next_message = 1001
    w.send("stop reminders")
    w.send("The lab is too expensive", {"step": "ask_patient", "question": QUESTION})
    assert get(w).barrier_attempts[-1].outcome == "contact_stopped" and not capture.requests


def test_attempt_shapes_scope_and_exact_projection_guard(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, _ = setup(store, clock)
    add(w)
    actual = store.commit
    checked = []

    def commit(request: CommitRequest) -> CommitResult:
        if request.command.payload.get("resolver_action"):
            row = next(r for r in request.puts if r.entity_type == "mission")
            current = from_record(row, Mission)
            for field, value in (
                ("title", "forged"),
                ("due_at", clock() + timedelta(days=99)),
                ("barrier_reason", "forged"),
            ):
                changes = {field: value}
                if field == "due_at":
                    changes["escalation_at"] = value
                forged = to_record(current.model_copy(update=changes), w.patient_scope)
                altered = request.model_copy(
                    update={"puts": tuple(forged if r == row else r for r in request.puts)}
                )
                outcome = actual(altered)
                assert outcome.status == "forbidden"
                checked.append(field)
            attempt = current.barrier_attempts[-1]
            forged_attempt = attempt.model_copy(
                update={"questions_spent": 0 if attempt.questions_spent else 1}
            )
            forged = to_record(
                current.model_copy(
                    update={"barrier_attempts": (*current.barrier_attempts[:-1], forged_attempt)}
                ),
                w.patient_scope,
            )
            assert (
                actual(
                    request.model_copy(
                        update={"puts": tuple(forged if r == row else r for r in request.puts)}
                    )
                ).status
                == "forbidden"
            )
        return actual(request)

    monkeypatch.setattr(store, "commit", commit)
    w.send("The lab is too expensive", {"step": "ask_patient", "question": QUESTION})
    assert checked
    attempt = get(w).barrier_attempts[-1]
    assert BarrierAttempt.model_validate_json(attempt.model_dump_json()) == attempt
    assert (
        store.get(
            PatientScope(doctor_id="foreign", patient_id=w.patient_scope.patient_id),
            "mission",
            "synthetic-test",
        )
        is None
    )


@pytest.mark.parametrize("change", ["cancel", "stop", "suspend"])
def test_revocation_between_reservation_and_http_blocks_call(
    store: StoreBase, clock: FakeClock, change: str
) -> None:
    w, capture = setup(store, clock)
    add(w)

    def checkpoint(stage: str) -> None:
        if stage != "resolver_before_places_call":
            return
        if change == "cancel":
            w.seed(
                revise(
                    get(w),
                    clock(),
                    state="cancelled",
                    work_clock=None,
                    cancellation_reason="synthetic",
                )
            )
        elif change == "stop":
            w.seed(
                revise(
                    w.profile,
                    clock(),
                    delivery_epoch=w.profile.delivery_epoch + 1,
                    routine_contact_enabled=False,
                )
            )
        else:
            w.seed(
                revise(w.doctor, clock(), status="suspended", auth_epoch=w.doctor.auth_epoch + 1)
            )

    w.concierge.checkpoint = checkpoint
    model = ScriptedModel(
        candidate(
            {"step": "find_places", "question": "What practical difficulty do you need help with?"}
        )
    )
    w.concierge.model_factory = lambda registry, role: model
    assert (
        w.post(update(PATIENT, "The lab is too expensive in Synthetic Quarter", 1000)).status_code
        == 200
    )
    assert not capture.requests
    assert w.receipt(1000).state == "pending"
    assert get(w).barrier_attempts[-1].searches_spent == 1


def test_cancel_after_offer_commit_suppresses_unsent_offer(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, _ = setup(store, clock)
    add(w)
    dispatch_one = w.runtime.dispatcher.dispatch_one
    monkeypatch.setattr(w.runtime.dispatcher, "dispatch_one", lambda *args, **kwargs: None)
    _, reply = w.send(
        "The lab is too expensive in Synthetic Quarter",
        {"step": "find_places", "question": "What practical difficulty do you need help with?"},
    )
    monkeypatch.setattr(w.runtime.dispatcher, "dispatch_one", dispatch_one)
    assert reply.status == "queued"
    assert reply.source_versions == (to_record(get(w), w.patient_scope).ref,)
    w.seed(
        revise(get(w), clock(), state="cancelled", work_clock=None, cancellation_reason="synthetic")
    )
    from store.contact_fixtures import dispatch

    calls = len(w.transport.calls)
    result = dispatch(w, reply)
    assert result.status == "suppressed" and len(w.transport.calls) == calls


@pytest.mark.parametrize("language", ["en", "ar"])
def test_offer_and_every_fixed_sentence_are_gated_in_both_languages(
    store: StoreBase, clock: FakeClock, language: str
) -> None:
    from sanad.agents.hygiene import patient_failure
    from sanad.safety.models import OutputContext

    w, capture = setup(store, clock)
    add(w)
    row = w.store.get(w.patient_scope, "patient", w.patient_scope.patient_id)
    assert row
    w.seed(revise(from_record(row, Patient), clock(), language=language))
    _, reply = w.send(
        "The lab is too expensive in Synthetic Quarter",
        {"step": "find_places", "question": templates.render("detail", language)},
    )
    assert templates.render("disclosure", language) in str(reply.payload)
    assert all(p.name in str(reply.payload) for p in get(w).barrier_attempts[-1].places)
    assert len(capture.requests) == 2
    context = OutputContext(language=language, mode="plan_explanation")  # type: ignore[arg-type]
    for key in templates.TEMPLATES:
        if key != "place":
            assert (
                patient_failure(templates.render(key, language), context, w.runtime.safety_policy)
                is None
            )


def test_checkpoint_cannot_clear_receipt_claim_or_reset_budget(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.store.records import InboundReceipt

    w, _ = setup(store, clock)
    add(w)
    actual = store.commit
    checks = []

    def commit(request: CommitRequest) -> CommitResult:
        if request.command.payload.get("resolver_action"):
            row = next(r for r in request.puts if r.entity_type == "inbound_receipt")
            old = from_record(row, InboundReceipt)
            forged = to_record(old.model_copy(update={"processing_claim": None}), w.patient_scope)
            result = actual(
                request.model_copy(
                    update={"puts": tuple(forged if r == row else r for r in request.puts)}
                )
            )
            assert result.status == "forbidden"
            checks.append(result.status)
        return actual(request)

    monkeypatch.setattr(store, "commit", commit)
    w.send("The lab is too expensive", {"step": "ask_patient", "question": QUESTION})
    assert len(checks) == 3


def test_legacy_block_gets_attempt_without_moving_its_pause(
    store: StoreBase, clock: FakeClock
) -> None:
    w, _ = setup(store, clock)
    add(w)
    pause = clock() + timedelta(hours=6)
    w.seed(
        revise(
            get(w),
            clock(),
            state="blocked",
            barrier_type="cost",
            barrier_reason="Original patient report",
            resume_at=pause,
        )
    )
    w.send("The lab is too expensive", {"step": "ask_patient", "question": QUESTION})
    assert get(w).resume_at == pause and get(w).barrier_reason == "Original patient report"
    assert len(get(w).barrier_attempts) == 1


@pytest.mark.parametrize("material", [True, False])
def test_older_interrupted_receipt_cannot_buy_attempt_after_new_reply(
    store: StoreBase, clock: FakeClock, material: bool
) -> None:
    w, capture = setup(store, clock)
    add(w)

    def crash(stage: str) -> None:
        if stage == "resolver_begin_persisted":
            raise RuntimeError("synthetic interrupted first receipt")

    w.concierge.checkpoint = crash
    assert w.post(update(PATIENT, "The lab is too expensive", 1000)).status_code == 200
    assert w.receipt(1000).state == "pending"
    w.concierge.checkpoint = lambda stage: None
    w.next_message = 1001
    model, _ = w.send(
        "I forgot the lab" if material else "The lab is too expensive",
        {"step": "hand_to_doctor", "question": "What practical difficulty do you need help with?"},
    )
    assert len(get(w).barrier_attempts) == (2 if material else 1)
    calls = len(model.script.calls)
    clock.now += timedelta(minutes=10)
    assert w.post(update(PATIENT, "The lab is too expensive", 1000)).status_code == 200
    assert w.receipt(1000).state == "completed"
    attempts = get(w).barrier_attempts
    assert len(attempts) == (2 if material else 1)
    assert all(a.reasoning_spent == 1 and a.questions_spent == 1 for a in attempts)
    assert all(a.phase == "complete" for a in attempts)
    assert len(model.script.calls) == calls and not capture.requests
