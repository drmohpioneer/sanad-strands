"""Real patient editor and doctor history on the current shell and merged Settings tab."""

import pytest
from playwright.sync_api import expect
from store.account_fixtures import PATIENT
from store.test_patient_schedule import details, seed_schedule

from browser.conftest import RenderedApp


def editor(rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    seed_schedule(rendered.world)
    rendered.login(PATIENT)
    response = rendered.page.goto(rendered.origin + "/pp")
    assert response and response.status == 200
    page = rendered.page
    expect(page.locator("#patient-schedules fieldset")).to_have_count(1)
    # The released parallel shell is optional on this branch, mandatory when present.
    settings = page.get_by_role(
        "tab",
        name="Settings" if page.locator("html").get_attribute("lang") == "en" else "الإعدادات",
        exact=True,
    )
    if settings.count():
        settings.click()
        expect(settings).to_have_attribute("aria-selected", "true")
    root = page.locator("#patient-schedules")
    expect(root.locator("h2")).to_have_count(0)
    root.get_by_role("button", name="Change reading times", exact=True).click()
    expect(root.locator('input[type="time"]')).to_have_count(2)
    root.locator('input[type="time"]').nth(0).fill("09:00")
    root.locator('input[type="time"]').nth(1).fill("21:00")
    root.get_by_role("button", name="Save", exact=True).click()
    expect(root.get_by_role("button", name="Yes, change", exact=True)).to_be_visible()
    expect(root.get_by_role("status")).to_contain_text("10:00, 22:00 → 09:00, 21:00")
    expect(root.get_by_role("status")).to_contain_text("Your earlier readings stay as they are.")
    assert not details(rendered.world).time_history


def test_patient_schedule29_editor_history(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor(rendered, monkeypatch)
    root = rendered.page.locator("#patient-schedules")
    root.get_by_role("button", name="Yes, change", exact=True).click()
    expect(root.get_by_role("button", name="Yes, change", exact=True)).to_have_count(0)
    assert details(rendered.world).time_history[-1].new_times == ("09:00", "21:00")
    rendered.login(rendered.world.owner.subject)
    rendered.detail()
    # History is on the plan; it is a record view, with no proactive doctor message.
    history = rendered.page.locator(".schedule-history")
    expect(history).to_contain_text("Patient changed reading times from")
    expect(history).to_contain_text("starting")
    assert not [
        r for r in rendered.world.rows("outbound_intent") if r.body.get("audience") == "doctor"
    ]


def test_patient_schedule29_cancel(rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch) -> None:
    editor(rendered, monkeypatch)
    before = len(rendered.world.patient_intents())
    root = rendered.page.locator("#patient-schedules")
    root.get_by_role("button", name="No", exact=True).click()
    expect(root.get_by_role("button", name="Yes, change", exact=True)).to_have_count(0)
    assert not details(rendered.world).time_history
    assert len(rendered.world.patient_intents()) == before


@pytest.mark.parametrize("delivery", ["immediate", "polled"])
@pytest.mark.parametrize("outcome", ["grant", "stale", "cancel"])
def test_patient_schedule29_r6_receipt_replies(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch, delivery: str, outcome: str
) -> None:
    from typing import Any

    from playwright.sync_api import Route
    from store.test_patient_schedule import occupy_quiet, quiet_consent, seed_quiet

    from sanad.concierge.templates import render

    seed_quiet(rendered.world)
    editor(rendered, monkeypatch)
    root = rendered.page.locator("#patient-schedules")
    responses: list[dict[str, Any]] = []
    polls: list[dict[str, Any]] = []

    def confirm(route: Route) -> None:
        response = route.fetch()
        assert response.status == 200
        data = response.json()
        responses.append(data)
        if delivery == "polled":
            route.fulfill(response=response, json={"queued": True, "token": data["token"]})
        else:
            route.fulfill(response=response)

    def poll(route: Route) -> None:
        response = route.fetch()
        assert response.status == 200
        data = response.json()
        polls.append(data)
        route.fulfill(response=response)

    rendered.page.route("**/api/patient/preferences/confirm", confirm)
    rendered.page.route("**/api/patient/preferences?token=*", poll)
    if outcome == "cancel":
        before = len(rendered.world.patient_intents())
        root.get_by_role("button", name="No", exact=True).click()
        expect(root.get_by_role("status")).to_be_empty()
        assert len(rendered.world.patient_intents()) == before
        assert not details(rendered.world).time_history
    else:
        root.get_by_role("button", name="Yes, change", exact=True).click()
        allow = root.get_by_role("button", name="Allow this reminder: Blood pressure", exact=True)
        expect(allow).to_be_visible()
        old = quiet_consent(rendered.world)
        if outcome == "stale":
            offered = next(
                r
                for r in rendered.world.rows("patient_action")
                if r.body.get("quiet_schedule_generation") and not r.body.get("consumed_at")
            )
            index = int(str(offered.body["slot_id"]).rsplit("-", 1)[1])
            occupy_quiet(rendered.world, index)
        allow.click()
        key = "patient_quiet_ack" if outcome == "grant" else "patient_callback_stale"
        expect(root.get_by_role("status")).to_contain_text(render(key, "en"))
        if outcome == "grant":
            expect(allow).to_be_visible()
            assert quiet_consent(rendered.world).version == old.version + 1
            assert len(quiet_consent(rendered.world).scheduled_slot_consents) == 1
        else:
            expect(allow).to_have_count(0)
            assert quiet_consent(rendered.world) == old
        assert responses[-1]["schedule_reply"]["text"].startswith(render(key, "en"))
    assert responses
    if delivery == "polled":
        assert len(polls) == len(responses)
        for immediate, polled in zip(responses, polls, strict=True):
            assert polled.get("schedule_reply") == immediate.get("schedule_reply")
    if outcome == "cancel":
        assert "schedule_reply" not in responses[-1]
