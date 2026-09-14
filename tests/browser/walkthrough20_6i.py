"""Rendered preference replies hydrate independently of failing history refreshes."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from playwright.sync_api import Route, expect
from store.account_fixtures import PATIENT

from browser.conftest import RenderedApp
from sanad.channels.telegram.router import RouteResult
from sanad.presentation.patient_browser import CATALOG


@pytest.mark.parametrize("rendered", [(390, "light"), (1440, "dark")], indirect=True)
def test_6i_hidden_and_queued_preferences(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sanad.web.api_patient as api

    w, page = rendered.world, rendered.page
    rendered.login(PATIENT)
    page.goto(rendered.origin + "/pp")
    expect(page.locator("#patient-stop")).to_have_attribute("aria-checked", "true")
    page.locator('[role="tab"][aria-controls="patient-settings"]').click()
    expect(page.locator("#patient-confirm")).to_be_hidden()
    assert page.locator("#patient-confirm").evaluate("e=>getComputedStyle(e).display") == "none"
    from sanad.channels.telegram.router import route_receipt

    original = route_receipt
    pending: list[Any] = []

    def queue(runtime: Any, key: Any, **kwargs: Any) -> RouteResult:
        pending.append(key)
        return RouteResult(route="busy", status="processing")

    monkeypatch.setattr(api, "route_receipt", queue)
    # Both unrelated projections fail after hydration. Preference replies must still render.
    for path in ("conversation", "uploads"):
        page.route(
            "**/api/patient/" + path,
            lambda route: route.fulfill(status=409, json={"reason": "store_busy"}),
        )
    polls = 0

    def complete(route: Route) -> None:
        nonlocal polls
        polls += 1
        if polls % 2 == 0 and pending:
            with ThreadPoolExecutor() as pool:
                pool.submit(original, w.runtime, pending.pop(0), owner="synthetic-worker").result()
        route.continue_()

    page.route("**/api/patient/preferences?token=*", complete)
    with page.expect_response(
        lambda r: r.url.endswith("/api/patient/preferences") and r.request.method == "POST"
    ) as saved:
        page.locator("#patient-stop").click()
    assert saved.value.json()["queued"] and saved.value.json()["token"]
    expect(page.locator("#patient-stop")).to_have_attribute("aria-checked", "false")
    assert polls >= 2
    expect(page.locator("#patient-confirm")).to_be_hidden()
    page.locator("#patient-stop").click()
    expect(page.locator("#patient-confirm")).to_be_visible()
    assert not page.locator("#patient-confirm").is_disabled()
    page.locator("#patient-confirm").click()
    expect(page.locator("#patient-stop")).to_have_attribute("aria-checked", "true")
    expect(page.locator("#patient-confirm")).to_be_hidden()
    page.locator("#patient-quiet-start").fill("22:30")
    page.locator("#patient-quiet-end").fill("07:30")
    page.locator("#patient-quiet-form button").click()
    expect(page.locator("#patient-quiet-summary")).to_contain_text("22:30, 07:30")
    expect(page.locator("#patient-quiet-start")).to_have_value("22:30")
    expect(page.locator("#patient-quiet-end")).to_have_value("07:30")
    assert page.request.get(rendered.origin + "/api/patient/preferences").json()["quiet_hours"] == [
        "22:30",
        "07:30",
    ]
    assert all(
        error == "Failed to load resource: the server responded with a status of 409 (Conflict)"
        for error in rendered.errors
    ), rendered.errors
    rendered.errors.clear()  # Only the deliberately failing history/upload projections.


@pytest.mark.parametrize("rendered", [(390, "light")], indirect=True)
@pytest.mark.parametrize("stalled", [False, True])
def test_6i_preference_poll_has_time_limit(rendered: RenderedApp, stalled: bool) -> None:
    page = rendered.page
    rendered.login(PATIENT)
    page.clock.install(time=rendered.world.clock())
    page.goto(rendered.origin + "/pp")
    expect(page.locator("#patient-stop")).to_have_attribute("aria-checked", "true")
    state = page.request.get(rendered.origin + "/api/patient/preferences").json()
    page.route(
        "**/api/patient/preferences",
        lambda route: (
            route.fulfill(json={"queued": True, "token": "synthetic"})
            if route.request.method == "POST"
            else route.fulfill(json=state)
        ),
    )
    polls = 0

    def poll(route: Route) -> None:
        nonlocal polls
        polls += 1
        if not stalled:
            route.fulfill(json=state | {"queued": True})

    page.route("**/api/patient/preferences?token=*", poll)
    page.locator('[role="tab"][aria-controls="patient-settings"]').click()
    page.locator("#patient-stop").click()
    for _ in range(21):
        page.clock.run_for(1000)
        page.wait_for_timeout(20)
    language = page.locator("html").get_attribute("lang")
    expect(page.locator("#patient-action-result")).to_have_text(
        CATALOG["patient_browser.still_working"]["ar" if language == "ar" else "en"]
    )
    before = polls
    page.clock.run_for(5000)
    assert polls == before and 0 < polls <= 20
    if stalled:
        assert polls == 1
    expect(page.locator("#patient-confirm")).to_be_hidden()
    expect(page.locator("#patient-stop")).to_have_attribute("aria-checked", "true")


@pytest.mark.parametrize("rendered", [(390, "light")], indirect=True)
def test_6i_passive_poll_preserves_quiet_edit(rendered: RenderedApp) -> None:
    page = rendered.page
    rendered.login(PATIENT)
    page.clock.install(time=rendered.world.clock())
    page.goto(rendered.origin + "/pp")
    expect(page.locator("#patient-quiet-start")).to_have_value("22:00")
    page.locator('[role="tab"][aria-controls="patient-settings"]').click()
    state = page.request.get(rendered.origin + "/api/patient/preferences").json()

    def preference(route: Route) -> None:
        if route.request.method == "GET":
            # A changed zone is a witness that the passive reply rendered; its
            # unchanged quiet hours must not replace the edit in progress.
            route.fulfill(json=state | {"timezone": "UTC"})
        else:
            route.continue_()

    page.route("**/api/patient/preferences", preference)
    page.locator("#patient-quiet-start").fill("21:30")
    page.locator("#patient-quiet-end").fill("07:30")
    page.clock.run_for(5000)
    expect(page.locator("#patient-quiet-summary")).to_contain_text("UTC")
    expect(page.locator("#patient-quiet-start")).to_have_value("21:30")
    expect(page.locator("#patient-quiet-end")).to_have_value("07:30")
    with page.expect_response(
        lambda r: r.url.endswith("/api/patient/preferences") and r.request.method == "POST"
    ) as saved:
        page.locator("#patient-quiet-form button").click()
    assert saved.value.json()["preferences"]["quiet_hours"] == ["21:30", "07:30"]
    expect(page.locator("#patient-quiet-summary")).to_contain_text("21:30, 07:30")
    expect(page.locator("#patient-quiet-summary")).to_contain_text("Africa/Cairo")
