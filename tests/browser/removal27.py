"""Authenticated removal at desktop and mobile widths; record/inbox stay readable."""

import pytest
from playwright.sync_api import expect

from browser.conftest import RenderedApp


@pytest.mark.parametrize("rendered", [(1440, "dark"), (390, "light")], indirect=True)
@pytest.mark.parametrize("world", ["question"], indirect=True)
def test_removal_browser_flow(rendered: RenderedApp) -> None:
    app = rendered
    app.login(app.world.owner.subject)
    app.detail()
    page = app.page
    opener = page.get_by_role("button", name="Remove patient", exact=True)
    expect(opener).to_be_visible()
    assert "primary" not in (opener.get_attribute("class") or "")
    assert page.locator(".detail-grid .removal").count() == 0
    opener.click()
    expect(
        page.locator(".removal p").filter(has_text="Type the patient's name to confirm.")
    ).to_be_visible()
    page.get_by_role("button", name="Keep", exact=True).click()
    expect(opener).to_be_focused()
    opener.click()
    name = page.locator("#title").inner_text()
    page.get_by_label("Patient's name", exact=True).fill(name)
    page.get_by_role("button", name="Remove", exact=True).click()
    expect(page.locator(".removed-banner")).to_contain_text("Removed on ")
    expect(page.get_by_text(name + " was removed.", exact=True)).to_be_visible()
    assert page.locator("[data-amend],[data-reopen],.removal").count() == 0
    assert page.locator(".removed-banner").evaluate(
        '(el)=>el.nextElementSibling.classList.contains("detail-grid")'
    )
    assert page.locator("body").evaluate("(el)=>el.scrollWidth <= window.innerWidth")
    patient_id = app.world.patient_scope.patient_id
    page.goto(app.origin + "/a")
    page.locator('#content[aria-busy="false"]').wait_for()
    assert page.locator(f'[data-record][href="/a/patients/{patient_id}"]').count() == 0
    page.goto(app.origin + "/a/inbox")
    page.locator('#content[aria-busy="false"]').wait_for()
    assert page.get_by_text(name, exact=False).count() > 0
    page.locator('.question-card [data-action="answer"]').click()
    page.get_by_label("Your answer", exact=True).fill("Bring your diary.")
    page.locator(".question-confirm").get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#action-toast")).to_contain_text("Not sent: this patient was removed.")
    app.detail()
    expect(page.locator(".removed-banner")).to_be_visible()
    assert not app.errors
