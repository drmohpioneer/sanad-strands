"""Real evidence decisions in both web surfaces, including lost-response replay."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from playwright.sync_api import Locator, Route, expect
from providers.fixtures import document
from store import evidence_fixtures as f
from store.concierge_fixtures import PatientWorld

from browser.conftest import RenderedApp
from sanad.auth.service import revise
from sanad.domain import EvidencePredicate, TaskDetails


def controls(rendered: RenderedApp, id: str) -> Locator:
    return rendered.page.locator(f'[data-evidence-actions="{id}"]').first


def choose(rendered: RenderedApp, id: str, label: str) -> Locator:
    box = controls(rendered, id)
    box.get_by_role("button", name=label, exact=True).click()
    return box.locator("form")


def test_6h_identity_associate_replay(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    w, page = rendered.world, rendered.page
    lab = f.lab(printed_name="Foreign Person")
    f.providers(w, lab, lab)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(f.upload, w).result() == "accepted"
    e = f.current(w)
    assert e.mission_id is None
    # Open the mission only after intake: confirm-identity has no mission prerequisite.
    mission = f.mission(w, title="Potassium test")
    rendered.detail()
    page.get_by_role("tab", name="Documents", exact=True).click()
    box = controls(rendered, e.evidence_id)
    expect(box.get_by_role("button", name="Confirm it is this patient", exact=True)).to_be_visible()
    expect(box.get_by_role("button", name="Associate: Potassium test", exact=True)).to_have_count(0)
    choose(rendered, e.evidence_id, "Confirm it is this patient").get_by_role(
        "button", name="Confirm", exact=True
    ).click()
    expect(page.locator("#action-toast")).to_contain_text("Your evidence decision was recorded.")
    confirmed = f.current(w)
    assert confirmed.mission_id is None and "identity_confirmed" in confirmed.flags
    association = next(
        r for r in w.rows("review") if r.body["review_kind"] == "evidence_association"
    )
    assert association.body["state"] != "resolved"
    page.goto(rendered.origin + "/a/inbox")
    page.locator('#content[aria-busy="false"]').wait_for()
    item = page.locator(f'[id="inbox-{association.id}"]')
    if item.get_attribute("open") is None:
        item.locator("summary").click()
    form = choose(rendered, e.evidence_id, "Associate: Potassium test")
    saved: list[dict[str, Any]] = []

    def lose_first_response(route: Route) -> None:
        if not saved:
            response = route.fetch()
            assert response.status == 200
            saved.append(response.json())
            route.fulfill(
                status=500, content_type="application/json", body='{"reason":"unhandled"}'
            )
        else:
            route.continue_()

    page.route("**/api/evidence/*/associate", lose_first_response)
    form.get_by_role("button", name="Confirm", exact=True).click()
    expect(form.locator("[role=alert]")).to_contain_text("The request could not be completed")
    version = f.current(w).version
    form.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#action-toast")).to_contain_text("already handled")
    expect(page.locator(f'[id="inbox-{association.id}"]')).to_have_count(0)
    assert f.current(w).version == version
    stored = w.store.get(w.patient_scope, "mission", mission.id)
    assert stored and stored.body["state"] == "fulfilled"
    assert any(
        r.body["review_kind"] == "result_review" and r.body["state"] != "resolved"
        for r in w.rows("review")
    )
    assert all("status of 500" in error for error in rendered.errors)
    rendered.errors.clear()


def task(w: PatientWorld) -> str:
    f.mission(
        w,
        kind="TASK",
        title="Requested proof",
        details=TaskDetails(
            category="proof", instruction="Send a document", completion_rule="doctor_acceptance"
        ),
        objective_predicate=EvidencePredicate(evaluator="task_evidence"),
    )
    reply = document(
        document_type="other", printed_name="Synthetic Patient", items=[{"name": "Requested proof"}]
    )
    f.providers(w, reply, reply)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(f.upload, w).result() == "accepted"
    return f.current(w).evidence_id


@pytest.mark.parametrize("action", ["accept", "reject", "refusal"])
def test_6h_task_actions_and_doctor_refusal(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    w, page = rendered.world, rendered.page
    id = task(w)
    rendered.detail()
    page.get_by_role("tab", name="Documents", exact=True).click()
    form = choose(rendered, id, "Reject" if action == "reject" else "Accept")
    if action == "reject":
        form.locator("textarea").fill("Not the requested proof")
    if action == "refusal":
        from sanad.domain import Mission
        from sanad.store.records import from_record

        mission = from_record(w.rows("mission")[0], Mission)
        w.seed(revise(mission, w.clock(), state="cancelled", work_clock=None))
    form.get_by_role("button", name="Confirm", exact=True).click()
    if action == "refusal":
        expect(form.locator("[role=alert]")).to_have_text(
            "This request is closed. Open /evidence again."
        )
        assert f.current(w).association_state == "candidate"
        assert all("status of 409" in error for error in rendered.errors)
        rendered.errors.clear()
    else:
        expect(page.locator("#action-toast")).to_contain_text(
            "Your evidence decision was recorded."
        )
        assert f.current(w).association_state == ("accepted" if action == "accept" else "rejected")
        if action == "reject":
            assert f.current(w).rejection_reason == "Not the requested proof"
        assert all(
            r.body["state"] == "resolved"
            for r in w.rows("review")
            if r.body["review_kind"] == "evidence_association"
        )


def test_6h_hold_reason_fallback_and_latest_stop(
    rendered: RenderedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    w, page = rendered.world, rendered.page

    def instruction(verb: str, id: int) -> None:
        p = w.dictate(
            "Synthetic Patient. " + verb + " Aspirin.",
            {
                "patient": {"name_as_spoken": "Synthetic Patient"},
                "orders": [{"action": "stop", "drug": "Aspirin", "action_quote": verb}],
            },
            id=id,
        )
        assert not p.issues
        w.tap("✅ Confirm", id=id + 1)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(instruction, "Hold", 8400).result()
    rendered.detail()
    expect(page.locator("[data-held-order]")).to_contain_text("Aspirin is on hold since")
    expect(page.locator("[data-held-order]")).to_contain_text(": no reason given.")
    assert "Aspirin: doctor instructed hold" in page.get_by_role("tabpanel").inner_text()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(instruction, "Stop", 8402).result()
    rendered.detail()
    expect(page.locator("[data-held-order]")).to_have_count(0)
    expect(page.locator("[data-order]")).to_have_count(0)
    page.get_by_role("tab", name="History", exact=True).click()
    expect(page.locator("[data-stopped-order]")).to_contain_text("Aspirin")
