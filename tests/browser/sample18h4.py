"""Ratified 18h-4 groups, browser clocks, real charts and upload expansion."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from playwright._impl._api_structures import SetCookieParam
from playwright.sync_api import expect
from store.account_fixtures import PATIENT

from browser.conftest import RenderedApp
from browser.depth18e import RETINA
from browser.words18g import (
    NOW,
    TODAY,
    fulfill_projection,
    goto,
    mission,
    projected,
    record,
    review,
)


@RETINA
def test_sample18h4_groups_and_return(rendered: RenderedApp) -> None:
    app, page = rendered, rendered.page
    data = record(
        app,
        missions=[mission("open", id="late"), mission("open", id="today", due_at=TODAY)],
        reviews=[review("incident_response"), review("result_review", id="review")],
    )
    projected(app, data)
    for group in ("needs", "all", "settled", "danger", "pending_review", "overdue", "due_today"):
        goto(app, "/a?filter=" + group)
        control = page.locator(
            f'[data-summary="{group}"]'
            if group in {"danger", "pending_review", "overdue", "due_today"}
            else f'#filter [data-filter="{group}"]'
        )
        expect(control).to_have_attribute("aria-pressed", "true")
        expect(
            page.locator('#filter [aria-pressed="true"], [data-summary][aria-pressed="true"]')
        ).to_have_count(1)
        expect(page.locator(".patient-row")).to_have_count(0 if group == "settled" else 1)
        control.click()
        expect(control).to_have_attribute("aria-pressed", "true")
        page.locator("#refresh").click()
        expect(control).to_have_attribute("aria-pressed", "true")
        page.reload()
        expect(control).to_have_attribute("aria-pressed", "true")
    page.locator("#search").fill("no match")
    expect(page.locator(".patient-row")).to_have_count(0)
    expect(page.locator('[data-summary="due_today"] strong')).to_have_text("1")
    page.locator("#search").fill("Synthetic")
    page.locator(".patient-row").click()
    page.locator(".patient-row.open + .detail a.primary").click()
    expect(page.locator("#what-to-do")).to_have_count(1)
    expect(page.locator("#back")).to_be_visible()
    back_box = page.locator("#back").bounding_box()
    profile_box = page.locator("#patient-profile").bounding_box()
    assert back_box is not None and profile_box is not None
    assert back_box["y"] < profile_box["y"]
    page.locator("#back").click()
    print(
        "return-url",
        page.url,
        page.evaluate("sessionStorage.getItem('sanad-list-return')"),
        flush=True,
    )
    expect(page.locator('[data-summary="due_today"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#search")).to_have_value("Synthetic")
    goto(app, "/demo?filter=all")
    page.locator('[data-summary="danger"]').click()
    page.locator("#search").fill("Nadia")
    page.reload()
    expect(page.locator('[data-summary="danger"]')).to_have_attribute("aria-pressed", "true")
    page.locator(".patient-row").click()
    page.locator(".patient-row.open + .detail a.primary").click()
    page.locator("#refresh").click()
    page.locator("#back").click()
    expect(page.locator("#search")).to_have_value("Nadia")
    expect(page.locator('[data-summary="danger"]')).to_have_attribute("aria-pressed", "true")
    goto(app, "/a?filter=invalid")
    expect(page.locator('#filter [data-filter="needs"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#refresh span")).to_be_visible()
    assert page.locator("html").get_attribute("dir") == "ltr"
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")


@pytest.mark.parametrize("rendered", [(1440, "dark")], indirect=True)
@pytest.mark.parametrize("zone", ["Africa/Cairo", "America/New_York", "UTC", "absent"])
def test_sample18h4_browser_times(
    rendered: RenderedApp, zone: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    actual = "UTC" if zone == "absent" else zone
    with rendered.browser.new_context(
        ignore_https_errors=True, timezone_id=actual, viewport={"width": 1440, "height": 1000}
    ) as context:
        cookies: list[SetCookieParam] = []
        for cookie in rendered.page.context.cookies():
            cookie_payload: SetCookieParam = {
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": cookie["domain"],
                "path": cookie["path"],
                "expires": cookie["expires"],
                "httpOnly": cookie["httpOnly"],
                "secure": cookie["secure"],
                "sameSite": cookie["sameSite"],
            }
            if "partitionKey" in cookie:
                cookie_payload["partitionKey"] = cookie["partitionKey"]
            cookies.append(cookie_payload)
        context.add_cookies(cookies)
        if zone == "absent":
            context.add_init_script(
                "const resolve=Intl.DateTimeFormat.prototype.resolvedOptions;"
                "Intl.DateTimeFormat.prototype.resolvedOptions=function(){"
                "return {...resolve.call(this),timeZone:undefined}}"
            )
        page = context.new_page()
        app = replace(rendered, page=page)
        now = datetime.fromisoformat("2026-11-01T05:30:00+00:00")  # NY fall-back boundary
        projected(
            app,
            record(
                app,
                missions=[
                    mission(
                        "open",
                        id="now",
                        due_at=now.isoformat(),
                        details={
                            "kind": "MONITOR",
                            "metric": "Weight",
                            "unit": "kg",
                            "slots": [now.isoformat()],
                            "readings": [
                                {
                                    "value": "72",
                                    "slot": 0,
                                    "observed_at": now.isoformat(),
                                    "received_at": now.isoformat(),
                                    "reading_index": 0,
                                }
                            ],
                        },
                    ),
                    mission("open", id="later", due_at=(now + timedelta(minutes=30)).isoformat()),
                    mission("open", id="late", due_at=(now - timedelta(seconds=1)).isoformat()),
                ],
                reviews=[review("result_review", review_at=now.isoformat())],
            ),
        )
        page.clock.set_fixed_time(now)
        goto(app, "/a?filter=due_today")
        expect(page.locator('[data-summary="due_today"] strong')).to_have_text("1")
        expect(page.locator('[data-summary="overdue"] strong')).to_have_text("1")
        goto(app, "/a/preferences")
        expect(page.locator(".preferences")).to_contain_text("Times shown in " + actual)
        expect(page.locator('#language option[value="ar"]')).to_have_text("Arabic")
        goto(app, "/a/patients/" + app.world.patient_scope.patient_id)
        expected = now.astimezone(ZoneInfo(actual)).strftime("%H:%M")
        texts = page.locator('time[datetime="' + now.isoformat() + '"]').all_text_contents()
        assert texts and any(expected in text for text in texts)
        assert all(expected in text for text in texts)
        page.get_by_role("tab", name="Requests", exact=True).click()
        expect(page.locator(".reading-table caption")).not_to_contain_text(actual)
        midnight = datetime(2026, 11, 2, tzinfo=ZoneInfo(actual))
        projected(
            app,
            record(
                app,
                missions=[
                    mission("open", id="boundary", due_at=midnight.isoformat()),
                    mission(
                        "open", id="next-day", due_at=(midnight + timedelta(days=1)).isoformat()
                    ),
                    mission(
                        "open", id="past", due_at=(midnight - timedelta(seconds=1)).isoformat()
                    ),
                ],
            ),
        )
        page.clock.set_fixed_time(midnight)
        goto(app, "/a?filter=due_today")
        expect(page.locator('[data-summary="due_today"] strong')).to_have_text("1")
        expect(page.locator('[data-summary="overdue"] strong')).to_have_text("1")
        page.clock.set_fixed_time(now)
        stamp = "2026-11-01T00:30:00+00:00"
        admin = json.loads(
            (Path(__file__).parents[2] / "src/sanad/web/static/demo-admin.json").read_text()
        )
        for row in admin:
            row.update(applied_at=stamp, decided_at=stamp, suspended_at=stamp)
        page.route(app.origin + "/assets/demo-admin.json", partial(fulfill_projection, admin))
        page.goto(app.origin + "/demo/admin")
        date = datetime.fromisoformat(stamp).astimezone(ZoneInfo(actual)).strftime("%b %-d, %Y")
        expect(page.locator(".application-card").first).to_contain_text("applied on " + date)
        for card in page.locator(".application-card").all():
            card.click()
            expect(card.locator(".detail")).to_contain_text(date)
        app.login(PATIENT)
        fixture = json.loads(
            (Path(__file__).parents[2] / "src/sanad/web/static/demo-patient.json").read_text()
        )
        fixture["plan"]["next_missions"] = [
            {"title": "Recorded request", "due_at": now.isoformat()}
        ]
        fixture["plan"]["preferences"]["resume_at"] = now.isoformat()
        fixture["plan"]["reading_history"] = [
            {
                "metric": "Weight",
                "unit": "kg",
                "value": "72",
                "observed_at": now.isoformat(),
                "received_at": now.isoformat(),
            }
        ]
        for path, payload in (
            ("/api/patient/plan", fixture["plan"]),
            ("/api/patient/preferences", fixture["preferences"]),
            (
                "/api/patient/agreement",
                {
                    "accepted_at": now.isoformat(),
                    "text": "Frozen synthetic agreement.",
                    "version": "synthetic",
                },
            ),
            ("/api/patient/uploads", [{"received_at": now.isoformat(), "state": "accepted"}]),
            (
                "/api/patient/conversation",
                {
                    "items": [
                        {
                            "id": "one",
                            "at": now.isoformat(),
                            "text": "Recorded text",
                            "direction": "inbound",
                        }
                    ],
                    "cursor": None,
                },
            ),
        ):
            page.route(app.origin + path, partial(fulfill_projection, payload))
        goto(app, "/pp")
        assert expected in page.locator("#patient-conversation").inner_text()
        assert expected in page.locator("#patient-uploads").inner_text()
        assert expected in page.locator("#patient-agreement").inner_text()
        expect(page.locator("#patient-quiet-summary")).to_contain_text(
            fixture["preferences"]["timezone"]
        )
        expect(page.locator("#patient-week .trend span")).to_have_count(1)
        page.locator('[aria-controls="patient-settings"]').click()
        expect(page.locator("#patient-week")).to_be_hidden()
        expect(page.locator("#patient-agreement")).to_be_hidden()
        expect(page.locator("#patient-quiet-start")).to_have_value(
            fixture["preferences"]["quiet_hours"][0]
        )
        assert page.locator("#content>section").last.get_attribute("id") == "patient-agreement"


@RETINA
def test_sample18h4_charts_and_uploads(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    app, page = rendered, rendered.page
    rows = [
        {
            "slot": None,
            "reading_index": i,
            "value": f"{110 + i}/{70 + i}",
            "observed_at": (NOW - timedelta(hours=11 - i)).isoformat(),
            "received_at": NOW.isoformat(),
        }
        for i in range(12)
    ]
    data = record(
        app,
        missions=[
            mission(
                "fulfilled",
                details={
                    "kind": "MONITOR",
                    "metric": "blood_pressure",
                    "unit": "mmHg",
                    "slots": [],
                    "readings": rows,
                },
            )
        ],
    )
    projected(app, data)
    goto(app, "/a/patients/" + data["patient_id"])
    expect(page.locator("#patient-profile .trend span")).to_have_count(20)
    expect(page.locator("#patient-profile .reading-values li")).to_have_count(10)
    expect(page.locator("#patient-profile .reading-values li").first).to_contain_text("112/72")
    expect(page.locator("#patient-profile .hot")).to_have_count(0)
    expect(page.locator("#what-to-do")).to_have_count(1)
    cells = page.locator("#patient-profile .cellp").evaluate_all(
        "es=>es.map(e=>({x:e.getBoundingClientRect().x,y:e.getBoundingClientRect().y,w:e.getBoundingClientRect().width,p:getComputedStyle(e).padding}))"
    )
    assert all(c["p"] == "22px" for c in cells)
    viewport = page.viewport_size
    assert viewport is not None
    if viewport["width"] > 900:
        assert max(c["w"] for c in cells) - min(c["w"] for c in cells) < 1
        assert cells[0]["x"] < cells[1]["x"] < cells[2]["x"]
    else:
        assert cells[0]["y"] < cells[1]["y"] < cells[2]["y"]
    app.login(PATIENT)
    fixture = json.loads(
        (Path(__file__).parents[2] / "src/sanad/web/static/demo-patient.json").read_text()
    )
    history = [{"metric": "blood_pressure", "unit": "mmHg", **r} for r in rows]
    history += [
        {
            "metric": "Weight",
            "unit": "kg",
            "value": "72",
            "observed_at": None,
            "recorded_at": NOW.isoformat(),
        },
        {"metric": "Weight", "unit": "lb", "value": "160", "observed_at": NOW.isoformat()},
        {
            "metric": "Glucose",
            "unit": None,
            "value": "not readable",
            "observed_at": NOW.isoformat(),
        },
        {"metric": "Glucose", "unit": None, "value": "7" * 400, "observed_at": NOW.isoformat()},
        {"metric": "Weight", "unit": "kg", "value": "999", "observed_at": "2026-09-06T23:59:59Z"},
        {"metric": "Weight", "unit": "kg", "value": "888", "observed_at": "2026-09-13T00:00:00Z"},
    ]
    fixture["plan"]["reading_history"] = history
    page.route(app.origin + "/api/patient/plan", partial(fulfill_projection, fixture["plan"]))
    uploads = [
        {
            "received_at": (NOW - timedelta(minutes=i)).isoformat(),
            "state": "accepted" if i else "processing",
        }
        for i in range(12)
    ]
    page.route(app.origin + "/api/patient/uploads", lambda route: route.fulfill(json=uploads))
    goto(app, "/pp")
    expect(page.locator("#patient-week .trend span")).to_have_count(26)
    expect(page.locator("#patient-week .reading-values li")).to_have_count(16)
    expect(page.locator("#patient-week")).to_contain_text("7" * 400)
    expect(page.locator("#patient-week")).not_to_contain_text("Infinity")
    expect(page.locator("#patient-week")).to_contain_text("Recorded on")
    expect(page.locator("#patient-week")).not_to_contain_text("999")
    expect(page.locator("#patient-week")).not_to_contain_text("888")
    expect(page.locator("#patient-week .hot")).to_have_count(0)
    assert page.locator("#patient-week").evaluate(
        "e=>e.previousElementSibling.contains(document.getElementById('patient-reminder-summary'))"
    )
    assert page.locator("#patient-week").evaluate(
        "e=>e.nextElementSibling.id==='patient-questions'"
    )
    expect(page.locator("#patient-uploads p")).to_have_count(5)
    toggle = page.locator("#patient-uploads-toggle")
    expect(toggle).to_have_attribute("aria-controls", "patient-uploads")
    toggle.focus()
    page.keyboard.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#patient-uploads p")).to_have_count(12)
    page.clock.run_for(5100)
    expect(page.locator("#patient-uploads p")).to_have_count(12)
    page.locator("#refresh").click()
    expect(page.locator("#patient-uploads p")).to_have_count(12)
    toggle.click()
    expect(page.locator("#patient-uploads p")).to_have_count(5)
    uploads[:] = uploads[:3]
    page.locator("#refresh").click()
    expect(page.locator("#patient-uploads p")).to_have_count(3)
    expect(toggle).to_have_count(0)
    uploads.clear()
    page.locator("#refresh").click()
    expect(page.locator("#patient-uploads p")).to_have_count(0)
    expect(toggle).to_have_count(0)


@pytest.mark.parametrize("rendered", [(1440, "dark")], indirect=True)
def test_sample18h4_alerts_and_motion(rendered: RenderedApp) -> None:
    page = rendered.page
    rows = [
        {
            "slot": None,
            "value": "180/100",
            "observed_at": "2026-09-11T07:00:00Z",
            "received_at": TODAY,
        },
        {
            "slot": None,
            "value": "130/70",
            "observed_at": "2026-09-12T07:00:00Z",
            "received_at": TODAY,
        },
        {
            "slot": None,
            "value": "110/90",
            "observed_at": "2026-09-12T08:00:00Z",
            "received_at": TODAY,
        },
    ]
    alert = {
        "id": "explicit-alert",
        "status": "active",
        "current_version": {
            "type": "value_alert",
            "structured_instruction": {
                "text": "Recorded alert",
                "metric": "blood_pressure",
                "unit": "mmHg",
                "comparator": "ge",
                "threshold": "120/80",
            },
        },
    }
    for variant in ("matching", "inactive", "unit", "absent"):
        order = json.loads(json.dumps(alert))
        if variant == "inactive":
            order["status"] = "superseded"
        if variant == "unit":
            order["current_version"]["structured_instruction"]["unit"] = "other"
        data = record(
            rendered,
            missions=[
                mission(
                    "fulfilled",
                    details={
                        "kind": "MONITOR",
                        "metric": "blood_pressure",
                        "unit": "mmHg",
                        "slots": [],
                        "readings": rows,
                    },
                )
            ],
            orders=[] if variant == "absent" else [order],
        )
        projected(rendered, data)
        goto(rendered, "/a/patients/" + data["patient_id"])
        expect(page.locator("#patient-profile .hot")).to_have_count(
            2 if variant == "matching" else 0
        )
        expect(page.locator("#patient-profile .reading-values li")).to_have_count(3)
        expect(page.locator("#patient-profile .reading-values li").nth(1)).to_contain_text("130/70")
        page.emulate_media(reduced_motion="reduce")
        expect(page.locator("#patient-profile .trend:not(.in)")).to_have_count(0)
        assert page.locator("#patient-profile .trend").evaluate_all(
            "es=>es.every(e=>e.getAttribute('aria-hidden')==='true')"
        )
