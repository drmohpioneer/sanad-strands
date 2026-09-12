"""Real loopback HTTPS app, accepted login, synthetic stores and captured providers."""

import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
from domain_fixtures import NOW
from harness import FakeClock
from playwright.sync_api import Browser, Page, Route, sync_playwright
from store import evidence_fixtures as f
from store.account_fixtures import ADMIN, PATIENT
from store.concierge_fixtures import PatientWorld
from store.executors_15_fixtures import question
from store.login_fixtures import browser_login
from store.medication_fixtures import confirm, send
from store.test_admin_boundary import admin_path
from store.test_corrections_19 import fact_change
from store.test_monitor import monitor

from sanad.steward.corrections import current_facts
from sanad.store.memory import MemoryStore
from sanad.web.settings import WebSettings


@pytest.fixture
def world(request: pytest.FixtureRequest) -> PatientWorld:
    clock = FakeClock(NOW)
    world = f.world(MemoryStore(clock=clock), clock)
    scenario = getattr(request, "param", "empty")
    if scenario == "monitor":
        monitor(world, count=1)
        world.send("BP 120/80", id=1700)
        old = current_facts(world.store, world.patient_scope)[0]
        assert fact_change(world, old.id, "BP 130/85").status == "accepted"
    elif scenario == "evidence":
        f.mission(world, order_refs=())
        lab = f.lab(
            items=[
                {"name": "Potassium", "value": "5.0", "unit": "mmol/L"},
                {"name": "Creatinine", "value": "1.0", "unit": "mg/dL"},
            ]
        )
        f.providers(world, lab, lab)
        assert f.upload(world) == "accepted"
    elif scenario == "medication":
        confirm(world, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
        send(world, "I started Atorvastatin")
    elif scenario == "question":
        question(world)
    else:
        assert scenario == "empty"
    return world


@dataclass
class RenderedApp:
    browser: Browser
    world: PatientWorld
    origin: str
    page: Page
    errors: list[str]

    cookies: dict[str, dict[str, str]]
    entry_paths: list[str]

    def login(self, subject: str) -> None:
        for name, value in self.cookies[subject].items():
            self.page.context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "url": self.origin,
                        "secure": True,
                        "sameSite": "Strict",
                    }
                ]
            )

    def detail(self) -> None:
        response = self.page.goto(f"{self.origin}/a/patients/{self.world.patient_scope.patient_id}")
        assert response and response.status == 200
        self.page.locator('#content[aria-busy="false"]').wait_for()
        self.page.evaluate("document.fonts.ready")
        assert self.page.evaluate("window.devicePixelRatio") == 2
        assert self.page.locator("#feedback").inner_text() == ""


@pytest.fixture(params=[(1440, "dark"), (1440, "light"), (375, "dark"), (375, "light")])
def rendered(
    request: pytest.FixtureRequest, world: PatientWorld, tmp_path: Path, loopback_network: None
) -> Iterator[RenderedApp]:
    cookies = {}
    for event_id, subject in enumerate((world.owner.subject, PATIENT), start=9600):
        with world.client() as client:
            assert browser_login(client, world.login_path(subject, id=event_id)).status_code == 303
            cookies[subject] = dict(client.cookies)
    with world.client() as client:
        assert browser_login(client, admin_path(world)).status_code == 303
        cookies[ADMIN] = dict(client.cookies)
    entry_paths = []
    if request.node.name.startswith("test_depth_entry"):
        entry_paths = [
            world.login_path(world.owner.subject, id=9801),
            world.login_path(PATIENT, id=9802),
            admin_path(world),
            "/p/" + world.invite(world.stub()).token.get_secret_value(),
        ]
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"https://127.0.0.1:{listener.getsockname()[1]}"
        world.app.state.web_settings = WebSettings(
            public_base_url=origin, bot_username="synthetic_sanad_bot"
        )
        server = uvicorn.Server(
            uvicorn.Config(
                world.app,
                ssl_keyfile=str(key),
                ssl_certfile=str(cert),
                access_log=False,
                log_level="error",
                ws="none",
            )
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "Fixture HTTPS server did not start"
            with httpx.Client(
                verify=False, trust_env=False, cookies=cookies[world.owner.subject]
            ) as probe:
                detail = probe.get(f"{origin}/a/patients/{world.patient_scope.patient_id}")
                assert detail.status_code == 200
                record = probe.get(f"{origin}/api/patients/{world.patient_scope.patient_id}")
                assert record.status_code == 200 and record.json()["correction_authority"]
            with sync_playwright() as driver:
                # Missing driver/browser or a denied launch is a failure, never a skip.
                with driver.chromium.launch(headless=True) as browser:
                    with browser.new_context(
                        ignore_https_errors=True,
                        device_scale_factor=2,
                        viewport={"width": request.param[0], "height": 1000},
                        color_scheme=request.param[1],
                        timezone_id="UTC",
                    ) as context:
                        errors: list[str] = []

                        def local_only(route: Route) -> None:
                            if route.request.url.startswith(origin + "/"):
                                route.continue_()
                            else:
                                errors.append("Unexpected non-fixture browser request")
                                route.abort()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on(
                            "console",
                            lambda msg: errors.append(msg.text) if msg.type == "error" else None,
                        )
                        app = RenderedApp(
                            browser, world, origin, page, errors, cookies, entry_paths
                        )
                        app.login(world.owner.subject)
                        yield app
                        assert errors == [], errors
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()


@pytest.fixture(autouse=True)
def depth_english_surface(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """18e measures the released English surface; older bilingual cases retain their locale."""
    if request.node.name.startswith("test_depth_"):
        monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
