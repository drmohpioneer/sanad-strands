"""Approved legal-copy oracles and the patient's rendered agreement."""

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import available_timezones

import pytest
from browser.conftest import RenderedApp
from browser.conftest import rendered as rendered
from browser.conftest import world as world
from playwright.sync_api import expect
from store.account_fixtures import PATIENT

from deploy import ops
from deploy.common import OperationError
from sanad.presentation import consent_terms
from sanad.store._base import Write
from sanad.store.records import record_item


@pytest.fixture(autouse=True)
def consent_english(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")


def test_approved_text_is_exact() -> None:
    # Golden hashes of the owner's draft sections 1 and 2: remove Markdown heading
    # markers and soft wrapping only; retain paragraphs, bullet and explicit line breaks.
    assert hashlib.sha256(consent_terms.SHORT_TEXT.encode()).hexdigest() == (
        "5282e253b3b4262c41ae33c5d9cac32d12779c161f7da5f14632ea2e876f0988"
    )
    assert hashlib.sha256(consent_terms.FULL_TEXT.encode()).hexdigest() == (
        "4b3ffa2d4b31684f26b235e6e5145ebbe11c047ebef8375e28a4bd660127f6a9"
    )


def test_maximum_escaped_fields_fit_one_plain_message() -> None:
    zone = max(available_timezones(), key=len)
    short, full = consent_terms.render("'" * 160, "23:59", "23:59", zone, "'" * 160)
    assert len(short) <= 4096 and len(full) <= 4096
    assert full.count("&#x27;") == 320 and short.count("&#x27;") == 160
    assert full.endswith("which version of these terms you saw.")
    short, full = consent_terms.render("<doctor>", "22:00", "08:00", "UTC", "<clinic>{x}")
    assert "&lt;doctor&gt;" in short
    assert "&lt;clinic&gt;&#123;x&#125;" in full


def test_clinic_contact_is_explicit_and_never_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    aws, ssm, cfn = Mock(), Mock(), Mock()
    values: dict[str, str] = {}
    aws.client.side_effect = lambda service, **kwargs: {"ssm": ssm, "cloudformation": cfn}[service]
    cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
    ssm.get_parameters.side_effect = lambda **kwargs: {
        "Parameters": [
            {"Name": name, "Value": values[name], "Version": 1}
            for name in kwargs["Names"]
            if name in values
        ]
    }
    ssm.put_parameter.side_effect = lambda **kwargs: values.update(
        {kwargs["Name"]: kwargs["Value"]}
    )
    local = {
        "GEMINI_API_KEY": "synthetic-gemini",
        "TELEGRAM_BOT_TOKEN_SANAD_STRANDS": "4242:synthetic-secret-token",
        "SANAD_ADMIN_TELEGRAM_USER_ID": "10001",
        "SANAD_BOT_USERNAME": "synthetic_bot",
        "SANAD_TELEGRAM_BOT_USERNAME": "synthetic_bot",
        "SANAD_BUDGET_EMAIL": "synthetic@example.test",
    }
    monkeypatch.setattr(ops, "env_values", lambda names: local)
    contact = "Synthetic Demo Clinic front desk, demonstration only"
    ops.secrets_set(aws, "dev", clinic_contact=contact)
    assert values["/sanad/dev/clinic-contact"] == contact
    before = list(ssm.put_parameter.call_args_list)
    ops.secrets_set(aws, "dev")
    assert ssm.put_parameter.call_args_list == before
    assert contact not in capsys.readouterr().out
    for invalid in ("", "x " * 81, "Clinic\ncontact", "token=synthetic-secret-value"):
        with pytest.raises(OperationError):
            ops.secrets_set(aws, "dev", clinic_contact=invalid)
    assert ssm.put_parameter.call_args_list == before


@pytest.mark.parametrize("legacy", [False, True])
def test_patient_sees_only_saved_agreement(
    rendered: RenderedApp, legacy: bool, tmp_path: Path
) -> None:
    app = rendered
    patient = app.world.claims.patient(
        app.world.patient_scope.doctor_id, app.world.patient_scope.patient_id
    )
    assert patient is not None
    row = app.world.store.get(patient.scope, "consent", patient.consent_id or "")
    assert row is not None
    if legacy:
        item = record_item(row)
        body = json.loads(item["body"])
        for field in ("offer_claim_id", "offer_generation", "language"):
            body.pop(field)
        body["policy_text_version"] = "legacy-synthetic"
        item["body"] = json.dumps(body)
        assert app.world.store._atomic([Write(item, row.version)], [])
    app.page.context.clear_cookies()
    app.login(PATIENT)
    app.page.emulate_media(reduced_motion="reduce")
    app.page.goto(app.origin + "/pp")
    expect(app.page.locator('#content[aria-busy="false"]')).to_be_visible()
    agreement = app.page.locator("#patient-agreement")
    expect(agreement).to_be_visible()
    assert app.page.locator("#content > :last-child").get_attribute("id") == "patient-agreement"
    data = app.page.request.get(app.origin + "/api/patient/agreement").json()
    assert agreement.get_attribute("data-version") == data["version"]
    expect(agreement.locator("h1,h2,h3")).to_have_count(0)
    assert agreement.locator(":scope > p").count() == 1
    if legacy:
        expect(agreement.locator("details")).to_have_count(0)
        assert agreement.inner_text().startswith("Agreed on ")
        assert agreement.inner_text().endswith(", version legacy-synthetic")
    else:
        date = agreement.locator("time").inner_text()
        assert agreement.locator(":scope > p").inner_text() == (
            f"You agreed to Sanad's terms on {date}."
        )
        expect(agreement.locator("summary")).to_have_text("Read the terms")
        assert agreement.locator("details").get_attribute("open") is None
        agreement.locator("summary").click()
        assert "\n\n".join(agreement.locator("details > p").all_text_contents()) == data["text"]
    # Desktop absolute columns must leave room for the appended agreement.
    box = agreement.bounding_box()
    assert box is not None
    for section in app.page.locator("#content > section:not(#patient-agreement)").all():
        other = section.bounding_box()
        assert other is not None
        overlaps_x = box["x"] < other["x"] + other["width"] and other["x"] < box["x"] + box["width"]
        if overlaps_x:
            assert box["y"] >= other["y"] + other["height"] - 1
    agreement.scroll_into_view_if_needed()
    agreement.screenshot(path=str(tmp_path / "consent-agreement.png"), animations="disabled")
