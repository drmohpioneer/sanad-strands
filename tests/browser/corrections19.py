"""Run with make test-browser: real rendered assertions and browser-to-store actions."""

from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect
from store import evidence_fixtures as f
from store.account_fixtures import ADMIN, PATIENT
from store.concierge_fixtures import PatientWorld

from browser.aurora18h import test_aurora_keyboard_focus as test_aurora_keyboard_focus
from browser.aurora18h import (
    test_aurora_motion_layout_and_staging as test_aurora_motion_layout_and_staging,
)
from browser.aurora18h import (
    test_aurora_performance as test_aurora_performance,
)
from browser.aurora18h import (
    test_aurora_sample_details as test_aurora_sample_details,
)
from browser.aurora18h import (
    test_aurora_themes as test_aurora_themes,
)
from browser.aurora18h import test_aurora_tokens as test_aurora_tokens
from browser.aurora18h import (
    test_aurora_trends as test_aurora_trends,
)
from browser.conftest import RenderedApp
from browser.demo18f import test_demo_admin_controls as test_demo_admin_controls
from browser.demo18f import (
    test_demo_does_not_unlock_real_apis as test_demo_does_not_unlock_real_apis,
)
from browser.demo18f import test_demo_patient_controls as test_demo_patient_controls
from browser.demo18f import (
    test_demo_routes_ignore_sessions_and_store as test_demo_routes_ignore_sessions_and_store,
)
from browser.depth18e import test_depth_entry_shells as test_depth_entry_shells

# Re-export the new module's tests into the Makefile's explicit browser entrypoint.
from browser.depth18e import (
    test_depth_font_and_theme_layout_stability as test_depth_font_and_theme_layout_stability,
)
from browser.depth18e import (
    test_depth_form_fields_and_pending_state as test_depth_form_fields_and_pending_state,
)
from browser.depth18e import (
    test_depth_identity_all_routes as test_depth_identity_all_routes,
)
from browser.depth18e import (
    test_depth_keyboard_and_motion as test_depth_keyboard_and_motion,
)
from browser.depth18e import (
    test_depth_lightbox_anatomy_and_both_openers as test_depth_lightbox_anatomy_and_both_openers,
)
from browser.depth18e import (
    test_depth_queue_density_and_groups as test_depth_queue_density_and_groups,
)
from browser.depth18e import (
    test_depth_record_loading_and_retry as test_depth_record_loading_and_retry,
)
from browser.depth18e import (
    test_depth_route_geometry_and_empty_anatomy as test_depth_route_geometry_and_empty_anatomy,
)
from browser.depth18e import test_depth_rtl_and_forced_colors as test_depth_rtl_and_forced_colors
from browser.depth18e import (
    test_depth_screenshots_and_computed_surfaces as test_depth_screenshots_and_computed_surfaces,
)
from browser.depth18e import (
    test_depth_summary_filters_chips_and_primary as test_depth_summary_filters_chips_and_primary,
)
from browser.logo18h import test_aurora_entry_logo as test_aurora_entry_logo
from browser.logo18h import test_logo_pages as test_logo_pages
from browser.sample18h3 import (
    test_sample18h3_admin_details as test_sample18h3_admin_details,
)
from browser.sample18h3 import (
    test_sample18h3_chip_sources_and_actions as test_sample18h3_chip_sources_and_actions,
)
from browser.sample18h3 import test_sample18h3_counts_and_pager as test_sample18h3_counts_and_pager
from browser.sample18h3 import (
    test_sample18h3_demo_cards as test_sample18h3_demo_cards,
)
from browser.sample18h3 import (
    test_sample18h3_headline_and_url as test_sample18h3_headline_and_url,
)
from browser.sample18h3 import (
    test_sample18h3_patient_tabs_and_bubbles as test_sample18h3_patient_tabs_and_bubbles,
)
from browser.sample18h3 import (
    test_sample18h3_reading_provenance as test_sample18h3_reading_provenance,
)
from browser.walkthrough20_6e import (
    test_6e_reply_and_immediate_danger as test_6e_reply_and_immediate_danger,
)
from browser.walkthrough20_6e import (
    test_6e_still_working_is_bounded as test_6e_still_working_is_bounded,
)
from browser.walkthrough20_6e import (
    test_6e_two_minutes_polling_and_saves as test_6e_two_minutes_polling_and_saves,
)
from browser.walkthrough20_6f import (
    test_6f_doctor_preferences_and_logout as test_6f_doctor_preferences_and_logout,
)
from browser.walkthrough20_6f import (
    test_6f_parallel_patient as test_6f_parallel_patient,
)
from browser.walkthrough20_6f import (
    test_6f_patient_wording as test_6f_patient_wording,
)
from browser.walkthrough20_6g import test_6g_failure_sentences as test_6g_failure_sentences
from browser.walkthrough20_6h import (
    test_6h_hold_reason_fallback_and_latest_stop as test_6h_hold_reason_fallback_and_latest_stop,
)
from browser.walkthrough20_6h import (
    test_6h_identity_associate_replay as test_6h_identity_associate_replay,
)
from browser.walkthrough20_6h import (
    test_6h_task_actions_and_doctor_refusal as test_6h_task_actions_and_doctor_refusal,
)
from browser.walkthrough20_6i import (
    test_6i_hidden_and_queued_preferences as test_6i_hidden_and_queued_preferences,
)
from browser.walkthrough20_6i import (
    test_6i_passive_poll_preserves_quiet_edit as test_6i_passive_poll_preserves_quiet_edit,
)
from browser.walkthrough20_6i import (
    test_6i_preference_poll_has_time_limit as test_6i_preference_poll_has_time_limit,
)
from browser.words18g import (
    test_18g_admin_counts_reasons_and_results as test_18g_admin_counts_reasons_and_results,
)
from browser.words18g import (
    test_18g_click_anywhere_back_and_counts as test_18g_click_anywhere_back_and_counts,
)
from browser.words18g import (
    test_18g_dates_targets_and_no_repeats as test_18g_dates_targets_and_no_repeats,
)
from browser.words18g import test_18g_demo_catalog as test_18g_demo_catalog
from browser.words18g import (
    test_18g_every_outstanding_sentence as test_18g_every_outstanding_sentence,
)
from browser.words18g import (
    test_18g_history_labels as test_18g_history_labels,
)
from browser.words18g import (
    test_18g_patient_links_words_and_uploads as test_18g_patient_links_words_and_uploads,
)
from browser.words18g import (
    test_18g_terminal_states_history_and_contact as test_18g_terminal_states_history_and_contact,
)
from browser.words18g import test_18g_urgency_uses_source_wait as test_18g_urgency_uses_source_wait
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
        expect(page.locator("#action-toast")).to_contain_text("Change recorded.")
    return dict(payload["action"])


@pytest.mark.parametrize("world", ["monitor"], indirect=True)
def test_corrected_monitor_render_and_doctor_decisions(
    world: PatientWorld, rendered: RenderedApp, tmp_path: Path
) -> None:
    rendered.detail()
    page = rendered.page
    page.get_by_role("tab", name="Requests", exact=True).click()
    table = page.locator(".reading-table")
    expect(table.locator("tbody tr")).to_have_count(1)
    expect(table.get_by_text("130/85", exact=True)).to_have_count(1)
    expect(table).not_to_contain_text("120/80")
    facts = page.locator(".detail-grid section").filter(has=page.locator("[data-correct-fact]"))
    expect(facts).to_contain_text("BP 130/85")
    expect(facts).not_to_contain_text("BP 120/80")
    # Superseded history stays retained; this assertion is about current truth.
    page.screenshot(path=str(tmp_path / "corrected-monitor.png"), full_page=True)
    page.get_by_role("tab", name="Medicines", exact=True).click()
    page.locator("[data-correct-fact]").first.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator('[name="text"]')).to_have_value("BP 130/85")
    dialog.locator('[name="text"]').fill("BP 135/85")
    action = submit(page, "Doctor rechecked the source")
    assert action["type"] == "CorrectRecord" and action["operation"] == "replace"
    page.get_by_role("tab", name="Requests", exact=True).click()
    expect(table.get_by_text("135/85", exact=True)).to_have_count(1)
    expect(table).not_to_contain_text("130/85")
    correction = next(
        r for r in world.rows("correction") if r.body["reason"] == "Doctor rechecked the source"
    )
    page.get_by_role("tab", name="History", exact=True).click()
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
    page.get_by_role("tab", name="Documents", exact=True).click()
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
    page.get_by_role("tab", name="Documents", exact=True).click()
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
    page.get_by_role("tab", name="Requests", exact=True).click()
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
    # The doctor surface always uses the released English state phrases.
    expect(mission.locator(".status").first).to_have_text("Not due yet")
    expect(mission.locator("time").first).to_have_attribute(
        "datetime", due.isoformat().replace("+00:00", "Z")
    )
    page.get_by_role("tab", name="Medicines", exact=True).click()
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
        if path in ("/a/inbox", "/a/history", "/a/preferences"):
            expect(page.locator("#freshness")).not_to_be_empty()
        expect(page.locator("#feedback")).to_be_empty()
    rendered.login(PATIENT)
    response = page.goto(rendered.origin + "/pp")
    assert response and response.status == 200
    page.locator('#content[aria-busy="false"]').wait_for()
    expect(page.locator("#freshness")).not_to_be_empty()
    expect(page.locator("#feedback")).to_be_empty()
    assert rendered.errors == []


IDENTITY_WALK = Path(__file__).with_name("identity_walk.js").read_text()


def assert_identity(page: Page) -> None:
    assert page.evaluate(IDENTITY_WALK) == []
    expect(page.locator(".identity > svg.sanad-lockup")).to_have_count(1)
    expect(page.locator(".identity > svg.sanad-lockup")).to_have_attribute("aria-hidden", "true")
    expect(page.locator(".identity > .visually-hidden")).to_have_text(
        "سند" if page.locator("html").get_attribute("lang") == "ar" else "Sanad"
    )


@pytest.mark.parametrize("world", ["monitor"], indirect=True)
def test_identity_tabs_themes_and_no_horizontal_overflow(rendered: RenderedApp) -> None:
    page = rendered.page
    rendered.detail()
    for name in ("Medicines", "Requests", "Documents", "History"):
        page.get_by_role("tab", name=name, exact=True).click()
        assert_identity(page)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    support = page.locator("details.support")
    expect(support).not_to_have_attribute("open", "")
    # Identity checks include hidden tabs/dialogs, but support remains deliberately excluded.
    support.locator("summary").click()
    expect(support).to_contain_text(rendered.world.patient_scope.patient_id)
    assert_identity(page)
    for path in ("/a", "/a/inbox", "/a/history", "/a/preferences", "/demo"):
        page.goto(rendered.origin + path)
        page.locator('#content[aria-busy="false"]').wait_for()
        assert_identity(page)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    rendered.login(PATIENT)
    page.goto(rendered.origin + "/pp")
    page.locator('#content[aria-busy="false"]').wait_for()
    assert_identity(page)
    expect(page.locator("details.support")).to_have_count(0)


def test_keyboard_record_sort_and_reduced_motion(rendered: RenderedApp) -> None:
    page = rendered.page
    page.goto(rendered.origin + "/demo")
    page.locator('#content[aria-busy="false"]').wait_for()
    rows = page.locator(".patient-row")
    rows.first.focus()
    page.keyboard.press("ArrowDown")
    expect(rows.nth(1)).to_be_focused()
    page.keyboard.press("Enter")
    expect(rows.nth(1)).to_have_attribute("aria-expanded", "true")
    page.locator(".patient-row.open + .detail a.primary").click()
    expect(page.locator("#what-to-do")).to_be_visible()
    expect(page.locator("#patient-drawer")).to_have_count(0)
    assert_identity(page)
    page.locator("#back").click()
    page.locator('#content[aria-busy="false"]').wait_for()
    page.emulate_media(reduced_motion="reduce")
    assert rows.first.evaluate("e => getComputedStyle(e).animationName") == "none"
    assert (
        float(
            page.locator("#refresh")
            .evaluate("e => getComputedStyle(e).transitionDuration")
            .split(",")[0]
            .removesuffix("s")
        )
        <= 0.000001
    )


def test_identity_walk_rejects_accessible_and_hidden_leaks(rendered: RenderedApp) -> None:
    page = rendered.page
    rendered.detail()
    page.evaluate("""() => {
      const leak = document.createElement('span');
      leak.textContent = 'abcdef1234567890abcdef1234567890';
      leak.setAttribute('aria-label', 'patient_report');
      leak.title = 'source_type'; leak.hidden = true;
      document.body.append(leak);
    }""")
    failures = page.evaluate(IDENTITY_WALK)
    assert "abcdef1234567890abcdef1234567890" in failures
    assert "patient_report" in failures
    assert "source_type" in failures


def test_admin_surface_identity_and_theme(rendered: RenderedApp) -> None:
    rendered.login(ADMIN)
    page = rendered.page
    response = page.goto(rendered.origin + "/admin")
    assert response and response.status == 200
    expect(page.get_by_role("heading", name="Administrator", exact=True)).to_be_visible()
    expect(page.locator("#admin-applications section")).not_to_have_count(0)
    assert_identity(page)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for choice in ("dark", "light"):
        page.locator(f'[data-theme-set="{choice}"]').click()
        expect(page.locator("html")).to_have_attribute("data-theme", choice)


@pytest.mark.parametrize("world", ["question"], indirect=True)
def test_question_defer_and_digest_form_use_existing_commands(rendered: RenderedApp) -> None:
    from store.executors_15_fixtures import get

    from sanad.domain import Mission
    from sanad.store.records import from_record

    target = from_record(
        next(r for r in rendered.world.rows("mission") if r.body["kind"] == "QUESTION"), Mission
    )
    page = rendered.page
    page.goto(rendered.origin + "/a/inbox")
    page.locator('#content[aria-busy="false"]').wait_for()
    card = page.locator(".question-card").first
    expect(card).to_be_visible()
    assert_identity(page)
    before = get(rendered.world, target)
    card.get_by_role("button", name="Defer", exact=True).click()
    assert get(rendered.world, target) == before
    with page.expect_response(lambda r: r.url.endswith("/defer")) as response:
        card.get_by_role("button", name="Confirm", exact=True).click()
    assert response.value.status == 200
    assert get(rendered.world, target).due_at == max(
        before.due_at, rendered.world.clock() + timedelta(hours=24)
    )
    expect(page.locator("#action-toast")).to_contain_text("Question deferred.")
    page.goto(rendered.origin + "/a/preferences")
    page.locator('#content[aria-busy="false"]').wait_for()
    page.locator("#digest-time").fill("19:30")
    page.locator("#digest-packing").select_option("each")
    with page.expect_response(
        lambda r: r.url.endswith("/api/preferences") and r.request.method == "POST"
    ) as saved:
        page.get_by_role("button", name="Save digest").click()
    assert saved.value.status == 200
    expect(page.locator("#action-toast")).to_contain_text("Digest preferences saved.")
    expect(page.locator("#digest-time")).to_have_value("19:30")


@pytest.mark.parametrize("world", ["evidence"], indirect=True)
def test_lightbox_image_and_unavailable_state(rendered: RenderedApp) -> None:
    import base64

    rendered.detail()
    page = rendered.page
    page.get_by_role("tab", name="Documents", exact=True).click()
    original = page.locator("[data-media]").first
    expect(original).to_have_count(1)
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7XkAAAAASUVORK5CYII="
    )
    page.route(
        "**/media/*", lambda route: route.fulfill(body=image_bytes, content_type="image/png")
    )
    original.click()
    expect(page.locator(".lightbox img")).to_be_visible()
    assert_identity(page)
    page.keyboard.press("Escape")
    expect(original).to_be_focused()
    page.unroute("**/media/*")
    page.route("**/media/*", lambda route: route.fulfill(status=403, body=""))
    original.click()
    expect(page.locator(".lightbox")).to_contain_text("Original unavailable.")
    # The denied stream is intentional; retain every unrelated console/runtime error.
    rendered.errors[:] = [
        e for e in rendered.errors if "Failed to load resource" not in e or "403" not in e
    ]
    page.keyboard.press("Escape")


def test_patient_upload_rejection_and_theme_choice(rendered: RenderedApp) -> None:
    rendered.login(PATIENT)
    page = rendered.page
    page.goto(rendered.origin + "/pp")
    page.locator('#content[aria-busy="false"]').wait_for()
    page.locator('[data-theme-set="dark"]').click()
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(page.locator('[data-theme-set="dark"]')).to_have_attribute("aria-pressed", "true")
    page.locator("#patient-file").set_input_files(
        {"name": "unsupported.txt", "mimeType": "text/plain", "buffer": b"Synthetic"}
    )
    page.locator('#patient-upload-form button[type="submit"]').click()
    expect(page.locator("#patient-action-result")).not_to_be_empty()
    assert_identity(page)


# The 23:48 action table has sixteen scope-specific rows, plus an overdue duplicate.
INBOX_CASES = [
    (
        "result_review",
        "patient",
        "Read the result and tell the patient what it means.",
        "Open evidence",
        "evidence",
    ),
    (
        "evidence_association",
        "patient",
        "A document arrived. Say which request it belongs to, or that it is not this patient's.",
        "Open evidence",
        "evidence",
    ),
    (
        "question_answer",
        "patient",
        "The patient asked a question. Answer it.",
        "Answer question",
        "questions",
    ),
    (
        "correction_disposition",
        "patient",
        "A correction to the record is waiting for your yes or no.",
        "Open history",
        "history",
    ),
    (
        "incident_response",
        "patient",
        "Respond to the danger report. Nobody has answered it yet.",
        "Open record",
        "plan",
    ),
    (
        "incident_response",
        "intake",
        "Respond to the danger report. Nobody has answered it yet.",
        "Handled from the intake message in Telegram.",
        None,
    ),
    (
        "unmet_objective",
        "patient",
        'The patient missed "the request". Decide: chase again, extend, or close it.',
        "Open requests",
        "requests",
    ),
    (
        "followup_disposition",
        "patient",
        (
            "The follow-up on the new medicine needs your decision (it came back, ran"
            " late, or could not be sent)."
        ),
        "Open requests",
        "requests",
    ),
    (
        "binding_review",
        "patient",
        (
            "Check this patient's link: who joined, or a conflict in what they asked "
            "for (stop, quiet hours)."
        ),
        "Open requests",
        "requests",
    ),
    (
        "media_failure",
        "patient",
        "A photo the patient sent could not be read. Ask them to send it again.",
        "Open requests",
        "requests",
    ),
    (
        "media_failure",
        "intake",
        "A photo the patient sent could not be read. Ask them to send it again.",
        "Handled from the intake message in Telegram.",
        None,
    ),
    (
        "intake_clarification",
        "intake",
        "A new patient's file needs one clarification before it is complete.",
        "Handled from the intake message in Telegram.",
        None,
    ),
    (
        "delivery_failure",
        "patient",
        "A message to this patient did not arrive. Check how to reach them.",
        "Open requests",
        "requests",
    ),
    (
        "delivery_failure",
        "intake",
        "A message about a new patient's file did not arrive.",
        "Handled from the intake message in Telegram.",
        None,
    ),
    (
        "delivery_failure",
        "doctor",
        "A message to you did not arrive. Check your Telegram.",
        "Handled from the review message in Telegram.",
        None,
    ),
    (
        "coverage_review",
        "patient",
        "Check who is covering these patients.",
        "Handled from the review message in Telegram.",
        None,
    ),
    (
        "result_review",
        "patient",
        "Read the result and tell the patient what it means.",
        "Open evidence",
        "evidence",
    ),
]


def test_walkthrough_inbox_action_table(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domain_fixtures import review

    from sanad.domain import ReviewKind
    from sanad.domain.entities import review_source_key

    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    w, page = rendered.world, rendered.page
    page.clock.set_fixed_time(w.clock())
    inbound_reviews: list[dict[str, object]] = []
    for i, (kind, scope, _, _, _) in enumerate(INBOX_CASES):
        source = (
            "intake" if scope == "intake" else "outbound_intent" if scope == "doctor" else "mission"
        )
        if scope == "intake" and kind in {"media_failure", "intake_clarification"}:
            source = "inbound_receipt"
        item = review(
            id=f"rail-{i}",
            owner_doctor_id=w.patient_scope.doctor_id,
            patient_id=w.patient_scope.patient_id if scope == "patient" else None,
            source_mission_id=None,
            source_type=source,
            source_id=f"source-{i}",
            review_kind=kind,
            unique_source_key=review_source_key(
                w.patient_scope.doctor_id, source, f"source-{i}", 2, ReviewKind(kind)
            ),
            review_at=w.clock() + (timedelta(days=-2) if i == 16 else timedelta(days=3)),
        )
        if source == "inbound_receipt":
            # model_scope currently rejects these unassigned receipt reviews.
            # Exercise their rendering at the API boundary without changing storage.
            inbound_reviews.append(item.model_dump(mode="json"))
        else:
            w.seed(item)

    def include_inbound_reviews(route: Route) -> None:
        response = route.fetch()
        assert response.ok
        payload = response.json()
        assert isinstance(payload, list)
        route.fulfill(response=response, json=payload + inbound_reviews)

    page.route("**/api/browser/reviews", include_inbound_reviews)
    page.goto(rendered.origin + "/a/inbox")
    page.locator('#content[aria-busy="false"]').wait_for()
    expect(page.locator(".inbox-item")).to_have_count(17)
    for i, (kind, scope, words, control, tab) in enumerate(INBOX_CASES):
        card = page.locator(f"#inbox-rail-{i}")
        line = card.locator(".review-line")
        expect(line).to_be_visible()
        expect(line).to_contain_text(words)
        expect(card.locator("summary")).to_contain_text(
            "Synthetic Patient"
            if scope == "patient"
            else "Unassigned intake"
            if scope == "intake"
            else "Your account"
        )
        expect(line).not_to_contain_text("days")  # No source-event date exists in these records.
        assert card.evaluate("(el)=>el.open") == (kind == "incident_response" or i == 16)
        if not card.evaluate("(el)=>el.open"):
            card.locator("summary").click()
        if tab:
            action = card.get_by_role("link", name=control, exact=True)
            expect(action).to_be_visible()
            expect(action).to_have_class("button")
            assert action.is_enabled() and action.get_attribute("aria-disabled") != "true"
            href = action.get_attribute("href")
            assert (
                href == "#questions"
                if tab == "questions"
                else href == f"/a/patients/{w.patient_scope.patient_id}#tab=requests"
            )
        else:
            expect(card.locator(".review-action")).to_have_text(control)
            expect(card.locator("a, button")).to_have_count(0)
    overdue = page.locator('[data-summary="overdue"]')
    overdue.click()
    expect(page.locator(".inbox-item")).to_have_count(
        0
    )  # Review deadlines are not patient lateness.
    expect(overdue).to_have_attribute("aria-pressed", "true")
    # No filtered overdue card means no dangling summary link.
    page.locator("#search").fill("Danger")
    expect(page.locator(".summary-strip a")).to_have_count(0)
    for tab, label in [
        ("plan", "Medicines"),
        ("requests", "Requests"),
        ("evidence", "Documents"),
        ("history", "History"),
    ]:
        page.goto(f"{rendered.origin}/a/patients/{w.patient_scope.patient_id}#tab={tab}")
        page.locator('#content[aria-busy="false"]').wait_for()
        expect(page.get_by_role("tab", name=label, exact=True)).to_have_attribute(
            "aria-selected", "true"
        )
        expect(page.get_by_role("tabpanel")).to_have_count(1)


@pytest.mark.parametrize("world", ["empty", "evidence", "monitor", "medication"], indirect=True)
def test_walkthrough_record_four_groups(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    w, page = rendered.world, rendered.page
    page.goto(rendered.origin + "/a?filter=all")
    page.locator('#content[aria-busy="false"]').wait_for()
    with page.expect_response(lambda r: r.url.endswith("/evidence")) as response:
        page.locator(".patient-row").first.click()
        page.locator(".patient-row.open + .detail a.primary").click()
    assert response.value.status == 200
    record_page = page.locator("#content")
    expect(page.locator("#patient-drawer")).to_have_count(0)
    for title in ["Medicines", "Requests", "Documents", "History"]:
        expect(page.get_by_role("tab", name=title, exact=True)).to_be_visible()
    record = page.request.get(f"{rendered.origin}/api/patients/{w.patient_scope.patient_id}").json()
    active_orders = [o for o in record["orders"] if o["status"] == "active"]
    for order in active_orders:
        instruction = order["current_version"]["structured_instruction"]
        for field in ("drug", "dose", "frequency"):
            if instruction.get(field):
                expect(record_page).to_contain_text(str(instruction[field]))
    if not active_orders:
        expect(record_page).to_contain_text("No active medication is recorded.")
    if not record["reviews"] and not record["missions"] and not record["followups"]:
        expect(record_page).to_contain_text(
            "All quiet. Nothing is waiting on you or on the patient."
        )
    for correction in record["corrections"]:
        expect(record_page.locator(f'[data-correction="{correction["id"]}"]')).to_have_count(1)
    evidence = response.value.json()
    if evidence:
        expect(record_page).to_contain_text("Lab result")
        expect(record_page).to_contain_text("On file.")
        assert record_page.locator("time").count() > 0
    else:
        expect(record_page).to_contain_text("No evidence is recorded for this patient.")
    if w.rows("correction"):
        expect(record_page).to_contain_text("120/80")
        expect(record_page).to_contain_text("130/85")
    else:
        expect(record_page).to_contain_text("No corrections are recorded for this patient.")
    expect(page.locator("#feedback")).to_be_empty()


@pytest.mark.parametrize("rendered", [(1440, "light"), (375, "light")], indirect=True)
def test_6d_admin_denials_keep_revocation(rendered: RenderedApp) -> None:
    """F12: reuse the same admin cookie after the first wrong-role request."""
    rendered.login(ADMIN)
    world, page = rendered.world, rendered.page
    cookie = rendered.cookies[ADMIN]["sanad_session"]
    before = world.login.session(cookie)
    assert before and before.revoked_at is None
    for path in (
        "/a",
        "/a/inbox",
        "/api/questions",
        "/api/patients",
        f"/a/patients/{world.patient_scope.patient_id}",
    ):
        response = page.goto(rendered.origin + path)
        assert response and response.status == 403
        expect(page.locator("body")).to_contain_text("Not available from an administrator session.")
        if not path.startswith("/api/"):
            expect(page.locator("body")).to_contain_text(
                "Your session was closed for safety; sign in again from Telegram."
            )
            expect(page.locator("body")).to_have_class("standalone")
            expect(page.get_by_role("link", name="Administrator sign-in")).to_have_attribute(
                "href", "/admin"
            )
        else:
            assert response.json() == {"detail": "Not available from an administrator session."}
        after = world.login.session(cookie)
        assert after and after.revoked_at
        expect(page.locator("body")).not_to_contain_text("Synthetic Patient")
    # The visible recovery link reaches the standalone entry with no clinical access.
    page.get_by_role("link", name="Administrator sign-in").click()
    expect(page.locator("body")).to_have_class("standalone")
    expect(page.locator("body")).to_contain_text("Open Telegram and send /login admin to sign in.")
    rendered.errors[:] = [
        e
        for e in rendered.errors
        if e
        not in {
            "Failed to load resource: the server responded with a status of 403 (Forbidden)",
            "Failed to load resource: the server responded with a status of 401 (Unauthorized)",
        }
    ]


@pytest.mark.parametrize("world", ["medication"], indirect=True)
@pytest.mark.parametrize("rendered", [(1440, "light"), (375, "light")], indirect=True)
def test_6d_last_activity_and_record_amend(
    rendered: RenderedApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    world, page = rendered.world, rendered.page
    page.clock.set_fixed_time(world.clock() + timedelta(hours=2))
    page.goto(rendered.origin + "/a?filter=all")
    page.locator('#content[aria-busy="false"]').wait_for()
    row = page.locator(".patient-row").first
    expect(row).to_contain_text(
        "قبل ساعتين" if page.locator("html").get_attribute("lang") == "ar" else "2 hours ago"
    )
    assert row.locator(".when time").get_attribute("datetime")
    page.screenshot(path=str(tmp_path / "6d-patient-list.png"), full_page=True)
    row.click()
    page.locator(".patient-row.open + .detail a.primary").click()
    page.locator("#what-to-do").wait_for()
    page.get_by_role("button", name="Amend instruction").click()
    dialog = page.locator("#correction-dialog")
    expect(dialog).to_be_visible()
    dialog.locator('[name="dose"]').fill("40 mg")
    dialog.locator('[name="reason"]').fill("Synthetic owner walkthrough correction")
    with page.expect_response(lambda r: r.url.endswith("/corrections")) as response:
        dialog.get_by_role("button", name="Confirm correction").click()
    assert response.value.status == 200
    expect(page.locator("[data-order]")).to_contain_text("40 mg")
    expect(page.locator("#action-toast")).to_contain_text("Change recorded.")
    assert any("40 mg" in str(r.body) for r in world.rows("care_order_version"))


@pytest.mark.parametrize("rendered", [(1440, "light"), (375, "light")], indirect=True)
def test_6d_unknown_and_foreign_patient_show_same_404(rendered: RenderedApp) -> None:
    world, page = rendered.world, rendered.page
    # A real patient in another tenant and a fabricated id are indistinguishable.
    foreign = world.claims.create_stub
    from sanad.auth.commands import CreatePatientStub
    from sanad.domain import TenantScope

    world.approve("999999999999991214")
    actor = world.actor("999999999999991214")
    assert (
        foreign(
            CreatePatientStub(
                command_id="foreign6d", actor=actor, display_name="Private other doctor's patient"
            )
        ).status
        == "accepted"
    )
    assert actor.doctor_id
    foreign_id = world.store.list_patients(TenantScope(doctor_id=actor.doctor_id))[0][0].id
    for patient_id in ("not-a-real-patient-20260912", foreign_id):
        response = page.goto(rendered.origin + "/a/patients/" + patient_id)
        assert response and response.status == 404
        expect(page.locator("body")).to_contain_text("Patient not found.")
        expect(page.locator("body")).not_to_contain_text("Private other doctor's patient")
    rendered.errors[:] = [
        e
        for e in rendered.errors
        if e != "Failed to load resource: the server responded with a status of 404 (Not Found)"
    ]
