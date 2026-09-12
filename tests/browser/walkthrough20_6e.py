"""Real rendered replies and two virtual minutes of polling against the real app."""

from datetime import timedelta

import pytest
from playwright.sync_api import expect
from store.account_fixtures import PATIENT

from browser.conftest import RenderedApp
from sanad.presentation.patient_browser import CATALOG
from sanad.store.records import CommitRequest, CommitResult, Forbidden


def test_6e_reply_and_immediate_danger(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, page = rendered.world, rendered.page
    rendered.login(PATIENT)
    page.goto(rendered.origin + "/pp")
    expect(page.locator("#patient-message")).to_be_visible()
    page.locator("#patient-message").fill("/plan")
    page.locator("#patient-message-form button").click()
    expect(page.locator("#patient-conversation")).to_contain_text("Synthetic Doctor")

    # A dangerous submission must never enter slow ordinary routing.
    def slow(*args: object, **kwargs: object) -> None:
        raise AssertionError("Emergency response waited for ordinary routing")

    monkeypatch.setattr("sanad.web.api_patient.route_receipt", slow)
    page.locator("#patient-message").fill("chest pain now")
    with page.expect_response("**/api/patient/messages") as response:
        page.locator("#patient-message-form button").click()
    emergency = response.value.json()["emergency"]
    assert emergency and "123" in emergency
    expect(page.locator("#patient-action-result")).to_have_text(emergency)
    assert len(w.rows("incident")) == 1


@pytest.mark.parametrize("role", ["patient", "doctor"])
def test_6e_two_minutes_polling_and_saves(
    rendered: RenderedApp, role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, page = rendered.world, rendered.page
    original = w.store.commit_account
    touches = 0

    def conflict(request: CommitRequest) -> CommitResult:
        nonlocal touches
        if request.command.payload.get("type") == "TouchWebSession":
            touches += 1
            if touches % 3 == 0:
                return Forbidden()  # A checked authority row conflicted, session version unchanged.
        return original(request)

    monkeypatch.setattr(w.store, "commit_account", conflict)
    rendered.login(PATIENT if role == "patient" else w.owner.subject)
    page.clock.install(time=w.clock())
    page.goto(rendered.origin + ("/pp" if role == "patient" else "/a"))
    endpoint = "/api/patient/preferences" if role == "patient" else "/api/preferences"
    # Advance browser timers and the server clock together; all fetches and saves are real.
    for i in range(24):
        w.clock.advance(timedelta(seconds=5))
        page.clock.run_for(5000)
        if role == "patient" and i % 6 == 0:
            page.locator("#patient-quiet-start").fill("22:00")
            page.locator("#patient-quiet-end").fill("07:00")
            with page.expect_response(
                lambda r: r.url.endswith(endpoint) and r.request.method == "POST"
            ) as saved:
                page.locator("#patient-quiet-form button").click()
            assert saved.value.status == 200
        if role == "doctor" and i % 6 == 0:
            saved = page.evaluate("""async () => {
                const p = await (await fetch('/api/preferences')).json();
                const csrf = decodeURIComponent(document.cookie.split('; ')
                    .find(s=>s.startsWith('sanad_csrf=')).slice(11));
                return (await fetch('/api/preferences', {method:'POST',
                    headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
                    body:JSON.stringify({digest_time:'17:00',digest_packing:'one',
                        expected_version:p.version,command_id:crypto.randomUUID()})})).status;
            }""")
            assert saved == 200
        response = page.request.get(rendered.origin + endpoint)
        assert response.status == 200
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    session = w.login.session(cookies["sanad_session"])
    assert touches > 24 and session and session.revoked_at is None
    if role == "doctor":
        assert page.request.get(rendered.origin + endpoint).json()["digest_time"] == "17:00"
    if role == "patient":
        assert page.request.get(rendered.origin + endpoint).json()["quiet_hours"] == [
            "22:00",
            "07:00",
        ]


def test_6e_still_working_is_bounded(rendered: RenderedApp) -> None:
    page = rendered.page
    rendered.login(PATIENT)
    page.clock.install(time=rendered.world.clock())
    page.route("**/api/patient/messages", lambda route: route.fulfill(json={"status": "received"}))
    page.goto(rendered.origin + "/pp")
    page.locator("#patient-message").fill("hello")
    page.locator("#patient-message-form button").click()
    language = page.locator("html").get_attribute("lang")
    assert language in {"ar", "en"}
    expect(page.locator("#patient-action-result")).to_have_text(
        CATALOG["patient_browser.pending"]["ar" if language == "ar" else "en"]
    )
    page.clock.run_for(25000)
    expect(page.locator("#patient-action-result")).to_have_text(
        CATALOG["patient_browser.still_working"]["ar" if language == "ar" else "en"]
    )
    expect(page.locator("#patient-action-result")).to_have_count(1)
