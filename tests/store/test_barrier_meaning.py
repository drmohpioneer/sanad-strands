"""Contract 28 through authenticated receipts and the real atomic store boundary."""

from datetime import timedelta
from typing import Any, cast

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate

from sanad.auth.service import revise
from sanad.concierge.records import BarrierOutcome
from sanad.models.registry import ModelRegistry
from sanad.store._base import StoreBase
from sanad.store.records import CommitRequest, CommitResult, InboundReceipt, from_record, to_record
from store.account_fixtures import PATIENT, update
from store.concierge_fixtures import PatientWorld
from store.login_fixtures import ORIGIN, browser_login
from store.resolver_fixtures import QUESTION, add, get, setup


def response(kind: str | None, quote: str = "", **fields: object) -> dict[str, object]:
    return {
        "problems": [
            {"category": kind, "quote": quote, "asserted": True, "subject": "patient", **fields}
        ]
        if kind
        else []
    }


def install(w: PatientWorld, *values: dict[str, object] | Exception) -> ScriptedModel:
    model = ScriptedModel(*(v if isinstance(v, Exception) else candidate(v) for v in values))
    w.concierge.barrier_model_factory = lambda registry, role: model
    return model


@pytest.mark.parametrize("case", ["accepted", "none", "question"])
def test_no_eligible_target_reads_and_keeps_todays_reply(
    store: StoreBase, clock: FakeClock, case: str
) -> None:
    w, _ = setup(store, clock)
    text = {
        "accepted": "The lab is too expensive",
        "none": "My appointment diary is at home",
        "question": "What does 'too expensive' mean?",
    }[case]
    value = response("cost", "too expensive") if case == "accepted" else response(None)
    readers = install(w, value, value)
    answer, reply = w.send(text)
    saved = w.receipt(1000)
    assert len(readers.script.calls) == 2
    assert saved.barrier_reservation and saved.barrier_outcome
    assert not any(r.body.get("barrier_attempts") for r in w.rows("mission"))
    if case == "accepted":
        assert saved.barrier_outcome.status == "accepted"
        assert saved.barrier_outcome.category == "cost"
        assert reply.template_id == "patient_barrier_missing" and not answer.script.calls
    else:
        assert saved.barrier_outcome.status == "none"
        assert reply.template_id != "patient_barrier_missing"
        assert len(answer.script.calls) == 1
        assert any(r.body["kind"] == "QUESTION" for r in w.rows("mission"))


def test_patient_send_scripts_readers_apart_from_answers(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import store.concierge_fixtures as fixtures

    w = PatientWorld.create(store, clock)
    assert isinstance(w, PatientWorld)
    w.enroll(medication=False)
    during: list[int] = []

    def observed(request: dict[str, Any]) -> dict[str, Any]:
        answer = cast(ScriptedModel, w.concierge.model_factory(ModelRegistry(), "worker"))
        during.append(len(answer.script.calls))
        return candidate({"problems": []})

    monkeypatch.setattr(fixtures, "no_problem", observed)
    answer, _ = w.send("Where should I bring my appointment diary?")
    assert len(during) == 2
    assert during == [0, 0]
    assert len(answer.script.calls) == 1


def test_contact_world_scripts_readers_apart_from_answers(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import store.concierge_fixtures as fixtures
    from store.contact_fixtures import world as contact_world

    w = contact_world(store, clock)
    answer = ScriptedModel(
        candidate(
            {"reply": "السؤال ده محتاج الدكتور", "kind": "cannot_answer", "needs_doctor": True}
        )
    )
    w.concierge.model_factory = lambda registry, role: answer
    during: list[int] = []

    def observed(request: dict[str, Any]) -> dict[str, Any]:
        during.append(len(answer.script.calls))
        return candidate({"problems": []})

    monkeypatch.setattr(fixtures, "no_problem", observed)
    assert w.post(update(PATIENT, "Where should I bring my appointment diary?", 1000)).status_code
    assert w.receipt(1000).state == "completed"
    assert len(during) == 2
    assert during == [0, 0]
    assert len(answer.script.calls) == 1


@pytest.mark.parametrize(
    "text,kind",
    [
        ("the pharmacy near me didn't have it", "availability"),
        ("it costs more than I can pay this month", "cost"),
        ("I keep forgetting the evening one", "forgot"),
        ("I felt dizzy after the new pill", "side_effect_experience"),
        ("I'm not sure if I take it before food", "confusion"),
        ("الصيدلية اللي جنبي ملقيتش عندها العلاج", "availability"),
        ("تمنه اكتر من اللي اقدر ادفعه الشهر ده", "cost"),
        ("كل مرة بنسى اللي بالليل", "forgot"),
        ("حسيت بدوخة بعد الحباية الجديدة", "side_effect_experience"),
        ("مش متأكد اخده قبل الاكل", "confusion"),
    ],
)
def test_paraphrase_help_and_replay(
    store: StoreBase, clock: FakeClock, text: str, kind: str
) -> None:
    w, capture = setup(store, clock)
    old = add(w, "MEDICATION")
    readers = install(w, response(kind, text), response(kind, text))
    _, reply = w.send(text)
    receipt = w.receipt(1000)
    assert receipt.barrier_outcome and receipt.barrier_outcome.status == "accepted"
    assert receipt.barrier_outcome.category == kind and len(readers.script.calls) == 2
    attempt = get(w, old.id).barrier_attempts[-1]
    assert attempt.barrier_type == kind and attempt.reasoning_spent == 1
    if kind == "side_effect_experience":
        assert attempt.state == "handed_to_doctor" and not capture.requests
    else:
        assert attempt.requested_fact == ("area" if kind in {"cost", "availability"} else "detail")
        assert attempt.outcome == "asked" and attempt.questions_spent == 1
    assert reply.template_id == "patient_barrier_recorded"
    assert w.post(update(PATIENT, text, 1000)).status_code == 200
    assert get(w, old.id).barrier_attempts[-1] == attempt
    assert len(readers.script.calls) == 2


@pytest.mark.parametrize(
    "case", ["quote", "negated", "wife", "question", "category", "timeout", "disagreement"]
)
def test_rejections_keep_input_without_help(store: StoreBase, clock: FakeClock, case: str) -> None:
    w, capture = setup(store, clock)
    add(w)
    text, value = {
        "quote": ("I feel fine", response("forgot", "invented")),
        "negated": (
            "I'm not having any side effects",
            response("side_effect_experience", "side effects"),
        ),
        "wife": ("my wife can't afford hers", response("cost", "can't afford")),
        "question": ("is it expensive?", response(None)),
        "category": ("a difficult problem", response("made_up", "a difficult problem")),
        "timeout": ("a difficult problem", response(None)),
        "disagreement": ("a difficult problem", response("cost", "a difficult problem")),
    }[case]
    readers = install(
        w,
        *(
            [TimeoutError()] * 4
            if case == "timeout"
            else [value, response("forgot", text) if case == "disagreement" else value]
        ),
    )
    _, reply = w.send(text)
    saved = w.receipt(1000)
    assert saved.state == "completed" and saved.payload and saved.payload["text"] == text
    assert saved.barrier_outcome and saved.barrier_outcome.status == (
        "none" if case == "question" else "failure" if case == "timeout" else "uncertain"
    )
    assert not get(w).barrier_attempts and not capture.requests
    if case != "question":
        assert reply.template_id == "patient_barrier_uncertain"
        assert reply.payload
        markup = reply.payload["reply_markup"]
        assert isinstance(markup, dict)
        keyboard = markup["inline_keyboard"]
        assert isinstance(keyboard, list) and len(keyboard) == 6
    assert len(readers.script.calls) == (4 if case == "timeout" else 2)


@pytest.mark.parametrize("stage", ["barrier_reserved", "barrier_read"])
def test_two_crashes_exhaust_one_extra_attempt(
    store: StoreBase, clock: FakeClock, stage: str
) -> None:
    w, _ = setup(store, clock)
    add(w)
    text = "I cannot afford it"
    readers = install(w, *(response("cost", text) for _ in range(4)))

    def crash(at: str) -> None:
        if at == stage:
            raise RuntimeError("synthetic interrupted reading")

    w.concierge.checkpoint = crash
    for attempt in (1, 2):
        assert w.post(update(PATIENT, text, 1000)).status_code == 200
        receipt = w.receipt(1000)
        assert receipt.state == "pending" and receipt.barrier_reservation
        assert receipt.barrier_reservation.attempts == attempt and not receipt.barrier_outcome
        clock.now += timedelta(minutes=10)
    calls = len(readers.script.calls)
    assert calls == (4 if stage == "barrier_read" else 0)
    w.concierge.checkpoint = lambda at: None
    assert w.post(update(PATIENT, text, 1000)).status_code == 200
    saved = w.receipt(1000)
    assert saved.state == "completed" and saved.barrier_outcome
    assert saved.barrier_outcome.status == "failure" and len(readers.script.calls) == calls
    assert not get(w).barrier_attempts
    assert any(i.template_id == "patient_barrier_uncertain" for i in w.patient_intents())


@pytest.mark.parametrize("stale", ["none", "expired", "target", "used"])
def test_telegram_category_choice_is_bound(store: StoreBase, clock: FakeClock, stale: str) -> None:
    w, _ = setup(store, clock)
    add(w)
    readers = install(
        w,
        response("uncertain", "a difficult problem"),
        response("uncertain", "a difficult problem"),
    )
    _, reply = w.send("a difficult problem")
    if stale == "expired":
        clock.now += timedelta(minutes=30)
    if stale == "target":
        w.seed(
            revise(
                get(w), clock(), state="cancelled", work_clock=None, cancellation_reason="synthetic"
            )
        )
    w.press(reply, index=0)
    if stale == "used":
        before = get(w).barrier_attempts
        w.press(reply, index=2, id=2001)
        assert get(w).barrier_attempts == before
    assert len(readers.script.calls) == 2
    if stale in {"none", "used"}:
        assert get(w).barrier_attempts[-1].barrier_type == "cost"
        saved = w.receipt(2000).barrier_outcome
        assert saved and saved.provenance == "patient_choice" and saved.choice_id
    else:
        assert not get(w).barrier_attempts
        assert any(i.template_id == "patient_callback_stale" for i in w.patient_intents())


@pytest.mark.parametrize("stale", ["none", "expired", "target", "used"])
@pytest.mark.parametrize("choice", ["Cost", "1"])
def test_web_category_choice(store: StoreBase, clock: FakeClock, stale: str, choice: str) -> None:
    w, _ = setup(store, clock)
    add(w)
    with w.client() as client:
        assert browser_login(client, w.login_path(PATIENT)).status_code == 303
        headers = {"origin": ORIGIN, "x-csrf-token": client.cookies["sanad_csrf"]}
        readers = install(
            w,
            response("uncertain", "a difficult problem"),
            response("uncertain", "a difficult problem"),
        )

        def post(text: str, id: str, *, refused: bool = False) -> None:
            result = client.post(
                "/api/patient/messages", headers=headers, json={"text": text, "command_id": id}
            )
            if refused:
                assert result.status_code == 409
            else:
                assert result.status_code == 200 and result.json()["status"] == "accepted"

        post("a difficult problem", "first")
        if stale == "expired":
            clock.now += timedelta(minutes=30)
            assert browser_login(client, w.login_path(PATIENT, id=201)).status_code == 303
            headers["x-csrf-token"] = client.cookies["sanad_csrf"]
        if stale == "target":
            w.seed(
                revise(
                    get(w),
                    clock(),
                    state="cancelled",
                    work_clock=None,
                    cancellation_reason="synthetic",
                )
            )
        post(choice, "choose", refused=stale in {"expired", "target"})
        if stale == "used":
            previous = get(w).barrier_attempts
            post("3", "again", refused=True)
            assert get(w).barrier_attempts == previous
        assert len(readers.script.calls) == 2
        if stale in {"none", "used"}:
            assert get(w).barrier_attempts[-1].barrier_type == "cost"
            outcomes = [
                from_record(r, InboundReceipt).barrier_outcome
                for r in store.patient_receipts(w.patient_scope, limit=30)[0]
            ]
            assert any(o and o.provenance == "patient_choice" for o in outcomes)
        else:
            assert not get(w).barrier_attempts


@pytest.mark.parametrize("stale", ["none", "expired", "target"])
def test_web_numbered_target_reuses_reading(store: StoreBase, clock: FakeClock, stale: str) -> None:
    w, _ = setup(store, clock)
    add(w, id="cbc", title="CBC")
    add(w, id="ldl", title="LDL")
    text = "The lab is too expensive"
    with w.client() as client:
        assert browser_login(client, w.login_path(PATIENT)).status_code == 303
        headers = {"origin": ORIGIN, "x-csrf-token": client.cookies["sanad_csrf"]}
        readers = install(w, response("cost", text), response("cost", text))
        r = client.post(
            "/api/patient/messages", headers=headers, json={"text": text, "command_id": "target"}
        )
        assert r.status_code == 200 and r.json()["status"] == "accepted"
        assert not get(w, "cbc").barrier_attempts and not get(w, "ldl").barrier_attempts
        assert any(
            "1. CBC" in str(i.payload) and "2. LDL" in str(i.payload) for i in w.patient_intents()
        )
        if stale == "expired":
            clock.now += timedelta(minutes=30)
            assert browser_login(client, w.login_path(PATIENT, id=201)).status_code == 303
            headers["x-csrf-token"] = client.cookies["sanad_csrf"]
        if stale == "target":
            w.seed(
                revise(
                    get(w, "cbc"),
                    clock(),
                    state="cancelled",
                    work_clock=None,
                    cancellation_reason="synthetic",
                )
            )
        r = client.post(
            "/api/patient/messages", headers=headers, json={"text": "1", "command_id": "choose"}
        )
        if stale == "none":
            assert r.status_code == 200 and r.json()["status"] == "accepted"
        else:
            assert r.status_code == 409
        assert len(readers.script.calls) == 2 and not get(w, "ldl").barrier_attempts
        assert bool(get(w, "cbc").barrier_attempts) == (stale == "none")


@pytest.mark.parametrize(
    "text",
    [
        "nausea",
        "nauseous",
        "dizziness",
        "دوخة",
        "صـدري",
        "صَدْري",
        "الصدر",
        "بصدري",
        "sadry",
        "aspirin",
        "CBC",
    ],
)
def test_rejected_area_ends_existing_attempt(store: StoreBase, clock: FakeClock, text: str) -> None:
    w, capture = setup(store, clock)
    add(w)
    w.send("The lab is too expensive", {"step": "ask_patient", "question": QUESTION})
    old = get(w)
    readers = install(w, response(None), response(None))
    answer, reply = w.send(text)
    now = get(w)
    assert len(now.barrier_attempts) == 1 and now.resume_at == old.resume_at
    attempt = now.barrier_attempts[-1]
    assert attempt.state == "handed_to_doctor" and attempt.questions_spent == 1
    assert attempt.searches_spent == 0 and attempt.patient_words[-1] == text
    assert not capture.requests and not answer.script.calls and len(readers.script.calls) == 2
    assert QUESTION not in str(reply.payload)


def test_quote_offsets_revalidated_atomically(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, _ = setup(store, clock)
    add(w)
    actual = store.commit
    checked = []

    def commit(request: CommitRequest) -> CommitResult:
        raw = request.command.payload.get("barrier_outcome")
        action = request.command.payload.get("resolver_action")
        if raw and isinstance(action, dict) and action.get("phase") == "begin":
            outcome = BarrierOutcome.model_validate(raw)
            assert outcome.citations
            forged = outcome.model_copy(
                update={
                    "citations": (
                        outcome.citations[0].model_copy(update={"end": 999}),
                        *outcome.citations[1:],
                    )
                }
            )
            command = request.command.model_copy(
                update={
                    "payload": {
                        **request.command.payload,
                        "barrier_outcome": forged.model_dump(mode="json"),
                    }
                }
            )
            puts = tuple(
                to_record(
                    from_record(r, InboundReceipt).model_copy(update={"barrier_outcome": forged}),
                    w.patient_scope,
                )
                if r.entity_type == "inbound_receipt"
                else r
                for r in request.puts
            )
            assert (
                actual(request.model_copy(update={"command": command, "puts": puts})).status
                == "forbidden"
            )
            assert not get(w).barrier_attempts
            checked.append(True)
        return actual(request)

    monkeypatch.setattr(store, "commit", commit)
    w.send("The lab is too expensive")
    assert checked == [True] and get(w).barrier_attempts


def test_uncertain_day3_fulfills_before_stale_choice(store: StoreBase, clock: FakeClock) -> None:
    from store.test_medication import task, waiting_checkin

    w, _ = setup(store, clock)
    parent = add(w, "MEDICATION")
    waiting_checkin(w)
    before = get(w, parent.id)
    text = "a difficult problem"
    readers = install(w, response("uncertain", text), response("uncertain", text))
    _, reply = w.send(text)
    completed = task(w)
    assert completed.state == "fulfilled" and len(completed.source_report_ids) == 1
    fact = store.get(w.patient_scope, "clinical_fact", completed.source_report_ids[0])
    assert fact and isinstance(fact.body["payload"], dict)
    assert fact.body["payload"]["report_kind"] == "day3"
    assert get(w, parent.id) == before
    assert reply.template_id == "patient_barrier_uncertain"
    w.press(reply, index=0)
    assert task(w) == completed and get(w, parent.id) == before
    assert len(readers.script.calls) == 2
    assert any(i.template_id == "patient_callback_stale" for i in w.patient_intents())


@pytest.mark.parametrize("several", [True, False])
def test_multi_problem_chooses_target_then_category(
    store: StoreBase, clock: FakeClock, several: bool
) -> None:
    w, _ = setup(store, clock)
    add(w, id="cbc", title="CBC")
    add(w, id="ldl", title="LDL")
    text = "CBC is too expensive and LDL was unavailable"
    value = response("cost", text)
    if several:
        value = {
            "problems": [
                {
                    "category": "cost",
                    "quote": "CBC is too expensive",
                    "asserted": True,
                    "subject": "patient",
                },
                {
                    "category": "availability",
                    "quote": "LDL was unavailable",
                    "asserted": True,
                    "subject": "patient",
                },
            ]
        }
    readers = install(w, value, value)
    _, reply = w.send(text)
    assert reply.template_id == "patient_barrier_mission_choose"
    assert not get(w, "cbc").barrier_attempts and not get(w, "ldl").barrier_attempts
    w.press(reply, index=0)
    choices = [i for i in w.patient_intents() if i.template_id == "patient_barrier_uncertain"]
    assert len(choices) == 1 and not get(w, "cbc").barrier_attempts
    w.press(choices[0], index=0, id=2001)
    assert get(w, "cbc").barrier_attempts[-1].barrier_type == "cost"
    assert not get(w, "ldl").barrier_attempts and len(readers.script.calls) == 2
