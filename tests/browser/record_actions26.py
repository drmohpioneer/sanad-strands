"""Doctor record interactions in real desktop and narrow browsers."""

from datetime import datetime, timedelta

import pytest
from domain_fixtures import review
from playwright.sync_api import expect
from store.executors_15_fixtures import add, get

from browser.conftest import RenderedApp
from browser.words18g import old_word_walk
from sanad.domain import Mission, ReviewKind
from sanad.domain.entities import review_source_key
from sanad.store.records import from_record


@pytest.mark.parametrize("rendered", [(1440, "dark"), (390, "dark")], indirect=True)
@pytest.mark.parametrize("world", ["question"], indirect=True)
def test_record_answer_extend_reviews(rendered: RenderedApp) -> None:
    w, page = rendered.world, rendered.page
    q = from_record(next(r for r in w.rows("mission") if r.body["kind"] == "QUESTION"), Mission)
    m = add(
        w,
        id="record-request",
        title="Bring the diary <img src=x>",
        due_at=w.clock() - timedelta(days=1),
        state="overdue",
    )
    r = review(
        id="record-review",
        owner_doctor_id=w.doctor.id,
        patient_id=w.patient_scope.patient_id,
        source_type="mission",
        source_id=m.id,
        source_mission_id=m.id,
        source_version=m.version,
        last_material_change_version=m.version,
        review_kind="result_review",
        unique_source_key=review_source_key(
            w.doctor.id, "mission", m.id, m.version, ReviewKind.result_review
        ),
        created_at=w.clock(),
        updated_at=w.clock(),
    )
    w.seed(r)
    rendered.detail()
    assert page.viewport_size is not None
    root = page.locator("#what-to-do")
    expect(
        root.locator(".item-actions a,.item-actions.primary,.item-actions [data-obligation]")
    ).to_have_count(0)
    assert root.locator(".item-actions").count() >= 3
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    old_word_walk(page)
    page.screenshot(
        path=f"lane/runs/26-record-{page.viewport_size['width']}.png",
        full_page=True,
        animations="disabled",
    )

    qactions = root.locator(f'[data-item-id="{q.id}"]')
    qactions.get_by_role("button", name="Answer", exact=True).click()
    answer = "Please bring your diary to the clinic. <b>Thank you</b>"
    qactions.get_by_label("Your answer").fill(answer)
    qactions.get_by_role("button", name="Preview answer", exact=True).click()
    expect(qactions.locator(".record-action-preview")).to_have_text(answer)
    expect(qactions.locator(".record-action-preview b")).to_have_count(0)
    page.screenshot(
        path=f"lane/runs/26-answer-preview-{page.viewport_size['width']}.png", full_page=True
    )
    assert get(w, q).version == q.version
    # Editing a preview requires a fresh preview before Send answer returns.
    qactions.get_by_label("Your answer").fill("Please bring your diary to the clinic.")
    expect(qactions.get_by_role("button", name="Send answer", exact=True)).to_have_count(0)
    qactions.get_by_role("button", name="Preview answer", exact=True).click()
    with page.expect_response(
        lambda r: r.url.endswith("/actions") and r.request.method == "POST"
    ) as response:
        qactions.get_by_role("button", name="Send answer", exact=True).click()
    assert response.value.status == 200, response.value.text()
    assert response.value.request.headers["x-csrf-token"]
    expect(page.locator("#action-toast")).to_have_text("Queued for the patient. ×")
    assert get(w, q).state == "fulfilled"

    mactions = root.locator(f'[data-item-id="{m.id}"]')
    mactions.get_by_role("button", name="Give more time", exact=True).click()
    mactions.get_by_label("New deadline (your browser timezone)", exact=True).fill(
        "2026-09-10T14:30"
    )
    mactions.get_by_label("Reason", exact=True).fill("Agreed with the patient")
    mactions.get_by_role("button", name="Review new deadline", exact=True).click()
    preview = mactions.locator(".record-action-preview").inner_text()
    assert "New deadline:" in preview and "Escalation time:" in preview
    assert get(w, m).version == m.version
    with page.expect_response(
        lambda r: r.url.endswith("/actions") and r.request.method == "POST"
    ) as response:
        mactions.get_by_role("button", name="Confirm", exact=True).click()
    assert response.value.status == 200, response.value.text()
    expect(page.locator("#action-toast")).to_have_text("Saved. ×")
    assert get(w, m).state == "open"
    assert response.value.request.post_data_json is not None
    assert get(w, m).due_at == datetime.fromisoformat(
        response.value.request.post_data_json["due_at"]
    )

    ractions = root.locator(f'[data-item-id="{r.id}"]')
    ractions.get_by_role("button", name="Mark as seen", exact=True).click()
    with page.expect_response(
        lambda r: r.url.endswith("/actions") and r.request.method == "POST"
    ) as response:
        ractions.get_by_role("button", name="Confirm", exact=True).click()
    assert response.value.status == 200, response.value.text()
    expect(root.locator(f'p[data-obligation="{r.id}"]')).to_have_text(
        "You have seen this. It stays open until you reply."
    )
    ractions.get_by_role("button", name="Record review decision", exact=True).click()
    expect(ractions.locator("form")).to_contain_text("This records your decision.")
    ractions.get_by_label("Reason", exact=True).fill("Reviewed together")
    with page.expect_response(
        lambda r: r.url.endswith("/actions") and r.request.method == "POST"
    ) as response:
        ractions.get_by_role("button", name="Confirm", exact=True).click()
    assert response.value.status == 200, response.value.text()
    expect(root.locator(f'p[data-obligation="{r.id}"]')).to_have_count(0)
    resolved = w.store.get_review(w.patient_scope, r.id)
    assert resolved and resolved.state == "resolved"
    assert get(w, m).state == "open"
    old_word_walk(page)
    assert not rendered.errors


@pytest.mark.parametrize("rendered", [(1440, "dark"), (390, "dark")], indirect=True)
def test_record_page_pagination(rendered: RenderedApp) -> None:
    w, page = rendered.world, rendered.page
    page.clock.set_fixed_time(w.clock())
    for i in range(230):
        add(w, id=f"record-{i:03}", title=f"Bring diary {i}")
    rendered.detail()
    assert page.viewport_size is not None
    root = page.locator("#what-to-do")
    initial = root.locator("p[data-obligation]").count()
    assert initial == 200
    assert root.locator(".item-actions").count() == 25
    for n in range(9):
        with page.expect_response(lambda r: "/actions?cursor=" in r.url):
            root.get_by_role("button", name="Show more", exact=True).click()
        expect(root.locator(".item-actions")).to_have_count(min(230, (n + 2) * 25))
    expect(root.get_by_role("button", name="Show more", exact=True)).to_have_count(0)
    expect(root.locator("p[data-obligation]")).to_have_count(230)
    ids = root.locator("p[data-obligation]").evaluate_all("ps=>ps.map(p=>p.dataset.obligation)")
    assert len(set(ids)) == 230
    assert (
        root.locator('p[data-obligation="record-229"]')
        .inner_text()
        .startswith('Nothing to do yet: "Bring diary 229"')
    )
    assert root.locator(".item-actions").evaluate_all(
        "xs=>xs.every(x=>x.previousElementSibling.matches('p[data-obligation]'))"
    )
    old_word_walk(page)
    assert not rendered.errors


@pytest.mark.parametrize("rendered", [(390, "dark")], indirect=True)
def test_record_empty_listing(rendered: RenderedApp) -> None:
    w, page = rendered.world, rendered.page
    add(w)
    page.route(
        "**/api/patients/*/actions", lambda route: route.fulfill(json={"items": [], "cursor": None})
    )
    rendered.detail()
    expect(page.locator("#what-to-do p[data-obligation]")).to_have_count(1)
    expect(page.locator("#what-to-do .item-actions")).to_have_count(0)
    assert not rendered.errors


@pytest.mark.parametrize("rendered", [(1440, "dark"), (390, "dark")], indirect=True)
def test_record_removed_never_linked_actions(rendered: RenderedApp) -> None:
    from store.test_record_actions import seed_removal_question

    w, page = rendered.world, rendered.page
    # A real stub has never had a patient binding or recipient.
    patient = w.stub()
    w.patient_scope = patient.scope
    assert patient.active_binding_id is None and w.profile.recipient_ref is None
    q = seed_removal_question(w)
    mission = add(w, id="removed-request", state="overdue", due_at=w.clock() - timedelta(days=1))
    rendered.detail()
    page.get_by_role("button", name="Remove patient", exact=True).click()
    page.get_by_label("Patient's name", exact=True).fill(patient.display_name)
    page.get_by_role("button", name="Remove", exact=True).click()
    expect(page.locator(".removed-banner")).to_be_visible()
    root = page.locator("#what-to-do")
    expect(root.locator(f'p[data-obligation="{mission.id}"]')).to_have_count(1)
    expect(root.locator(f'[data-item-id="{mission.id}"]')).to_have_count(0)
    expect(root.get_by_role("button", name="Give more time", exact=True)).to_have_count(0)
    expect(root.get_by_role("button", name="Close as not done", exact=True)).to_have_count(0)
    expect(root.get_by_role("button", name="Cancel request", exact=True)).to_have_count(0)
    expect(root.locator('[data-item-id="removal-question-review"]')).to_be_visible()
    actions = root.locator(f'[data-item-id="{q.id}"]')
    expect(actions.get_by_role("button", name="Later", exact=True)).to_be_visible()
    expect(actions.get_by_role("button", name="Close without answer", exact=True)).to_be_visible()
    actions.get_by_role("button", name="Answer", exact=True).click()
    actions.get_by_label("Your answer", exact=True).fill("Bring your diary.")
    actions.get_by_role("button", name="Preview answer", exact=True).click()
    with page.expect_response(
        lambda r: r.url.endswith("/actions") and r.request.method == "POST"
    ) as response:
        actions.get_by_role("button", name="Send answer", exact=True).click()
    assert response.value.status == 200, response.value.text()
    assert response.value.json()["outcome_reason"] == "patient_removed"
    expect(page.locator("#action-toast")).to_have_text("Not sent: this patient was removed. ×")
    assert not w.patient_intents()
    assert get(w, q).state == "fulfilled"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not rendered.errors
