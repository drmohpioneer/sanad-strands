"""Offline checks of the shipped JavaScript clock and exact runtime link wording."""

import json
import subprocess
from pathlib import Path

import pytest

from sanad.channels.telegram import wording

SCRIPT = Path("src/sanad/web/static/browser.js").read_text()


@pytest.mark.parametrize(
    ("due", "now", "zone", "expected"),
    [
        ("2026-09-09T18:30:00Z", "2026-09-12T12:00:00Z", "Africa/Cairo", "overdue by 2 days"),
        ("2026-09-12T08:10:00Z", "2026-09-12T12:00:00Z", "Africa/Cairo", "overdue by 3 hours"),
        ("2026-09-12T11:40:00Z", "2026-09-12T12:00:00Z", "Africa/Cairo", "due now"),
        ("2026-09-12T12:00:00Z", "2026-09-12T12:00:00Z", "Africa/Cairo", "due now"),
        ("2026-09-12T19:00:00Z", "2026-09-12T12:00:00Z", "Africa/Cairo", "due today"),
        ("2026-09-12T22:30:00Z", "2026-09-12T19:00:00Z", "Africa/Cairo", "due in 3 hours"),
        ("2026-09-12T22:30:00Z", "2026-09-12T19:00:00Z", "UTC", "due today"),
        ("2026-09-13T21:00:00Z", "2026-09-12T19:00:00Z", "Africa/Cairo", "due in 1 day"),
        ("2026-09-16T18:00:00Z", "2026-09-12T19:00:00Z", "Africa/Cairo", "due in 3 days"),
        ("2026-09-13T00:10:00Z", "2026-09-12T23:40:00Z", "UTC", "due now"),
        ("2026-11-01T07:30:00Z", "2026-11-01T04:30:00Z", "America/New_York", "due today"),
        ("2026-09-12T11:00:00Z", "2026-09-12T12:00:00Z", "UTC", "overdue by 1 hour"),
    ],
)
def test_review_relative_time(due: str, now: str, zone: str, expected: str) -> None:
    catalog = SCRIPT[SCRIPT.index("  Object.assign(words, {") : SCRIPT.index("  const t = key =>")]
    function = SCRIPT[
        SCRIPT.index("  function reviewTime(") : SCRIPT.index("  function reviewPresentation(")
    ]
    program = (
        "const words={};"
        + catalog
        + "const t=key=>words[key][0];"
        + function
        + "process.stdout.write(reviewTime("
        + json.dumps(due)
        + ",Date.parse("
        + json.dumps(now)
        + "),"
        + json.dumps(zone)
        + "));"
    )
    assert subprocess.check_output(["node", "-e", program], text=True) == expected


@pytest.mark.parametrize("key", ["doctor_login_link", "patient_login_link"])
@pytest.mark.parametrize(
    ("language", "index", "expected"),
    [
        (
            "en",
            1,
            "Your one-time sign-in link, valid for ten minutes. Copy it and paste it into "
            "your browser. Do not tap it inside Telegram.\nhttps://example.test/synthetic",
        ),
        (
            "ar",
            0,
            "لينك دخولك لسند صالح لعشر دقايق ولمرة واحدة. انسخه والصقه في المتصفح، "
            "ومتدوسش عليه جوه تيليجرام.\nhttps://example.test/synthetic",
        ),
    ],
)
def test_one_time_login_runtime_wording(
    key: str, language: str, index: int, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    assert wording.render(key, language, link="https://example.test/synthetic") == expected
    assert (
        wording.ALL_TEMPLATES[key][index].format(link="https://example.test/synthetic") == expected
    )


@pytest.mark.parametrize(
    ("language", "index", "expected"),
    [
        (
            "en",
            1,
            "Open the link or scan the code, consent to linking, and wait for the doctor's "
            "confirmation.\nValid for 24 hours\nhttps://example.test/synthetic\n"
            "A new /qr replaces this link.",
        ),
        (
            "ar",
            0,
            "افتح اللينك أو امسح الكود، وبعدها وافق على الربط واستنى تأكيد الدكتور.\n"
            "صالح 24 ساعة\nhttps://example.test/synthetic\nأي /qr جديد بيلغي اللينك ده.",
        ),
    ],
)
def test_qr_runtime_wording(
    language: str, index: int, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    assert (
        wording.render("scribe_invitation", language, link="https://example.test/synthetic")
        == expected
    )
    assert (
        wording.SCRIBE_TEMPLATES["scribe_invitation"][index].format(
            link="https://example.test/synthetic"
        )
        == expected
    )
