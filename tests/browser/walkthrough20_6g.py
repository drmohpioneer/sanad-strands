"""Fixed failure sentences on rendered doctor pages and patient message submissions."""

import json

import pytest
from playwright.sync_api import expect
from store.account_fixtures import PATIENT

from browser.conftest import RenderedApp
from sanad.api.failures import REASONS


@pytest.mark.parametrize("audience", ["doctor", "patient"])
@pytest.mark.parametrize("reason", list(REASONS))
def test_6g_failure_sentences(rendered: RenderedApp, audience: str, reason: str) -> None:
    page = rendered.page
    status = 503 if reason == "configuration" else 500 if reason == "unhandled" else 409
    body = json.dumps({"reason": reason, "detail": "Never display arbitrary backend text"})
    if audience == "doctor":
        page.route(
            "**/api/patients",
            lambda route: route.fulfill(status=status, content_type="application/json", body=body),
        )
        page.goto(rendered.origin + "/a")
        expect(page.locator("#feedback [role=alert]")).to_have_text(REASONS[reason])
    else:
        rendered.login(PATIENT)
        page.goto(rendered.origin + "/pp")
        expect(page.locator("#patient-message")).to_be_visible()
        page.route(
            "**/api/patient/messages",
            lambda route: route.fulfill(status=status, content_type="application/json", body=body),
        )
        page.locator("#patient-message").fill("Synthetic message")
        page.locator("#patient-message-form button").click()
        expect(page.locator("#patient-action-result")).to_have_text(REASONS[reason])
        expect(page.locator("#patient-message")).to_have_value("Synthetic message")
    assert all(f"status of {status}" in error for error in rendered.errors)
    rendered.errors.clear()
