"""Whole-page behavior and real-source facts for Addendum 18h-3."""

import json
from functools import partial
from pathlib import Path
from typing import Any

from playwright.sync_api import expect

from browser.conftest import RenderedApp
from browser.depth18e import RETINA, ready
from browser.words18g import (
    NOW,
    PAST,
    TODAY,
    fulfill_projection,
    goto,
    mission,
    projected,
    record,
    review,
)


def test_sample18h3_demo_cards(rendered: RenderedApp) -> None:
    page = rendered.page
    page.clock.set_fixed_time(NOW)
    goto(rendered, "/demo")
    fixture = json.loads((Path(__file__).parents[2] / "src/sanad/web/static/demo.json").read_text())
    assert len(fixture) == 18
    assert [r["patient_id"] for r in fixture] == [f"demo-{i:03}" for i in range(18)]
    names = [r["display_name"] for r in fixture]
    assert len(set(names)) == 18 and all(len(n.split()) == 2 and n.isascii() for n in names)
    assert [(r["display_name"], str(r["age"])) for r in fixture[:6]] == [
        ("Nadia Farouk", "61"),
        ("Omar Zaki", "54"),
        ("Hassan Gamal", "58"),
        ("Layla Shafik", "47"),
        ("Karim Adel", "66"),
        ("Mona Rashad", "39"),
    ]
    needs = page.locator(".patient-row").count()
    assert 4 <= needs <= 7
    expect(page.locator("h1.htitle")).to_have_text("Eighteen patients, six need you now")
    expect(page.locator(".hsub")).to_have_text(
        "Everything below is sorted by what is urgent, then how long someone has been "
        "waiting for you, not by name."
    )
    expect(page.locator(".hsub b")).to_have_text(
        "what is urgent, then how long someone has been waiting for you"
    )
    expect(page.locator("#search")).to_have_attribute("placeholder", "Find a patient by name")
    expect(page.locator("#filter button")).to_have_text(["Needs me", "All 18", "Settled"])
    expect(
        page.locator("caption,th,[data-sort],.filter-summary,.toolbar [data-clear],#next")
    ).to_have_count(0)
    expect(page.locator("#freshness")).to_be_empty()
    page.locator('#filter [data-filter="settled"]').click()
    settled = page.locator(".patient-row").count()
    page.locator('#filter [data-filter="all"]').click()
    assert needs + settled == page.locator(".patient-row").count() == 18
    for colour in ("red", "amber", "cool", "green"):
        assert page.locator(".patient-row .chip." + colour).count() > 0
    rows = page.locator(".patient-row")
    nadia = rows.filter(has_text="Nadia Farouk")
    nadia.click()
    expect(nadia).to_have_attribute("aria-expanded", "true")
    panel = page.locator(".patient-row.open + .detail")
    expect(panel.locator(".kv span")).to_have_text(
        [
            "Blood pressure, 5 hours ago",
            "Blood pressure, yesterday",
            "Medicines on the plan",
            "Open requests",
            "Documents sent",
        ]
    )
    expect(panel.locator(".kv b")).to_have_text(["158/96 mmHg", "141/88 mmHg", "1", "2", "1"])
    expect(panel.locator(".acts a")).to_have_count(1)
    nadia.focus()
    page.keyboard.press("Enter")
    expect(nadia).to_have_attribute("aria-expanded", "false")
    page.keyboard.press("Space")
    expect(nadia).to_have_attribute("aria-expanded", "true")
    omar = rows.filter(has_text="Omar Zaki")
    if page.viewport_size and page.viewport_size["width"] > 900:
        omar.locator(".go").click()
    else:
        expect(omar.locator(".go")).to_be_hidden()
        omar.click()
    expect(nadia).to_have_attribute("aria-expanded", "false")
    expect(omar).to_have_attribute("aria-expanded", "true")
    expect(panel.locator(".kv span")).to_have_text(
        ["Medicines on the plan", "Open requests", "Documents sent"]
    )
    page.locator(".patient-row.open + .detail a.primary").click()
    expect(page.locator("#title")).to_have_text("Omar Zaki")
    expect(page.locator("#what-to-do")).to_be_visible()


def test_sample18h3_headline_and_url(rendered: RenderedApp) -> None:
    page = rendered.page
    for changes, headline in [
        ({}, "One patient, nobody needs you now"),
        ({"reviews": [review("question_answer")]}, "One patient, one needs you now"),
    ]:
        projected(rendered, record(rendered, **changes))
        goto(rendered, "/a?filter=unknown&q=Synthetic&page=1")
        expect(page.locator("h1")).to_have_text(headline)
        expect(page.locator('#filter [aria-pressed="true"]')).to_have_attribute(
            "data-filter", "needs"
        )
        assert "q=Synthetic" in page.url and "page=1" in page.url
        page.locator('#filter [data-filter="all"]').click()
        expect(page.locator(".patient-row")).to_have_count(1)
        page.locator("#search").fill("No matches")
        expect(page.locator("h1")).to_have_text(headline)
        page.locator("[data-clear]").click()
        expect(page.locator("#search")).to_have_value("")
        expect(page.locator('#filter [aria-pressed="true"]')).to_have_attribute(
            "data-filter", "all"
        )


def test_sample18h3_chip_sources_and_actions(rendered: RenderedApp) -> None:
    reviews = {
        "incident_response": ("red", "Respond now"),
        "question_answer": ("red", "Answer the question"),
        "result_review": ("amber", "Read the result"),
        "unmet_objective": ("amber", "Decide what next"),
        "followup_disposition": ("amber", "Decide what next"),
        "media_failure": ("amber", "Ask for a new photo"),
        "delivery_failure": ("amber", "Check contact"),
        "correction_disposition": ("cool", "Confirm the change"),
        "evidence_association": ("cool", "Link the document"),
        "binding_review": ("cool", "Check the link"),
        "intake_clarification": ("cool", "Clarify the file"),
        "coverage_review": ("cool", "Check cover"),
    }
    cases: list[tuple[dict[str, Any], str, str]] = [
        ({"reviews": [review(kind)]}, colour, label) for kind, (colour, label) in reviews.items()
    ]
    cases += [
        ({"reviews": [review(kind, state="acknowledged")]}, "cool", "Still open")
        for kind in reviews
    ]
    for state, colour, label in [
        ("overdue", "amber", "Nudge the patient"),
        ("missing", "amber", "Nudge the patient"),
        ("blocked", "amber", "Check contact"),
        ("unreachable", "amber", "Check contact"),
        ("proposed", "cool", "Confirm the request"),
        ("awaiting_link", "green", "Nothing needed"),
        ("waiting_patient", "green", "Nothing needed"),
    ]:
        cases.append(({"missions": [mission(state)]}, colour, label))
    cases += [
        ({"missions": [mission("open", due_at=TODAY)]}, "cool", "Due today"),
        (
            {"missions": [mission("fulfilled", fulfillment_validity="invalidated_pending_review")]},
            "cool",
            "Confirm the request",
        ),
        (
            {
                "followups": [
                    {
                        "id": "f",
                        "kind": "MEDICATION_DAY3",
                        "state": "contact_suppressed",
                        "due_at": PAST,
                    }
                ]
            },
            "amber",
            "Check contact",
        ),
        (
            {
                "followups": [
                    {"id": "f", "kind": "MEDICATION_DAY3", "state": "overdue", "due_at": PAST}
                ]
            },
            "amber",
            "Nudge the patient",
        ),
        ({}, "green", "Nothing needed"),
        ({"contact_status": "unreachable"}, "amber", "Check contact"),
        ({"contact_status": "frozen"}, "amber", "Check contact"),
    ]
    page = rendered.page
    for changes, colour, label in cases:
        data = record(rendered, age=None, **changes)
        projected(rendered, data)
        goto(rendered, "/a?filter=all")
        row = page.locator(".patient-row")
        expect(row.locator(".chip." + colour)).to_have_text(label)
        expect(row.locator(".chip em")).to_have_count(1)
        expect(row.locator(".who2 small")).to_have_count(0)
        row.click()
        panel = page.locator(".patient-row.open + .detail")
        kind = changes.get("reviews", [{}])[0].get("review_kind")
        extra = kind in {"question_answer", "result_review", "evidence_association"}
        expect(panel.locator(".acts a")).to_have_count(2 if extra else 1)
        if extra:
            expected = (
                "/a/inbox#questions"
                if kind == "question_answer"
                else f"/a/patients/{data['patient_id']}#tab=documents&item=not-projected"
            )
            expect(panel.locator(".acts a").nth(1)).to_have_attribute("href", expected)
        expect(panel.locator(".kv b").last).to_have_text("0")


@RETINA
def test_sample18h3_patient_tabs_and_bubbles(rendered: RenderedApp) -> None:
    page = ready(rendered, "/demo/patient")
    expect(page.locator("#patient-tabs [role=tab]")).to_have_text(["Your care", "Settings"])
    expect(page.locator("#patient-settings")).to_be_hidden()
    expect(page.locator("#content")).to_have_attribute("role", "tabpanel")
    assert (
        page.locator("#patient-controls > :nth-child(3)").get_attribute("id") == "patient-settings"
    )
    for kind, side in [("doc", "start"), ("pat", "end")]:
        bubbles = page.locator(".msg." + kind + " .b")
        assert bubbles.count() >= 2
        for bubble in bubbles.all():
            assert bubble.evaluate(
                """(e, side)=>{
              const r=e.getBoundingClientRect(),p=e.parentElement.getBoundingClientRect();
              const avatar=e.parentElement.querySelector('.av').getBoundingClientRect();
              const mid=p.x+p.width/2;
              return side==='start'
                ? r.left<mid && Math.abs(r.left-avatar.right-10)<.5
                  && Math.abs(avatar.left-p.left)<.5
                : r.right>mid && Math.abs(avatar.left-r.right-10)<.5
                  && Math.abs(avatar.right-p.right)<.5;
            }""",
                side,
            )
    tabs = page.locator("#patient-tabs [role=tab]")
    tabs.first.focus()
    page.keyboard.press("ArrowRight")
    expect(tabs.nth(1)).to_be_focused()
    expect(page.locator("#patient-settings")).to_be_visible()
    expect(page.locator("#content,.patient-talk,.patient-upload")).to_have_count(3)
    for selector in ("#content", ".patient-talk", ".patient-upload"):
        expect(page.locator(selector)).to_be_hidden()
    expect(page.get_by_role("switch", name="Reminders")).to_be_visible()
    page.get_by_role("switch", name="Reminders").click()
    expect(page.locator("#patient-stop")).to_have_attribute("aria-checked", "false")
    tabs.nth(1).focus()
    page.keyboard.press("Home")
    expect(page.locator("#content")).to_be_visible()
    expect(page.locator("#patient-settings")).to_be_hidden()


def test_sample18h3_admin_details(rendered: RenderedApp) -> None:
    page = rendered.page
    page.goto(rendered.origin + "/demo/admin")
    page.locator("#admin-applications .summary-strip").wait_for()
    cards = page.locator(".application-card")
    rejected = cards.filter(has_text="Identity could not be verified")
    rejected.click()
    expect(rejected).to_have_attribute("aria-expanded", "true")
    expect(rejected.locator(".detail")).to_contain_text("Sep 8, 2026")
    suspended = cards.filter(has_text="Paused by the administrator")
    suspended.focus()
    page.keyboard.press("Space")
    expect(rejected).to_have_attribute("aria-expanded", "false")
    expect(suspended).to_have_attribute("aria-expanded", "true")
    expect(suspended.locator(".detail")).to_contain_text("Sep 11, 2026")
    pending = cards.filter(has=page.get_by_role("button", name="Reject", exact=True))
    pending.get_by_role("button", name="Reject", exact=True).click()
    expect(pending).to_have_attribute("aria-expanded", "false")
    pending.get_by_label("Declined by the administrator", exact=True).check()
    expect(pending).to_have_attribute("aria-expanded", "false")
    pending.get_by_role("button", name="Confirm rejection").click()
    expect(page.locator("#admin-result")).to_have_text("Rejected.")
    declined = cards.filter(has_text="Declined by the administrator")
    declined.click()
    expect(declined.locator(".detail")).to_contain_text("Declined by the administrator")
    fixture = json.loads(
        (Path(__file__).parents[2] / "src/sanad/web/static/demo-admin.json").read_text()
    )
    for row in fixture:
        row.pop("decision_reason", None)
    page.route("**/assets/demo-admin.json", partial(fulfill_projection, fixture))
    page.goto(rendered.origin + "/demo/admin")
    page.locator("#admin-applications .summary-strip").wait_for()
    legacy = cards.filter(has_text="Rejected")
    legacy.click()
    expect(legacy.locator(".kv").filter(has_text="Reason").locator("b")).to_have_text(
        "Not recorded"
    )


def test_sample18h3_counts_and_pager(rendered: RenderedApp) -> None:
    page = rendered.page
    page.clock.set_fixed_time(NOW)
    for n, m, headline in [
        (0, 0, "Zero patients, nobody needs you now"),
        (2, 0, "Two patients, nobody needs you now"),
        (2, 1, "Two patients, one needs you now"),
        (21, 1, "21 patients, one needs you now"),
        (51, 1, "51 patients, one needs you now"),
    ]:
        records = [
            record(
                rendered,
                patient_id=f"count-{i:03}",
                reviews=[review("question_answer")] if i < m else [],
            )
            for i in range(n)
        ]
        payloads: list[tuple[str, Any]] = [
            ("/api/patients", [{"patient_id": r["patient_id"]} for r in records])
        ]
        for r in records:
            payloads.extend(
                [
                    (f"/api/patients/{r['patient_id']}", r),
                    (f"/api/patients/{r['patient_id']}/evidence", []),
                ]
            )
        for path, payload in payloads:
            page.unroute(rendered.origin + path)
            page.route(rendered.origin + path, partial(fulfill_projection, payload))
        goto(rendered, "/a")
        expect(page.locator("h1")).to_have_text(headline)
        expect(page.locator(".patient-row")).to_have_count(m)
        page.locator('#filter [data-filter="all"]').click()
        expect(page.locator(".patient-row")).to_have_count(min(n, 20))
        expect(page.locator("#next")).to_have_count(int(n > 20))
        if n > 20:
            page.locator("#next").click()
            expect(page.locator(".patient-row")).to_have_count(min(n - 20, 20))
            assert "page=2" in page.url


def test_sample18h3_reading_provenance(rendered: RenderedApp) -> None:
    data = record(
        rendered,
        orders=[{"status": "active"}, {"status": "superseded"}],
        missions=[
            mission(
                "fulfilled",
                id="monitor-one",
                details={
                    "kind": "MONITOR",
                    "metric": "blood_pressure",
                    "unit": "mmHg",
                    "readings": [
                        {
                            "slot": "slot-a",
                            "observed_at": "2026-09-12T10:00:00Z",
                            "value": "old-slot-value",
                        },
                        {
                            "slot": "slot-a",
                            "observed_at": "2026-09-12T08:00:00Z",
                            "value": "last-slot-value",
                        },
                        {
                            "slot": None,
                            "observed_at": "2026-09-12T09:00:00Z",
                            "value": "unlinked-reading",
                        },
                    ],
                },
            ),
            mission(
                "fulfilled",
                id="monitor-two",
                details={
                    "kind": "MONITOR",
                    "metric": "blood_pressure",
                    "unit": "mmHg",
                    "readings": [
                        {
                            "slot": "slot-b",
                            "observed_at": "2026-09-12T11:00:00Z",
                            "value": "newest-reading",
                        },
                    ],
                },
            ),
        ],
    )
    projected(rendered, data, [{"evidence_id": "a"}, {"evidence_id": "b"}])
    page = goto(rendered, "/a?filter=all")
    page.locator(".patient-row").click()
    panel = page.locator(".patient-row.open + .detail")
    expect(panel.locator(".kv b")).to_have_text(
        ["newest-reading mmHg", "unlinked-reading mmHg", "1", "0", "2"]
    )
    expect(panel.locator(".kv span")).to_have_text(
        [
            "Blood pressure, 1 hour ago",
            "Blood pressure, 3 hours ago",
            "Medicines on the plan",
            "Open requests",
            "Documents sent",
        ]
    )
