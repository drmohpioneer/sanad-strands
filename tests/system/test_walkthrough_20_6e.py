"""Round-two owner reproductions through receipts, stored cards and both channels."""

from collections.abc import Iterator
from typing import Any

import pytest
from domain_fixtures import NOW
from providers.fixtures import ScriptedModel, candidate
from store.account_fixtures import PATIENT, callback, update
from store.concierge_fixtures import PatientWorld
from store.login_fixtures import browser_login
from store.test_browser_upload import UploadWorld, mount
from store.test_patient_browser_controls import headers
from store.test_patient_browser_controls import (
    test_stop_quiet_and_two_step_resume_keep_session as preferences_case,
)

from sanad.auth.service import revise
from sanad.concierge import education
from sanad.store._base import StoreBase
from sanad.store.records import (
    CommitRequest,
    CommitResult,
    Forbidden,
    WebSession,
)
from system.test_clock_paths_20_6e import AdvancingClock


@pytest.fixture
def clock() -> AdvancingClock:
    return AdvancingClock(NOW)


def planned_world(store: StoreBase, clock: AdvancingClock) -> PatientWorld:
    w = PatientWorld.create(store, clock)
    assert isinstance(w, PatientWorld)
    w.approve()
    w.seed(revise(w.doctor, clock(), language="en"))
    p = w.named_stub("Synthetic Patient")
    w.patient_scope = p.scope
    # Confirm an actual card before invitation or consent; this is F07's ordering.
    w.dictate(
        "Synthetic Patient, put him on Exforge 5/160 once daily",
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "orders": [
                {
                    "action": "start",
                    "drug": "Exforge",
                    "dose": "5/160",
                    "frequency": "once daily",
                    "action_quote": "put him on",
                }
            ],
        },
        id=400,
    )
    w.tap("✅ Confirm", id=401)
    assert w.rows("mission") and all(r.body["state"] == "awaiting_link" for r in w.rows("mission"))
    pending = w.consent(w.claim(w.invite(p)))
    card = next(i for i in w.intents() if i.template_id == "claim_awaiting_doctor")
    assert card.payload and isinstance(card.payload["reply_markup"], dict)
    keyboard = card.payload["reply_markup"]["inline_keyboard"]
    assert (
        isinstance(keyboard, list)
        and isinstance(keyboard[0], list)
        and isinstance(keyboard[0][0], dict)
    )
    w.tap(raw=str(keyboard[0][0]["callback_data"]), id=102)
    assert w.receipt(102).state == "completed"
    accepted_claim = w.claims.patient_claim(pending.id)
    assert accepted_claim and accepted_claim.state == "approved"
    assert all(r.body["state"] == "open" for r in w.rows("mission"))
    assert any(i.template_id == "binding_confirmed" for i in w.patient_intents())
    w.concierge.synthetic = True
    return w


@pytest.fixture
def browser(store: StoreBase, clock: AdvancingClock) -> Iterator[UploadWorld]:
    w = planned_world(store, clock)
    with w.client() as client:
        assert browser_login(client, w.login_path(PATIENT)).status_code == 303
        session = w.login.session(client.cookies["sanad_session"])
        assert session
        yield mount(w, client, WebSession.model_validate(session.model_dump()))


def education_reply(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # Reviewed synthetic educational fixture, not a claim of clinical validation.
    source = education.source_set()[0].model_copy(
        update={
            "topic_tags": ("Exforge",),
            "permitted_questions": ("what is Exforge for?",),
            "text_en": "Exforge is used to treat high blood pressure.",
            "source_label_en": "Synthetic reviewed medicine information",
            "reviewed_by": "synthetic reviewer",
        }
    )
    monkeypatch.setattr(education, "source_set", lambda: (source,))
    return {"reply": source.lines("en")[0], "kind": "education", "needs_doctor": False}


def test_bound_turns_both_channels(browser: UploadWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    w = browser.world
    from store.medication_fixtures import scripted_barrier_factory

    w.concierge.barrier_model_factory = scripted_barrier_factory
    value = education_reply(monkeypatch)
    model, answer = w.send("what is Exforge for?", value)
    assert model.script.calls and "Source:" in str(answer.payload)
    w.concierge.model_factory = lambda registry, role: ScriptedModel(candidate(value))
    response = browser.client.post(
        "/api/patient/messages",
        json={"text": "what is Exforge for?", "command_id": "education"},
        headers=headers(browser),
    )
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    assert any(
        "Source:" in str(i.get("text"))
        for i in browser.client.get("/api/patient/conversation").json()["items"]
    )
    _, dose = w.send("can I double the dose?")
    assert dose.template_id == "patient_treatment_change_relay"
    response = browser.client.post(
        "/api/patient/messages",
        json={"text": "can I double the dose?", "command_id": "dose"},
        headers=headers(browser),
    )
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    assert any(r.body["kind"] == "QUESTION" for r in w.rows("mission"))
    preferences_case(browser)
    _, stop = w.send("stop reminders")
    assert stop.template_id == "patient_stop_ack" and not w.profile.routine_contact_enabled
    _, resume = w.send("resume")
    assert not w.profile.routine_contact_enabled
    assert resume.payload and resume.payload["reply_markup"]
    keyboard = resume.payload["reply_markup"]
    assert isinstance(keyboard, dict)
    rows = keyboard["inline_keyboard"]
    assert isinstance(rows, list) and isinstance(rows[0], list) and isinstance(rows[0][0], dict)
    raw = str(rows[0][0]["callback_data"])
    assert w.post(callback(raw, PATIENT, 5101)).status_code == 200
    assert w.receipt(5101).state == "completed" and w.profile.routine_contact_enabled
    _, quiet = w.send("quiet hours 22:00 to 07:00")
    consent = w.rows("consent")[0]
    assert consent.body["quiet_hours"] == ["22:00", "07:00"]
    assert quiet.template_id == "patient_quiet_ack"


@pytest.mark.parametrize("conflicts", [1, 2])
def test_touch_conflicts_other_rows_never_revoke(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, conflicts: int
) -> None:
    w = browser.world
    original = w.store.commit_account
    seen = 0

    def conflict(request: CommitRequest) -> CommitResult:
        nonlocal seen
        if request.command.payload.get("type") == "TouchWebSession" and seen < conflicts:
            seen += 1
            return Forbidden()
        return original(request)

    monkeypatch.setattr(w.store, "commit_account", conflict)
    assert browser.client.get("/api/patient/me").status_code == 200
    assert seen == conflicts
    session = w.login.session(browser.client.cookies["sanad_session"])
    assert session and session.revoked_at is None
    # The same failed touch cannot hide a current consent invalidation.
    w.seed(revise(w.profile, w.clock(), consent_active=False))
    assert browser.client.get("/api/patient/me").status_code == 401


def test_refused_turn_completes_and_safely_replies(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = browser.world
    original = w.store.commit

    def refuse(request: CommitRequest) -> CommitResult:
        return (
            Forbidden()
            if request.command.payload.get("executor") == "concierge-v1"
            else original(request)
        )

    monkeypatch.setattr(w.store, "commit", refuse)
    assert w.post(update(PATIENT, "/plan", 6001)).status_code == 200
    assert w.receipt(6001).state == "completed"
    assert any(
        i.template_id == "patient_safety_ack" and i.delivered_text for i in w.patient_intents()
    )


def test_revocation_log_has_reason_and_redacted_path(
    browser: UploadWorld, caplog: pytest.LogCaptureFixture
) -> None:
    w = browser.world
    cookie = browser.client.cookies["sanad_session"]
    session = w.login.session(cookie)
    assert session
    caplog.clear()
    with caplog.at_level("INFO", logger="sanad.web"):
        w.login.revoke(
            session,
            reason="logout",
            path="/api/patients/private-patient/media/private-media?token=private-token",
        )
    assert "reason=logout path=/api/patients/<redacted>/media/<redacted>" in caplog.text
    for private in (cookie, session.id, "private-patient", "private-media", "private-token"):
        assert private not in caplog.text
