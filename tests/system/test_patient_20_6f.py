"""Real patient routing, education gate and cross-channel session pin regressions."""

from datetime import timedelta

import pytest
from providers.fixtures import ScriptedModel, candidate
from store.account_fixtures import PATIENT, update
from store.executors_15_fixtures import doctor
from store.login_fixtures import browser_login
from store.test_browser_upload import UploadWorld
from store.test_patient_browser_controls import headers

from sanad.auth.commands import RevokeBinding
from sanad.concierge import education
from sanad.store._base import Check, Write
from sanad.store.records import AuthorizationUnavailable
from system.test_walkthrough_20_6e import browser as browser
from system.test_walkthrough_20_6e import clock as clock
from system.test_walkthrough_20_6e import education_reply


@pytest.mark.parametrize("channel", ["telegram", "browser"])
@pytest.mark.parametrize("approved", [False, True])
def test_education_source_or_honest_question(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, channel: str, approved: bool
) -> None:
    w = browser.world
    from store.medication_fixtures import scripted_barrier_factory

    w.concierge.barrier_model_factory = scripted_barrier_factory
    w.concierge.synthetic = approved
    question = "what is Exforge for?"
    if not approved:
        assert not education.retrieve(question, synthetic=False)
    value = (
        education_reply(monkeypatch)
        if approved
        else {
            "reply": "Your doctor prescribed: Exforge, 5/160, once daily",
            "kind": "plan",
            "needs_doctor": False,
        }
    )
    w.concierge.model_factory = lambda registry, role: ScriptedModel(candidate(value))
    if channel == "telegram":
        _, reply = w.send(question, value)
        text = str(reply.payload)
    else:
        response = browser.client.post(
            "/api/patient/messages",
            headers=headers(browser),
            json={
                "text": question,
                "command_id": "education6f",
            },
        )
        assert response.status_code == 200 and response.json()["status"] == "accepted"
        text = str(browser.client.get("/api/patient/conversation").json())
    questions = [r for r in w.rows("mission") if r.body["kind"] == "QUESTION"]
    if approved:
        assert "treat high blood pressure" in text and "Source:" in text
        assert not questions
    else:
        assert "not yet available from the clinic" in text and "passed to your doctor" in text
        assert len(questions) == 1


@pytest.mark.parametrize("channel", ["telegram", "browser"])
def test_second_dose_after_answer_and_authorization_conflict(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    w = browser.world
    w.send("can I double the dose?")
    doctor(w, "/questions", 7101)
    doctor(w, "/answer 1 Please follow the recorded plan.", 7102)
    assert any(
        r.body["state"] == "fulfilled" for r in w.rows("mission") if r.body["kind"] == "QUESTION"
    )
    original = w.store._atomic
    conflicts = 3

    def conflict(writes: list[Write], checks: list[Check]) -> bool:
        nonlocal conflicts
        if not writes and checks and checks[0].key.pk.startswith("SUBJECT#") and conflicts:
            conflicts -= 1
            return False
        return original(writes, checks)

    monkeypatch.setattr(w.store, "_atomic", conflict)
    message = update(PATIENT, "can I double the dose?", 7103)

    def send() -> int:
        if channel == "telegram":
            return int(w.post(message).status_code)
        return int(
            browser.client.post(
                "/api/patient/messages",
                headers=headers(browser),
                json={"text": "can I double the dose?", "command_id": "second-dose6f"},
            ).status_code
        )

    assert send() == 200  # Three transient conflicts recover inside the request.
    assert conflicts == 0
    assert send() == 200
    if channel == "telegram":
        assert w.receipt(7103).state == "completed" and w.receipt(7103).scope == w.patient_scope
    questions = [r for r in w.rows("mission") if r.body["kind"] == "QUESTION"]
    assert len(questions) == 2 and sum(r.body["state"] == "open" for r in questions) == 1
    w.clock.advance(timedelta(minutes=6))
    w.send("can I double the dose?", id=7104)
    assert len([r for r in w.rows("mission") if r.body["kind"] == "QUESTION"]) == 2


def test_telegram_preferences_repin_open_browser(browser: UploadWorld) -> None:
    w = browser.world
    cookie = browser.client.cookies["sanad_session"]
    w.send("stop reminders")
    assert browser.client.get("/api/patient/preferences").json()["reminders"] == "paused"
    _, resume = w.send("resume")
    assert "Yes, resume reminders" in str(resume.payload)
    w.press(resume, id=7201)
    w.send("quiet hours 21:00 to 06:00")
    response = browser.client.get("/api/patient/preferences")
    assert response.status_code == 200 and response.json()["quiet_hours"] == ["21:00", "06:00"]
    session = w.login.session(cookie)
    assert (
        session and not session.revoked_at and session.consent_version == w.profile.consent_version
    )


def test_busy_authorization_preserves_browser_session(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = browser.world
    original = w.store._atomic

    def conflict(writes: list[Write], checks: list[Check]) -> bool:
        return False if not writes else original(writes, checks)

    monkeypatch.setattr(w.store, "_atomic", conflict)
    with pytest.raises(AuthorizationUnavailable):
        w.login.require(browser.client.cookies["sanad_session"], "patient")
    assert browser.client.get("/api/patient/preferences").status_code == 409
    session = w.login.session(browser.client.cookies["sanad_session"])
    assert session and session.revoked_at is None
    monkeypatch.setattr(w.store, "_atomic", original)
    assert browser.client.get("/api/patient/preferences").status_code == 200


def test_invitation_name_and_revocation_reason(
    browser: UploadWorld, caplog: pytest.LogCaptureFixture
) -> None:
    w = browser.world
    card = next(i for i in w.intents() if i.template_id == "claim_awaiting_doctor")
    assert "Synthetic Patient" in str(card.payload) and PATIENT not in str(card.payload)
    cookie = browser.client.cookies["sanad_session"]
    session = w.login.session(cookie)
    assert session
    assert (
        w.claims.revoke_binding(
            RevokeBinding(
                command_id="revoke6f",
                actor=w.owner,
                patient_id=w.patient_scope.patient_id,
                reason_code="identity_error",
            )
        ).status
        == "accepted"
    )
    with caplog.at_level("INFO", logger="sanad.web"):
        response = browser.client.get("/api/patient/preferences")
    assert response.status_code == 401
    assert response.json()["detail"] == (
        "Your access changed elsewhere. Please sign in again from Telegram."
    )
    assert session.id[:8] in caplog.text and cookie not in caplog.text
    assert session.id not in caplog.text


def test_doctor_contact_preferences_and_signed_out_page(browser: UploadWorld) -> None:
    w = browser.world
    w.send("stop reminders")
    w.send("quiet hours 21:00 to 06:00")
    with w.client() as doctor_client:
        assert (
            browser_login(doctor_client, w.login_path(w.owner.subject, id=7500)).status_code == 303
        )
        record = doctor_client.get("/api/patients/" + w.patient_scope.patient_id)
        assert record.status_code == 200
        assert record.json()["contact_preferences"] == {
            "reminders": "paused",
            "quiet_hours": ["21:00", "06:00"],
            "timezone": "Africa/Cairo",
        }
        assert w.post(update(w.owner.subject, "/logout", 7501)).status_code == 200
        response = doctor_client.get("/a")
        assert response.status_code == 401
        assert "You signed out. Send /login in Telegram for a new link." in response.text
        assert "This action cannot be completed" not in response.text
