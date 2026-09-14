"""No-login demos exercise the shipped shells without any clinical transport."""

from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, expect
from providers.fixtures import png
from store.account_fixtures import PATIENT
from store.concierge_fixtures import PatientWorld
from store.login_fixtures import browser_login
from store.test_admin_boundary import admin_path
from store.test_browser_upload import mount

from browser.conftest import RenderedApp
from sanad.store.records import WebSession
from sanad.web.security import HEADERS

IDENTITY = Path(__file__).with_name("identity_walk.js").read_text()
BANNER = "Synthetic demonstration · Fictional records only"
DEMO_PATHS = {"/demo", "/demo/patient", "/demo/admin"}
ASSETS = Path(__file__).parents[2] / "src/sanad/web/static"


def observe(app: RenderedApp) -> list[tuple[str, str]]:
    app.page.context.clear_cookies()
    assert app.page.context.cookies() == []
    traffic: list[tuple[str, str]] = []
    app.page.on("request", lambda r: traffic.append((r.method, r.url)))
    app.page.add_init_script("""(() => {
      window.demoFetches=[];
      const fetch=window.fetch;
      window.fetch=(url,options)=>{
        window.demoFetches.push({url,credentials:options?.credentials});
        return fetch(url,options);
      };
      Object.defineProperty(document,'cookie',{get(){throw Error('Demo read cookies');}});
      XMLHttpRequest.prototype.open=()=>{throw Error('Demo opened a transport');};
    })();""")
    return traffic


def check_demo(app: RenderedApp, traffic: list[tuple[str, str]]) -> None:
    page = app.page
    expect(page.locator("body")).to_have_attribute("data-demo", "true")
    expect(page.locator(".demo-banner strong")).to_have_text(BANNER)
    assert page.locator(".demo-banner a").all_text_contents() == [
        "Doctor demo",
        "Patient demo",
        "Admin demo",
    ]
    assert set(page.locator(".demo-banner a").evaluate_all("es=>es.map(e=>e.pathname)")) == (
        DEMO_PATHS
    )
    assert page.evaluate(IDENTITY) == []
    assert page.locator("a").evaluate_all(
        "es=>es.every(e=>e.origin===location.origin && "
        "['/demo','/demo/patient','/demo/admin'].includes(e.pathname))"
    )
    assert page.evaluate("demoFetches.length > 0 && demoFetches.every(r=>r.credentials==='omit')")
    assert app.page.context.cookies() == []
    allowed = DEMO_PATHS | {"/assets/" + p.name for p in ASSETS.iterdir()}
    assert traffic
    assert all(
        method == "GET" and url.startswith(app.origin + "/") and urlsplit(url).path in allowed
        for method, url in traffic
    ), traffic


def patient_ready(page: Page) -> None:
    expect(page.locator("#content")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#patient-conversation article")).to_have_count(8)
    expect(page.locator("#patient-uploads p")).to_have_count(3)
    expect(page.locator("#patient-preferences")).to_have_text("Reminders enabled · Africa/Cairo")


def test_demo_patient_controls(rendered: RenderedApp, tmp_path: Path) -> None:
    page = rendered.page
    traffic = observe(rendered)
    page.clock.install(time="2026-09-12T09:59:00Z")
    response = page.goto(rendered.origin + "/demo/patient")
    assert response and response.status == 200
    patient_ready(page)
    expect(page.locator(".account")).to_have_text("Mona Test")
    expect(page.locator("#content")).to_contain_text("Amlodipine")
    expect(page.locator("#content")).to_contain_text("Atorvastatin")
    expect(page.locator("#content")).to_contain_text("Report your blood pressure each morning")
    expect(page.locator("#patient-conversation")).to_contain_text(
        "Your doctor's answer to your question: You can send the photo here."
    )
    expect(page.locator("#patient-conversation")).to_contain_text(
        "A sign-in link was sent on Sep 10, 09:00"
    )
    expect(page.locator("#patient-conversation article").nth(5)).to_contain_text("Read")
    expect(page.locator("#patient-quiet-start")).to_have_value("22:00")
    expect(page.locator("#patient-quiet-end")).to_have_value("07:00")
    expect(page.locator("#patient-older")).to_be_hidden()
    check_demo(rendered, traffic)
    page.screenshot(path=str(tmp_path / "patient-demo.png"), full_page=True)

    # Keep the two-second reply boundary deterministic, including HTML escaping.
    page.clock.pause_at("2026-09-12T10:00:00Z")
    message = "A demo message <img src='/api/patient/me'>"
    page.locator("#patient-message").fill(message)
    page.locator("#patient-message-form button").click()
    expect(page.locator("#patient-conversation article")).to_have_count(9)
    expect(page.locator("#patient-conversation article").last).to_contain_text(message)
    expect(page.locator("#patient-conversation img")).to_have_count(0)
    page.clock.run_for(1999)
    expect(page.locator("#patient-conversation article")).to_have_count(9)
    page.clock.run_for(1)
    expect(page.locator("#patient-conversation article")).to_have_count(10)
    expect(page.locator("#patient-conversation article").last).to_contain_text(
        "This is a demonstration reply."
    )

    page.locator('[role="tab"][aria-controls="patient-settings"]').click()
    switch = page.get_by_role("switch", name="Reminders")
    switch.click()
    expect(switch).to_have_attribute("aria-checked", "false")
    expect(page.locator("#patient-reminder-summary")).to_have_text("Reminders paused")
    switch.click()
    expect(page.get_by_role("button", name="Yes, resume reminders")).to_be_visible()
    expect(switch).to_have_attribute("aria-checked", "false")
    page.get_by_role("button", name="Yes, resume reminders").click()
    expect(switch).to_have_attribute("aria-checked", "true")
    expect(page.locator("#patient-confirm")).to_be_hidden()

    page.locator("#patient-quiet-start").fill("21:00")
    page.locator("#patient-quiet-end").fill("06:00")
    page.get_by_role("button", name="Save quiet hours").click()
    expect(page.locator("#patient-quiet-summary")).to_contain_text("21:00, 06:00")
    page.locator("#refresh").click()
    expect(page.locator("#patient-quiet-summary")).to_contain_text("21:00, 06:00")
    page.locator("#patient-quiet-end").fill("21:00")
    page.get_by_role("button", name="Save quiet hours").click()
    expect(page.locator("#patient-action-result")).to_have_text("Choose two different times.")
    expect(page.locator("#patient-quiet-summary")).to_contain_text("21:00, 06:00")

    page.locator('[role="tab"][aria-controls="content"]').click()
    page.locator("#patient-file").set_input_files(
        {"name": "synthetic.png", "mimeType": "image/png", "buffer": png()}
    )
    page.locator("#patient-caption").fill("My demo upload")
    page.locator("#patient-upload-form button").click()
    progress = page.locator("#patient-upload-progress")
    expect(progress).to_be_visible()
    page.clock.run_for(300)
    expect(progress).to_have_js_property("value", 25)
    page.clock.run_for(600)
    expect(progress).to_be_hidden()
    expect(page.locator("#patient-uploads p")).to_have_count(4)
    expect(page.locator("#patient-uploads p").last).to_contain_text("Read")
    expect(page.locator("#patient-conversation article").last).to_contain_text("My demo upload")
    expect(page.locator("#patient-conversation article").last).to_contain_text("Read")
    expect(page.locator("#patient-action-result")).to_have_text("Read by Sanad")
    page.clock.run_for(10000)  # Ordinary polling also stays in memory.
    check_demo(rendered, traffic)

    page.reload()
    patient_ready(page)
    expect(page.locator("#patient-quiet-start")).to_have_value("22:00")
    expect(page.locator("#patient-quiet-end")).to_have_value("07:00")
    expect(page.locator("#patient-message")).to_have_value("")
    check_demo(rendered, traffic)


def test_demo_admin_controls(rendered: RenderedApp, tmp_path: Path) -> None:
    page = rendered.page
    traffic = observe(rendered)
    response = page.goto(rendered.origin + "/demo/admin")
    assert response and response.status == 200
    rows = page.locator("#admin-applications > section")
    expect(rows).to_have_count(4)
    expect(rows.nth(0)).to_contain_text("Family medicine · Cairo · applied on Sep 12, 2026")
    expect(rows.nth(0).locator(".application-state")).to_contain_text("Waiting for your decision")
    expect(rows.nth(1).locator(".application-state")).to_contain_text("Approved and working")
    expect(rows.nth(2).locator(".application-state")).to_contain_text("Suspended:")
    expect(page.locator("#admin-logout")).to_have_count(0)
    check_demo(rendered, traffic)
    page.screenshot(path=str(tmp_path / "admin-demo.png"), full_page=True)
    page.on("dialog", lambda dialog: dialog.accept())
    rows.nth(0).get_by_role("button", name="Approve", exact=True).click()
    expect(rows.nth(0).locator(".application-state")).to_contain_text("Approved and working")
    rows.nth(0).get_by_role("button", name="Suspend", exact=True).click()
    expect(rows.nth(0).locator(".application-state")).to_contain_text("Suspended:")
    rows.nth(0).get_by_role("button", name="Reinstate", exact=True).click()
    expect(rows.nth(0).locator(".application-state")).to_contain_text("Approved and working")
    rows.nth(2).get_by_role("button", name="Reinstate", exact=True).click()
    expect(rows.nth(2).locator(".application-state")).to_contain_text("Approved and working")
    check_demo(rendered, traffic)
    page.reload()
    expect(rows.nth(0).locator(".application-state")).to_contain_text("Waiting for your decision")
    expect(rows.nth(2).locator(".application-state")).to_contain_text("Suspended:")
    rows.nth(0).get_by_role("button", name="Reject", exact=True).click()
    rows.nth(0).get_by_label("Identity could not be verified").check()
    rows.nth(0).get_by_role("button", name="Confirm rejection").click()
    expect(rows.nth(0).locator(".application-state")).to_contain_text("Rejected")
    expect(rows.nth(0).get_by_role("button", name="Approve", exact=True)).to_have_count(0)
    expect(page.locator("#admin-result")).to_have_text("Rejected.")
    check_demo(rendered, traffic)
    page.reload()
    expect(rows.nth(0).locator(".application-state")).to_contain_text("Waiting for your decision")
    page.locator('[data-theme-set="dark"]').click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.locator('[data-theme-set="light"]').click()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    for name, path in [
        ("Doctor demo", "/demo"),
        ("Patient demo", "/demo/patient"),
        ("Admin demo", "/demo/admin"),
    ]:
        page.get_by_role("link", name=name, exact=True).click()
        expect(page).to_have_url(rendered.origin + path)
        if path == "/demo":
            expect(page.locator("#content")).to_have_attribute("aria-busy", "false")
        elif path == "/demo/patient":
            patient_ready(page)
        else:
            expect(rows).to_have_count(4)
        check_demo(rendered, traffic)


def test_demo_routes_ignore_sessions_and_store(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = rendered.world
    forged = "forged-demo-session"
    cookies = [{}, {"sanad_session": forged}, *rendered.cookies.values()]

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("A public demo accessed a session or store")

    with monkeypatch.context() as guard:
        guard.setattr(world.login, "session", forbidden)
        guard.setattr(world.login, "revoke", forbidden)
        for name in dir(world.store):
            if not name.startswith("__") and callable(getattr(world.store, name)):
                guard.setattr(world.store, name, forbidden)
        with world.client() as client:
            for cookie in cookies:
                client.cookies.clear()
                client.cookies.update(cookie)
                for path in sorted(DEMO_PATHS) + [
                    "/assets/" + p.name
                    for p in ASSETS.iterdir()
                    if p.suffix in {".css", ".js", ".ttf", ".json"}
                ]:
                    response = client.get(path)
                    assert response.status_code == 200, path
                    assert "set-cookie" not in response.headers
                    for header, value in HEADERS.items():
                        assert response.headers[header] == value
                    if path in DEMO_PATHS:
                        assert BANNER in response.text and 'data-demo="true"' in response.text

        page = rendered.page
        page.context.clear_cookies()
        page.context.add_cookies(
            [{"name": "sanad_session", "value": forged, "url": rendered.origin}]
        )
        for path in sorted(DEMO_PATHS):
            navigation = page.goto(rendered.origin + path)
            assert navigation and navigation.status == 200
            assert navigation.request.all_headers()["cookie"] == "sanad_session=" + forged
            assert "set-cookie" not in navigation.all_headers()
            for header, value in HEADERS.items():
                assert navigation.all_headers()[header.lower()] == value
            expect(page.locator(".demo-banner strong")).to_have_text(BANNER)
            if path == "/demo/admin":
                expect(page.locator("#admin-applications > section")).to_have_count(4)
            elif path == "/demo/patient":
                patient_ready(page)
            else:
                expect(page.locator("#content")).to_have_attribute("aria-busy", "false")
            assert page.context.cookies()[0]["value"] == forged

    # The same browser's forged cookie cannot open authenticated data or pages.
    for path in ["/api/me", "/api/patient/me", "/api/admin/applications", "/a/patients/demo"]:
        refused = page.request.get(rendered.origin + path)
        assert refused.status == 401, path


def test_demo_does_not_unlock_real_apis(world: PatientWorld) -> None:
    with world.client() as setup:
        assert browser_login(setup, world.login_path(PATIENT, id=8810)).status_code == 303
        session = world.login.session(setup.cookies["sanad_session"])
        assert session
        mount(world, setup, WebSession.model_validate(session.model_dump()))
    with world.client() as client:
        for path in DEMO_PATHS:
            assert client.get(path).status_code == 200
        for path in [
            "/api/patient/me",
            "/api/patient/plan",
            "/api/patient/conversation",
            "/api/patient/uploads",
            "/api/patient/preferences",
            "/api/admin/applications",
            "/pp",
            "/admin",
        ]:
            assert client.get(path).status_code == 401, path
        for path in [
            "/api/patient/messages",
            "/api/patient/uploads",
            "/api/patient/preferences",
            "/api/patient/preferences/confirm",
            "/api/admin/applications/demo/approve",
            "/api/admin/applications/demo/reject",
            "/api/admin/doctors/demo/suspend",
            "/api/admin/doctors/demo/reinstate",
            "/api/admin/logout",
        ]:
            assert client.post(path, json={}).status_code == 401, path

    # Real admin sessions must still be revoked by the central wrong-role guard,
    # including near-matches and non-GET requests to the public routes.
    for method, path in [
        ("GET", "/api/me"),
        ("GET", "/api/patient/me"),
        ("GET", "/a/patients/demo"),
        ("GET", "/demo/patient/extra"),
        ("GET", "/assets/nested/browser.js"),
        ("POST", "/demo"),
        ("POST", "/demo/patient"),
        ("POST", "/demo/admin"),
        ("POST", "/assets/browser.js"),
        ("HEAD", "/demo/patient"),
    ]:
        with world.client() as client:
            assert browser_login(client, admin_path(world)).status_code == 303
            raw = client.cookies["sanad_session"]
            response = client.request(method, path)
            assert response.status_code == 403, (method, path)
            session = world.login.session(raw)
            assert session and session.revoked_at is not None
            for header, value in HEADERS.items():
                assert response.headers[header] == value
