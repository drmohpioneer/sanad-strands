"""Run with make test-browser: real rendered assertions and browser-to-store actions."""

from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from store import evidence_fixtures as f
from store.account_fixtures import PATIENT
from store.concierge_fixtures import PatientWorld

from browser.conftest import RenderedApp
from sanad.domain import Mission
from sanad.store.records import from_record


def submit(page: Page, reason: str, label: str = "Confirm correction") -> dict[str, object]:
    dialog = page.get_by_role("dialog")
    if reason:
        dialog.locator('[name="reason"]').fill(reason)
    preview = label == "Preview what will resume"
    with ExitStack() as waits:
        response = waits.enter_context(
            page.expect_response(lambda r: r.url.endswith("/corrections"))
        )
        if not preview:
            record_url = page.url.replace("/a/patients/", "/api/patients/")
            refreshed = waits.enter_context(
                page.expect_response(lambda r: r.url == record_url and r.request.method == "GET")
            )
        dialog.get_by_role("button", name=label, exact=True).click()
    result = response.value
    assert result.status == 200, result.text()
    payload = result.request.post_data_json
    assert isinstance(payload, dict)
    assert result.request.headers["x-csrf-token"]
    assert result.request.headers["origin"] == page.url.split("/a/")[0]
    assert payload["expected_binding_epoch"] >= 1
    assert payload["command_id"]
    outcome = result.json()
    assert outcome["status"] == "accepted"
    if preview:
        assert len(outcome["offers"]) == 1
        assert outcome["offers"][0]["consumed_at"] is None
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("button", name="Confirm reopening", exact=True)).to_be_visible()
    else:
        assert outcome["offers"] == []
        assert refreshed.value.status == 200
        expect(page.get_by_role("dialog")).not_to_be_visible()
        page.locator('#content[aria-busy="false"]').wait_for()
        expect(page.locator("#feedback")).to_be_empty()
    return dict(payload["action"])


@pytest.mark.parametrize("world", ["monitor"], indirect=True)
def test_corrected_monitor_render_and_doctor_decisions(
    world: PatientWorld, rendered: RenderedApp, tmp_path: Path
) -> None:
    rendered.detail()
    page = rendered.page
    table = page.locator(".reading-table")
    expect(table.locator("tbody tr")).to_have_count(1)
    expect(table.get_by_text("130/85", exact=True)).to_have_count(1)
    expect(table).not_to_contain_text("120/80")
    facts = page.locator(".detail-grid section").filter(has=page.locator("[data-correct-fact]"))
    expect(facts).to_contain_text("BP 130/85")
    expect(facts).not_to_contain_text("BP 120/80")
    # Superseded history stays retained; this assertion is about current truth.
    page.screenshot(path=str(tmp_path / "corrected-monitor.png"), full_page=True)
    page.locator("[data-correct-fact]").first.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator('[name="text"]')).to_have_value("BP 130/85")
    dialog.locator('[name="text"]').fill("BP 135/85")
    action = submit(page, "Doctor rechecked the source")
    assert action["type"] == "CorrectRecord" and action["operation"] == "replace"
    expect(table.get_by_text("135/85", exact=True)).to_have_count(1)
    expect(table).not_to_contain_text("130/85")
    correction = next(
        r for r in world.rows("correction") if r.body["reason"] == "Doctor rechecked the source"
    )
    page.locator(f'[data-response="{correction.id}"]').click()
    assert submit(page, "Doctor recorded follow-up")["type"] == "CorrectionResponse"
    page.locator(f'[data-validate="{correction.id}"]').click()
    assert submit(page, "Reviewed current reading")["type"] == "ValidateCorrection"
    mission = world.store.get_mission(world.patient_scope, "monitor")
    assert mission and mission.fulfillment_validity == "valid"
    assert rendered.errors == []


@pytest.mark.parametrize("world", ["evidence"], indirect=True)
def test_reading_selection_updates_inputs_and_detach_submits(
    world: PatientWorld, rendered: RenderedApp
) -> None:
    old = f.current(world)
    rendered.detail()
    page = rendered.page
    page.locator("[data-correct-evidence]").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    dialog.locator('[name="row_index"]').select_option("1")
    expect(dialog.locator('[name="value"]')).to_have_value("1.0")
    expect(dialog.locator('[name="unit"]')).to_have_value("mg/dL")
    dialog.locator('[name="row_index"]').select_option("0")
    expect(dialog.locator('[name="value"]')).to_have_value("5.0")
    expect(dialog.locator('[name="unit"]')).to_have_value("mmol/L")
    dialog.locator('[name="row_index"]').select_option("1")
    dialog.locator('[name="value"]').fill("1.2")
    action = submit(page, "Correct second reading")
    assert action["row_index"] == 1
    changed = f.current(world)
    assert changed.extracted_values[0] == old.extracted_values[0]
    assert changed.extracted_values[1].value == "1.2"
    page.locator("[data-correct-evidence]").click()
    dialog.locator('[name="operation"]').select_option("detach")
    action = submit(page, "Wrong chart")
    assert action["operation"] == "detach" and action["changes"] == {}
    assert "row_index" not in action
    assert f.current(world).association_state == "detached"
    assert rendered.errors == []


@pytest.mark.parametrize("world", ["medication"], indirect=True)
def test_medication_reopen_preview_confirmation_and_amendment(
    world: PatientWorld, rendered: RenderedApp
) -> None:
    before = from_record(world.rows("mission")[0], Mission)
    assert before.state == "fulfilled"
    page = rendered.page
    page.clock.set_fixed_time(world.clock())
    rendered.detail()
    page.locator("[data-reopen]").click()
    dialog = page.get_by_role("dialog")
    due = world.clock() + timedelta(days=4)
    dialog.locator('[name="due"]').fill(due.strftime("%Y-%m-%dT%H:%M"))
    assert (
        submit(page, "Request another report", "Preview what will resume")["type"]
        == "PreviewReopen"
    )
    expect(dialog).to_contain_text("Atorvastatin")
    expect(dialog.locator('[name="reason"]')).not_to_be_visible()
    expect(dialog.locator('[name="reason"]')).to_be_disabled()
    assert world.store.get_mission(world.patient_scope, before.id) == before
    assert submit(page, "", "Confirm reopening")["type"] == "ConfirmReopen"
    reopened = world.store.get_mission(world.patient_scope, before.id)
    assert reopened and reopened.state == "open" and reopened.due_at == due
    expect(page.locator("[data-reopen]")).to_have_count(0)
    mission = page.locator(".detail-grid .record-item").filter(
        has=page.get_by_role("heading", name=before.title, exact=True)
    )
    # The fixture doctor is Arabic and no contest-English override is set here,
    # so the badge renders in Arabic. Asserting English asserted the wrong world.
    expect(mission.locator(".status").first).to_have_text("لم يحن الموعد")
    expect(mission.locator("time").first).to_have_attribute(
        "datetime", due.isoformat().replace("+00:00", "Z")
    )
    page.locator("[data-amend]").click()
    expect(dialog).to_contain_text("cannot be unsent")
    expect(dialog.locator('[name="reason"]')).to_be_visible()
    expect(dialog.locator('[name="reason"]')).to_be_enabled()
    dialog.locator('[name="dose"]').fill("40 mg")
    action = submit(page, "Doctor amended dose")
    assert action["type"] == "AmendOrder" and action["changes"] == {"dose": "40 mg"}
    assert len(world.rows("care_order_version")) == 2
    assert rendered.errors == []


def test_dashboard_and_patient_scripts_render(rendered: RenderedApp) -> None:
    page = rendered.page
    for path in ("/a", "/a/inbox", "/a/history", "/a/preferences", "/demo"):
        response = page.goto(rendered.origin + path)
        assert response and response.status == 200
        page.locator('#content[aria-busy="false"]').wait_for()
        expect(page.locator("#freshness")).not_to_be_empty()
        expect(page.locator("#feedback")).to_be_empty()
    rendered.login(PATIENT)
    response = page.goto(rendered.origin + "/pp")
    assert response and response.status == 200
    page.locator('#content[aria-busy="false"]').wait_for()
    expect(page.locator("#freshness")).not_to_be_empty()
    expect(page.locator("#feedback")).to_be_empty()
    assert rendered.errors == []
