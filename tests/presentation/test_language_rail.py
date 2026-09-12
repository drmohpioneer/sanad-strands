"""Attempt 6a: render real synthetic journeys with stored Arabic preferences."""

import re

import pytest
from domain_fixtures import NOW
from harness import FakeClock
from store.account_fixtures import APPLICANT, PATIENT, update
from store.concierge_fixtures import PatientWorld
from store.executors_15_fixtures import doctor
from store.executors_15_fixtures import world as executor_world
from store.login_fixtures import browser_login
from store.scribe_fixtures import ScribeWorld

from sanad.auth.service import revise
from sanad.concierge import education
from sanad.store.memory import MemoryStore
from sanad.store.records import OutboundIntent, from_record

ARABIC = re.compile(r"[\u0600-\u06FF]")
NAME = "أحمد رضا"


@pytest.fixture(autouse=True)
def contest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")


def english(text: str, surface: str) -> None:
    assert not ARABIC.search(text.replace(NAME, "")), f"{surface}: {text}"


def payload(intent: OutboundIntent) -> str:
    assert intent.payload and isinstance(intent.payload.get("text"), str)
    return str(intent.payload["text"])


def scribe_world() -> ScribeWorld:
    clock = FakeClock(NOW)
    w = ScribeWorld.create(MemoryStore(clock=clock), clock)
    w.approve(language="ar")
    w.named_stub(NAME)
    return w


def dictation(w: ScribeWorld) -> None:
    w.dictate(
        f"{NAME}. Start Forxiga 10 mg. Start Concor. Request CBC tomorrow.",
        {
            "patient": {"name_as_spoken": NAME},
            "missions": [{"kind": "TEST", "text": "CBC", "timing_expression": "tomorrow"}],
            "orders": [
                {"action": "start", "action_quote": "Start", "drug": "Forxiga", "dose": "10 mg"},
                {"action": "start", "action_quote": "Start", "drug": "Concor"},
            ],
        },
    )


@pytest.mark.parametrize("surface", ["welcome", "help", "dictation", "confirmation", "qr"])
def test_doctor_stored_arabic(surface: str) -> None:
    w = scribe_world()
    before = {i.id for i in (*w.cards(), *w.intents())}
    if surface in {"dictation", "confirmation"}:
        dictation(w)
        if surface == "confirmation":
            before = {i.id for i in (*w.cards(), *w.intents())}
            w.tap("✅ Confirm")
    else:
        command = {"welcome": "/start", "help": "/help", "qr": "/qr " + NAME}[surface]
        assert w.post(update(APPLICANT, command, 15)).status_code == 200
    replies = [i for i in (*w.cards(), *w.intents()) if i.id not in before]
    assert replies
    for i in replies:
        english(payload(i), surface)
    assert w.doctor.language == "ar"


@pytest.mark.parametrize("surface", ["continue", "refused", "dashboard"])
def test_login_pages_stored_arabic(surface: str) -> None:
    w = scribe_world()
    with w.client() as client:
        path = w.login_path()
        if surface == "continue":
            response = client.get(path)
        elif surface == "refused":
            response = client.post(path)
            assert response.status_code == 403
        else:
            assert browser_login(client, path).status_code == 303
            response = client.get("/a")
        english(response.text, surface)


@pytest.mark.parametrize(
    "surface", ["plan", "education", "reminders", "quiet", "quiet_slots", "patient_page"]
)
def test_patient_stored_arabic(surface: str) -> None:
    clock = FakeClock(NOW)
    w = PatientWorld.create(MemoryStore(clock=clock), clock)
    assert isinstance(w, PatientWorld)
    patient = w.enroll(medication=False)
    w.seed(revise(patient, clock(), language="ar", display_name=NAME))
    if surface == "patient_page":
        with w.client() as client:
            assert browser_login(client, w.login_path(PATIENT)).status_code == 303
            english(client.get("/pp").text, surface)
        return
    if surface == "quiet_slots":
        from store.medication_fixtures import confirm

        confirm(w, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
        _, start = w.send("I started Atorvastatin")
        assert start.template_id == "patient_start_recorded"
        _, reply = w.send("quiet hours 00:00 23:59")
        assert "Day-three follow-up" in payload(reply)
        assert "this reminder needs separate consent" in payload(reply)
        english(str(reply.payload), surface)
        return
    if surface == "education":
        line = education.retrieve("hypertension", synthetic=True)[0].lines("en")[0]
        _, reply = w.send(
            "What is hypertension?",
            {"reply": line, "kind": "education", "needs_doctor": False},
        )
        assert reply.template_id == "patient_answer"
    else:
        _, reply = w.send(
            {"plan": "/plan", "reminders": "stop reminders", "quiet": "quiet hours 22:00 08:00"}[
                surface
            ]
        )
        assert (
            reply.template_id
            == {
                "plan": "patient_plan_summary",
                "reminders": "patient_stop_ack",
                "quiet": "patient_quiet_ack",
            }[surface]
        )
    english(payload(reply), surface)


def test_digest_stored_arabic() -> None:
    clock = FakeClock(NOW)
    w = executor_world(MemoryStore(clock=clock), clock)
    w.seed(revise(w.doctor, clock(), language="ar"))
    english(payload(doctor(w, "/digest")), "digest")


def test_correction_stored_arabic() -> None:
    from store import evidence_fixtures as f
    from store.test_corrections_19 import correct_value, upload

    clock = FakeClock(NOW)
    w = f.world(MemoryStore(clock=clock), clock)
    w.seed(revise(w.doctor, clock(), language="ar"))
    upload(w)
    assert correct_value(w).status == "accepted"
    replies = [
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if r.body.get("template_id") == "accepted_correction"
    ]
    assert len(replies) == 1
    english(payload(replies[0]), "correction")


@pytest.mark.parametrize(
    "reason,variant",
    [
        ("malformed", "token"),
        ("malformed", "cookie"),
        ("no_pre_session", "missing"),
        ("no_pre_session", "expired"),
        ("no_pre_session", "consumed"),
        ("bad_csrf", "mismatch"),
        ("unknown_link", "unknown"),
        ("already_used", "consumed"),
        ("expired", "elapsed"),
        ("expired", "revoked"),
        ("wrong_account", "epoch"),
        ("commit_failed", "store"),
        ("origin", "cross_site"),
    ],
)
def test_login_refusal_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reason: str, variant: str
) -> None:
    import logging
    from datetime import timedelta

    from store.login_fixtures import ORIGIN, replace_model

    from sanad.auth.tokens import token_hash
    from sanad.store._base import Write
    from sanad.store.records import Forbidden, LoginExchange, PreSession, record_item, to_record
    from sanad.web.routes import PRE_COOKIE

    w = scribe_world()
    path = w.login_path()
    raw = path.rsplit("/", 1)[-1]
    with w.client() as client:
        page = client.get(path)
        match = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert match
        csrf = match[1]
        cookie = client.cookies[PRE_COOKIE]
        if reason == "malformed":
            if variant == "token":
                path = "/d/invalid"
            else:
                client.cookies.clear()
        elif reason == "no_pre_session":
            digest = token_hash(cookie)
            assert digest
            pre = w.login.load(w.login.scope, "pre_session", digest, PreSession)
            assert pre
            if variant == "missing":
                client.cookies.set(PRE_COOKIE, "Z" * 43, domain="sanad.example", path="/")
            else:
                changes: dict[str, object] = (
                    {"expires_at": w.clock() - timedelta(seconds=1)}
                    if variant == "expired"
                    else {"consumed_at": w.clock()}
                )
                row = to_record(revise(pre, w.clock(), **changes), pre.scope)
                assert w.store._atomic([Write(record_item(row), pre.version)], [])
        elif reason == "bad_csrf":
            csrf = "wrong"
        elif reason == "unknown_link":
            path = "/d/" + "Z" * 43
        elif reason == "already_used":
            assert browser_login(client, path).status_code == 303
            # Token checks require a fresh valid pre-session after successful consumption.
            page = client.get(path)
            match = re.search(r'name="csrf" value="([^"]+)"', page.text)
            assert match
            csrf = match[1]
        elif reason == "expired":
            digest = token_hash(raw)
            assert digest
            exchange = w.login.load(w.login.scope, "doctor_login", digest, LoginExchange)
            assert exchange
            changes = (
                {"expires_at": w.clock() - timedelta(seconds=1)}
                if variant == "elapsed"
                else {"state": "revoked"}
            )
            row = to_record(revise(exchange, w.clock(), **changes), exchange.scope)
            assert w.store._atomic([Write(record_item(row), exchange.version)], [])
        elif reason == "wrong_account":
            replace_model(w, revise(w.doctor, w.clock(), auth_epoch=w.doctor.auth_epoch + 1))
        elif reason == "commit_failed":
            monkeypatch.setattr(w.login, "commit", lambda *args, **kwargs: Forbidden())
        caplog.set_level(logging.INFO, logger="sanad.web.routes")
        response = client.post(
            path,
            data={"csrf": csrf},
            headers={"Origin": "https://foreign.example" if reason == "origin" else ORIGIN},
        )
        assert response.status_code == 403
        sentence = {
            "expired": "This link has expired; send /login again.",
            "unknown_link": "This link has expired; send /login again.",
            "already_used": "This link was already used; send /login again.",
            "wrong_account": "This link is not for this account.",
        }.get(reason, "Please open the link from the message again.")
        assert sentence in response.text
        lines = [r.getMessage() for r in caplog.records if r.name == "sanad.web.routes"]
        assert lines == [f"login_refused role=doctor reason={reason}"]
        assert all(secret not in lines[0] for secret in (raw, cookie, csrf, APPLICANT))
        assert "sanad_session" not in client.cookies


def test_pre_session_outage_keeps_503(monkeypatch: pytest.MonkeyPatch) -> None:
    w = scribe_world()
    monkeypatch.setattr(w.login, "pre_session", lambda: None)
    with w.client() as client:
        response = client.get("/d/" + "A" * 43)
    assert response.status_code == 503
    assert "Please try again in a minute." in response.text


def test_welcome_uses_free_text_and_help_keeps_commands() -> None:
    w = scribe_world()
    before = {i.id for i in w.cards()}
    w.post(update(APPLICANT, "/start", 3000))
    welcome = next(i for i in w.cards() if i.id not in before)
    assert payload(welcome) == (
        "Welcome back. Write or record what you want to do for a patient, in your own words. "
        "You can also send a prescription photo. Type /help for the commands."
    )
    before = {i.id for i in w.cards()}
    w.post(update(APPLICANT, "/help", 3001))
    help_reply = next(i for i in w.cards() if i.id not in before)
    assert "/new name" in payload(help_reply)


@pytest.mark.parametrize("hour,expected", [(16, "today"), (18, "tomorrow")])
def test_digest_next_cairo_and_eligible_waiting_count(hour: int, expected: str) -> None:
    from datetime import timedelta

    from store.executors_15_fixtures import question
    from store.test_question_digest_17c import overdue

    from sanad.contact.question_digest import due

    clock = FakeClock(NOW)
    w = executor_world(MemoryStore(clock=clock), clock)
    w.seed(revise(w.doctor, clock(), language="ar"))
    empty = payload(doctor(w, "/digest", id=3010))
    assert empty == (
        "Patient questions are collected and sent to you once a day at 20:00 Cairo "
        "(packing: one message). Nothing is waiting right now."
    )
    mission = question(w)
    assert not due(w.store, w.doctor.scope)
    overdue(w, mission)
    from sanad.contact.question_digest import identity, load

    schedule = load(w.store, w.doctor.scope)
    assert schedule
    w.seed(
        revise(
            schedule, clock(), last_shown_ids=(identity(w.patient_scope.patient_id, mission.id),)
        )
    )
    assert len(due(w.store, w.doctor.scope)) == 1  # Previously shown, still unresolved.
    clock.now = clock().replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=1)
    reply = payload(doctor(w, "/digest 20:00 each", id=3011))
    assert reply == f"Next digest: {expected} at 20:00 Cairo. 1 questions are waiting."
    assert payload(doctor(w, "/digest", id=3012)) == (
        "Patient questions are collected and sent to you once a day at 20:00 Cairo "
        "(packing: each question). 1 questions are waiting."
    )


def test_changed_by_uses_recipient_name_or_safe_fallback() -> None:
    from store import evidence_fixtures as f
    from store.test_corrections_19 import correct_value, upload

    from sanad.corrections import Correction
    from sanad.steward.corrections import notice_text

    clock = FakeClock(NOW)
    w = f.world(MemoryStore(clock=clock), clock)
    upload(w)
    assert correct_value(w).status == "accepted"
    correction = from_record(w.rows("correction")[0], Correction)
    for recipient, actor, expected in [
        (w.doctor, w.doctor, "you"),
        (w.doctor.model_copy(update={"telegram_user_id": "222"}), w.doctor, w.doctor.name),
        (None, None, "the doctor"),
        (None, w.doctor.model_copy(update={"name": ""}), "the doctor"),
    ]:
        text = notice_text(correction, recipient=recipient, actor=actor)
        assert f"Changed by {expected}:" in text
        assert correction.actor.subject not in text
    with w.client() as client:
        assert browser_login(client, w.login_path()).status_code == 303
        notice = client.get(f"/api/patients/{w.patient_scope.patient_id}").json()["corrections"][0][
            "notice"
        ]
        assert "Changed by you:" in notice and correction.actor.subject not in notice
    reply = next(
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if r.body.get("template_id") == "accepted_correction"
    )
    assert "Changed by you:" in payload(reply)


def test_resolver_question_uses_effective_language_for_its_gate() -> None:
    from sanad.resolver.templates import question_ok
    from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT

    assert question_ok(
        "Which area or neighbourhood should I search near?",
        "area",
        "ar",
        SAFETY_POLICY_V1_CARDIOLOGY_DRAFT,
    )
