"""Parallel rendered patient operations using deployed strong point-read semantics."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, local
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect
from providers.fixtures import png
from store.account_fixtures import PATIENT, update
from store.test_browser_upload import mount

from browser.conftest import RenderedApp
from sanad.auth.commands import RevokeBinding
from sanad.store._base import Check, Write
from sanad.store.dynamodb import DynamoStore, _decode, _encode
from sanad.store.keys import Key
from sanad.store.records import WebSession


@pytest.mark.parametrize("rendered", [(1440, "light"), (375, "dark")], indirect=True)
def test_6f_doctor_preferences_and_logout(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    w, page = rendered.world, rendered.page
    rendered.detail()
    page.get_by_role("tab", name="Medicines", exact=True).click()
    preferences = page.get_by_role("tabpanel").locator(".consent-binding")
    expect(preferences).to_contain_text("Reminders: Enabled")
    expect(preferences).to_contain_text("22:00 to 08:00")

    with ThreadPoolExecutor(max_workers=1) as telegram:
        telegram.submit(w.send, "stop reminders").result()
        telegram.submit(w.send, "quiet hours 21:00 to 06:00").result()
    page.locator("#refresh").click()
    expect(preferences).to_contain_text("Reminders: Paused")
    expect(preferences).to_contain_text("Quiet hours: 21:00 to 06:00")
    expect(preferences).to_contain_text("Africa/Cairo")
    expect(preferences).not_to_contain_text("Reminders: Enabled")
    expect(preferences).not_to_contain_text("22:00 to 08:00")

    with ThreadPoolExecutor(max_workers=1) as telegram:
        response = telegram.submit(w.post, update(w.owner.subject, "/logout", 7501)).result()
    assert response.status_code == 200
    page.locator("#refresh").click()
    expect(page.locator("#feedback [role=alert]")).to_have_text(
        "You signed out. Send /login in Telegram for a new link."
    )
    expect(page.locator("#content")).to_be_empty()
    expect(page.locator("#content")).to_have_attribute("aria-busy", "false")
    assert rendered.errors and all(
        error == "Failed to load resource: the server responded with a status of 401 (Unauthorized)"
        for error in rendered.errors
    )
    rendered.errors.clear()  # The deliberate logout generated these HTTP errors.


@pytest.mark.parametrize("rendered", [(1440, "light"), (375, "dark")], indirect=True)
def test_6f_parallel_patient(rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    w, page = rendered.world, rendered.page
    rendered.login(PATIENT)
    cookie = rendered.cookies[PATIENT]["sanad_session"]
    session = w.login.session(cookie)
    assert session
    mount(w, w.client(), WebSession.model_validate(session.model_dump()))
    original_read = w.store._read
    reads = 0

    class StrongClient:
        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal reads
            assert kwargs["ConsistentRead"] is True
            reads += 1
            key = _decode(kwargs["Key"])
            row = original_read(Key(key["PK"], key["SK"]))
            return {"Item": _encode(row)} if row else {}

    deployed_reads = DynamoStore(StrongClient(), "synthetic", clock=w.clock)
    local_request = local()
    upload_read, preference_done, armed = Event(), Event(), Event()
    race_seen = False
    original_require = w.login.require

    def require(raw_cookie: str, role: str, *, path: str = "auth") -> Any:
        local_request.upload = armed.is_set() and path == "/api/patient/uploads"
        try:
            return original_require(raw_cookie, role, path=path)  # type: ignore[call-overload]
        finally:
            local_request.upload = False

    def read(key: Key) -> Any:
        nonlocal race_seen
        row = deployed_reads._read(key)
        if (
            not race_seen
            and getattr(local_request, "upload", False)
            and row
            and row.get("entity_type") == "patient_profile"
        ):
            race_seen = True
            upload_read.set()
            assert preference_done.wait(10), "Parallel preference save never completed"
        return row

    original_atomic = w.store._atomic

    def atomic(writes: list[Write], checks: list[Check]) -> bool:
        accepted = original_atomic(writes, checks)
        if accepted and any(r.item.get("entity_type") == "consent" for r in writes):
            preference_done.set()
        return accepted

    monkeypatch.setattr(w.login, "require", require)
    monkeypatch.setattr(w.store, "_read", read)
    monkeypatch.setattr(w.store, "_atomic", atomic)
    page.clock.install(time=w.clock())
    page.goto(rendered.origin + "/pp")
    other = page.context.new_page()
    other.goto(rendered.origin + "/pp")
    expect(other.locator("#patient-preferences")).to_contain_text("Reminders enabled")
    page.locator("#patient-file").set_input_files(
        {
            "name": "synthetic.png",
            "mimeType": "image/png",
            "buffer": png(),
        }
    )
    armed.set()
    page.locator("#patient-upload-form button").click()
    for _ in range(50):
        if upload_read.is_set():
            break
        page.wait_for_timeout(100)
    assert upload_read.is_set(), page.locator("#patient-action-result").inner_text()
    other.locator("#patient-quiet-start").fill("21:00")
    other.locator("#patient-quiet-end").fill("06:00")
    with other.expect_response(
        lambda r: r.url.endswith("/api/patient/preferences") and r.request.method == "POST"
    ) as saved:
        other.locator("#patient-quiet-form button").click()
    assert saved.value.status == 200
    expect(other.locator("#patient-quiet-summary")).to_contain_text("21:00, 06:00")
    expect(page.locator("#patient-uploads")).to_contain_text("Received")
    assert race_seen and preference_done.is_set()
    for i in range(24):
        w.clock.advance(timedelta(seconds=5))
        page.clock.run_for(5000)
        if i == 5:
            other.locator("#patient-quiet-start").fill("22:00")
            with other.expect_response(
                lambda r: r.url.endswith("/api/patient/preferences") and r.request.method == "POST"
            ) as saved:
                other.locator("#patient-quiet-form button").click()
            assert saved.value.status == 200
        if i == 10:
            page.locator("#patient-message").fill("/plan")
            page.locator("#patient-message-form button").click()
            expect(page.locator("#patient-conversation")).to_contain_text("Synthetic Doctor")
        assert page.request.get(rendered.origin + "/api/patient/preferences").status == 200
    session = w.login.session(cookie)
    assert (
        session
        and session.revoked_at is None
        and session.consent_version == w.profile.consent_version
    )
    assert reads > 100
    expect(page.locator("#patient-quiet-summary")).to_contain_text("22:00, 06:00")
    other.close()


@pytest.mark.parametrize("rendered", [(1440, "light"), (375, "dark")], indirect=True)
def test_6f_patient_wording(rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    w, page = rendered.world, rendered.page
    rendered.login(PATIENT)
    page.clock.install(time=w.clock())
    page.goto(rendered.origin + "/pp")
    page.locator("#patient-message").fill("/plan")
    page.locator("#patient-message-form button").click()
    expect(page.locator("#patient-conversation")).to_contain_text("Synthetic Doctor")
    patient = w.claims.patient(w.patient_scope.doctor_id, w.patient_scope.patient_id)
    assert patient
    local_time = w.clock().astimezone(ZoneInfo(patient.timezone))
    expect(page.locator("#patient-conversation small").first).to_have_text(
        "Today " + local_time.strftime("%H:%M")
    )
    expect(page.locator("#patient-conversation small").first).not_to_contain_text("T12:")
    expect(page.get_by_role("switch", name="Reminders")).to_have_count(1)
    expect(page.get_by_role("button", name="Resume reminders", exact=True)).to_have_count(0)
    page.get_by_role("switch", name="Reminders").click()
    expect(page.locator("#patient-reminder-summary")).to_have_text("Reminders paused")
    expect(page.locator("#patient-preferences")).to_contain_text("Reminders paused")
    page.get_by_role("switch", name="Reminders").click()
    expect(page.get_by_role("button", name="Yes, resume reminders")).to_have_count(1)
    page.get_by_role("button", name="Yes, resume reminders").click()
    expect(page.locator("#patient-reminder-summary")).to_have_text("Reminders enabled")
    page.clock.set_fixed_time(w.clock() + timedelta(days=1))
    page.locator("#refresh").click()
    expect(page.locator("#patient-conversation small").first).to_have_text(
        f"{local_time:%b} {local_time.day}, {local_time:%H:%M}"
    )
    assert (
        w.claims.revoke_binding(
            RevokeBinding(
                command_id="rendered-revoke6f",
                actor=w.owner,
                patient_id=w.patient_scope.patient_id,
                reason_code="identity_error",
            )
        ).status
        == "accepted"
    )
    page.locator("#refresh").click()
    expect(page.locator("#patient-action-result")).to_have_text(
        "Your access changed elsewhere. Please sign in again from Telegram."
    )
    expect(page.locator("#patient-message-form")).to_have_count(0)
    expect(page.locator("#content")).to_have_attribute("aria-busy", "false")
    assert rendered.errors and all(
        error == "Failed to load resource: the server responded with a status of 401 (Unauthorized)"
        for error in rendered.errors
    )
    rendered.errors.clear()  # The intentional access withdrawal generated these HTTP errors.
