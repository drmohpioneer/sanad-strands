"""Offline presentation oracles; the complete rendered DOM walk lives in tests/browser."""

import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from sanad.web.browser import surface
from sanad.web.pages import admin_home, doctor_home, patient_home

SCRIPT = Path(__file__).parents[1] / "src/sanad/web/static/browser.js"


def formatter(correction: dict[str, object]) -> str:
    source = SCRIPT.read_text()
    formatter_source = source.split("  function correctionProse(c){", 1)[1].split(
        "  function summaryStrip(", 1
    )[0]
    script = (
        """
const lang='en',state={zone:'UTC'};
const t=k=>({not_recorded:'Not recorded',accepted:'Accepted document',detached:'Not used'}[k]);
const esc=s=>String(s).replaceAll('<','&lt;').replaceAll('>','&gt;');
function correctionProse(c){
"""
        + formatter_source
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            script + "console.log(correctionProse(JSON.parse(process.argv[1])));",
            json.dumps(correction),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (
            {"payload": {"text": "BP 120/80"}},
            {"payload": {"text": "BP 130/85"}},
            "Recorded text changed from BP 120/80 to BP 130/85",
        ),
        (
            {"values": [{"value": "5.0", "unit": "mmol/L"}]},
            {"values": [{"value": "5.2", "unit": "mmol/L"}]},
            "Reading changed from 5.0 to 5.2",
        ),
        ({"dose": "20 mg"}, {"dose": "40 mg"}, "Dose changed from 20 mg to 40 mg"),
        (
            {"state": "accepted"},
            {"state": "detached"},
            "Document use changed from Accepted document to Not used",
        ),
    ],
)
def test_correction_prose_uses_structured_values(
    before: dict[str, object], after: dict[str, object], expected: str
) -> None:
    rendered = formatter(
        {
            "before": before,
            "after": after,
            "created_at": "2026-09-11T12:00:00Z",
            "notice": "mission: secret_identifier",
            "predicates": {"private": True},
        }
    )
    assert expected in rendered
    assert "by the doctor on Sep 11, 2026." in rendered
    assert "mission" not in rendered and "private" not in rendered


def test_unstructured_correction_uses_dated_fallback_not_notice() -> None:
    assert formatter({"created_at": "2026-09-11T12:00:00Z", "notice": "private_raw_notice"}) == (
        "Correction recorded on Sep 11, 2026."
    )


def test_correction_prose_escapes_markup_and_retains_zero() -> None:
    result = formatter({"before": {"value": 0}, "after": {"value": "<img>"}})
    assert "from 0 to &lt;img&gt;" in result
    assert "<img>" not in result


class PublicText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.support_depth = 0
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "details" and (self.support_depth or dict(attrs).get("class") == "support"):
            self.support_depth += 1
        if not self.support_depth:
            self.text.extend(v for k, v in attrs if k in {"aria-label", "title"} and v)

    def handle_endtag(self, tag: str) -> None:
        if tag == "details" and self.support_depth:
            self.support_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.support_depth:
            self.text.append(data)


def test_server_shells_keep_identity_out_of_public_text() -> None:
    identifier = "abcdef1234567890abcdef1234567890"
    for html in (
        surface("Synthetic Patient", "en", patient_id=identifier),
        doctor_home(identifier, "Synthetic Doctor"),
        patient_home("Synthetic Patient", 42),
        admin_home(),
    ):
        parsed = PublicText()
        parsed.feed(html)
        assert identifier not in " ".join(parsed.text)
    assert 'class="support"' not in patient_home("Synthetic Patient", 42)
    assert 'class="support"' not in surface("Synthetic Patient", "en", audience="patient")


def test_rendered_labels_have_no_underscore_humanization_fallback() -> None:
    source = SCRIPT.read_text()
    assert "key.replaceAll('_',' ')" not in source
    assert "e.category.replaceAll" not in source
    assert '<pre class="correction-notice">' not in source


def test_every_projected_enum_has_an_explicit_label() -> None:
    from typing import get_args

    from sanad.domain.entities import ReviewKind
    from sanad.store.records import Evidence, Patient

    source = SCRIPT.read_text().split("  const words = {", 1)[1].split("  const t =", 1)[0]
    result = subprocess.run(
        ["node", "-e", "const words={" + source + ";console.log(JSON.stringify(words));"],
        check=True,
        capture_output=True,
        text=True,
    )
    words = json.loads(result.stdout)
    keys = {r.value for r in ReviewKind}
    keys.update(get_args(Evidence.model_fields["category"].annotation))
    keys.update(get_args(Patient.model_fields["contact_status"].annotation))
    assert keys <= words.keys(), keys - words.keys()
    assert all("_" not in words[key][0] for key in keys)
