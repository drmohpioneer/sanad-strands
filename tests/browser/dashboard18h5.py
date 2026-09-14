"""Stable dashboard controls, patient counts and twenty-patient pages."""

from functools import partial
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from browser.conftest import RenderedApp
from browser.words18g import NOW, TODAY, fulfill_projection, goto, mission, record, review

GROUPS = ("danger", "pending_review", "overdue", "due_today")


def roster(app: RenderedApp, records: list[dict[str, Any]], path: str) -> None:
    payloads: list[tuple[str, Any]] = (
        [("/assets/demo.json", records)]
        if path == "/demo"
        else [("/api/patients", [{"patient_id": r["patient_id"]} for r in records])]
    )
    if path == "/a":
        for r in records:
            payloads.extend(
                [
                    (f"/api/patients/{r['patient_id']}", r),
                    (f"/api/patients/{r['patient_id']}/evidence", []),
                ]
            )
    for url, payload in payloads:
        app.page.unroute(app.origin + url)
        app.page.route(app.origin + url, partial(fulfill_projection, payload))


def busy_record(app: RenderedApp, index: int) -> dict[str, Any]:
    return record(
        app,
        patient_id=f"patient-{index:03}",
        display_name=f"Patient {index:03}",
        reviews=[
            review(kind, id=f"{kind}-{i}")
            for kind in ("incident_response", "result_review")
            for i in range(2)
        ],
        missions=[mission("open", id=f"late-{i}") for i in range(2)]
        + [mission("open", id=f"today-{i}", due_at=TODAY) for i in range(2)],
    )


def watch_counts(page: Page) -> None:
    page.evaluate("""() => {
      window.countNodes=[...document.querySelectorAll('#content [data-count]')];
      window.countChanges=[];
      window.countWatch?.disconnect();
      window.countWatch=new MutationObserver(records=>{
        for(const r of records)if(r.target.closest?.('[data-count]'))
          window.countChanges.push(r.target.textContent);
      });
      window.countWatch.observe(document.getElementById('content'),
        {subtree:true,childList:true,characterData:true});
    }""")


def assert_still(page: Page) -> None:
    assert page.evaluate("""() => window.countNodes.every((node,i)=>
      node===document.querySelectorAll('#content [data-count]')[i])""")
    assert page.evaluate("window.countChanges") == []


@pytest.mark.parametrize("path", ["/a", "/demo"])
def test_dashboard18h5_counts_and_search(rendered: RenderedApp, path: str) -> None:
    page = rendered.page
    page.clock.set_fixed_time(NOW)
    records = [busy_record(rendered, i) for i in range(3)]
    records.append(record(rendered, patient_id="quiet", display_name="Quiet Patient"))
    records.append({**busy_record(rendered, 4), "removed_at": NOW.isoformat()})
    roster(rendered, records, path)
    goto(rendered, path)
    expect(page.locator(".summary-tile strong")).to_have_text(["3"] * 4)
    expect(page.locator('#filter [data-filter="all"]')).to_have_text("All 4")
    page.wait_for_timeout(1200)
    watch_counts(page)
    for group in GROUPS:
        tile = page.locator(f'[data-summary="{group}"]')
        tile.click()
        expect(page.locator(".patient-row")).to_have_count(3)
        expect(tile).to_have_attribute("aria-pressed", "true")
        expect(page.locator('#filter [aria-pressed="true"]')).to_have_count(0)
        tile.click()
        expect(tile).to_have_attribute("aria-pressed", "true")
        assert_still(page)
    for group, count in (("settled", 1), ("all", 4), ("needs", 3)):
        page.locator(f'#filter [data-filter="{group}"]').click()
        expect(page.locator(".patient-row")).to_have_count(count)
        assert_still(page)
    page.locator('[data-summary="overdue"]').click()
    page.evaluate(
        "window.searchNode=document.getElementById('search');window.headingText=document.querySelector('h1').firstChild"
    )
    search = page.locator("#search")
    search.focus()
    for letter in "Patient 002":
        page.keyboard.insert_text(letter)
        expect(search).to_be_focused()
        assert page.evaluate("window.searchNode===document.getElementById('search')")
        assert search.evaluate("e=>e.selectionStart===e.value.length")
        assert page.evaluate("window.headingText===document.querySelector('h1').firstChild")
        assert_still(page)
    expect(page.locator(".patient-row")).to_have_count(1)
    search.evaluate("e=>e.setSelectionRange(8,11)")
    page.keyboard.insert_text("001")
    expect(search).to_have_value("Patient 001")
    expect(page.locator(".patient-row .who2 b")).to_have_text("Patient 001")
    assert search.evaluate("e=>e.selectionStart===11 && e.selectionEnd===11")
    search.fill("No match")
    expect(page.locator(".patient-row")).to_have_count(0)
    assert_still(page)
    expect(page.locator('[data-summary="overdue"]')).to_have_attribute("aria-pressed", "true")
    page.locator("[data-clear]").click()
    expect(search).to_have_value("")
    expect(search).to_be_focused()
    assert page.evaluate("window.searchNode===document.getElementById('search')")
    assert_still(page)


@pytest.mark.parametrize("path", ["/a", "/demo"])
def test_dashboard18h5_pages(rendered: RenderedApp, path: str) -> None:
    page = rendered.page
    page.clock.set_fixed_time(NOW)
    records = [busy_record(rendered, i) for i in range(45)]
    roster(rendered, records, path)
    for group in ("needs", "all", *GROUPS, "settled"):
        if group == "settled":
            roster(rendered, [{**r, "reviews": [], "missions": []} for r in records], path)
        goto(rendered, path + "?filter=" + group)
        expect(page.locator(".patient-row")).to_have_count(20)
        expect(page.locator(".pager [data-page]")).to_have_text(["1", "2", "3"])
        expect(page.locator("#previous")).to_be_disabled()
        page.wait_for_timeout(1200)
        watch_counts(page)
        seen = page.locator(".patient-row .who2 b").all_text_contents()
        page.locator("#next").click()
        expect(page.locator(".patient-row")).to_have_count(20)
        expect(page.locator('.pager [aria-current="page"]')).to_have_text("2")
        assert "page=2" in page.url
        seen += page.locator(".patient-row .who2 b").all_text_contents()
        page.locator('[data-page="3"]').click()
        expect(page.locator(".patient-row")).to_have_count(5)
        expect(page.locator("#next")).to_be_disabled()
        seen += page.locator(".patient-row .who2 b").all_text_contents()
        assert len(seen) == len(set(seen)) == 45
        assert_still(page)
        page.locator("#previous").click()
        expect(page.locator('.pager [aria-current="page"]')).to_have_text("2")
        page.locator("#search").fill("Patient 00")
        expect(page.locator(".patient-row")).to_have_count(10)
        expect(page.locator(".pager")).to_have_count(0)
        assert "page=1" in page.url
        assert_still(page)
    roster(rendered, [busy_record(rendered, i) for i in range(201)], path)
    goto(rendered, path + "?filter=all&page=6")
    expect(page.locator(".pager button")).to_have_text(
        ["Previous", "1", "5", "6", "7", "11", "Next"]
    )
    expect(page.locator(".pager > span")).to_have_text(["…", "…"])
    page.locator('[data-summary="overdue"]').click()
    expect(page.locator('.pager [aria-current="page"]')).to_have_text("1")
    page.locator('[data-page="11"]').click()
    page.locator('#filter [data-filter="all"]').click()
    expect(page.locator('.pager [aria-current="page"]')).to_have_text("1")
    goto(rendered, path + "?filter=all&page=11")
    page.reload()
    expect(page.locator('.pager [aria-current="page"]')).to_have_text("11")
    expect(page.locator(".patient-row")).to_have_count(1)


def test_dashboard18h5_demo_and_motion(rendered: RenderedApp) -> None:
    page = rendered.page
    page.clock.install(time=NOW)
    goto(rendered, "/demo?filter=all")
    expect(page.locator(".patient-row")).to_have_count(18)
    expect(page.locator(".pager")).to_have_count(0)
    page.clock.run_for(2000)
    watch_counts(page)
    page.locator("#refresh").click()
    page.locator('#content[aria-busy="false"]').wait_for()
    page.clock.run_for(100)
    count = page.locator("#filter [data-count]")
    assert 0 < int(count.inner_text()) < 18, page.evaluate("""() => ({
      count:document.querySelector('#filter [data-count]').textContent,
      time:performance.now(),bar:document.querySelector('.bar').className,
      top:document.querySelector('.bar').getBoundingClientRect().top,
      height:innerHeight,scroll:scrollY
    })""")
    page.clock.run_for(1200)
    expect(count).to_have_text("18")
    assert len(page.evaluate("window.countChanges")) > 1
    page.reload()
    page.locator('#content[aria-busy="false"]').wait_for()
    page.clock.run_for(100)
    assert 0 < int(count.inner_text()) < 18, page.evaluate("""() => ({
      count:document.querySelector('#filter [data-count]').textContent,
      time:performance.now(),bar:document.querySelector('.bar').className,
      top:document.querySelector('.bar').getBoundingClientRect().top,
      height:innerHeight,scroll:scrollY
    })""")
    page.clock.run_for(1200)
    expect(count).to_have_text("18")
    page.emulate_media(reduced_motion="reduce")
    page.locator("#refresh").click()
    page.locator('#content[aria-busy="false"]').wait_for()
    expect(count).to_have_text("18")
    for group in GROUPS:
        tile = page.locator(f'[data-summary="{group}"]')
        tile.click()
        assert int(tile.locator("strong").inner_text()) == page.locator(".patient-row").count()
        expect(page.locator(".pager")).to_have_count(0)
