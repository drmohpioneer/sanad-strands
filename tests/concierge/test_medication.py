"""Handwritten bilingual input and calendar oracles; no providers."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from sanad.concierge import reports
from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY, OWNER_REVIEW_PENDING
from sanad.contact.templates import render


@pytest.mark.parametrize(
    "text,action",
    [
        ("I started it", "start"),
        ("بدأت الدوا", "start"),
        ("I stopped it", "stop"),
        ("وقفت", "stop"),
        ("بطلت", "stop"),
        ("مبقتش اخد", "stop"),
        ("I changed it", "change"),
        ("I took the new dose", "change"),
        ("غيرت", "change"),
        ("بدأت الجرعة الجديدة", "change"),
        ("خدت الجرعة الجديدة", "change"),
    ],
)
def test_acknowledgments(text: str, action: str) -> None:
    assert getattr(reports, "is_" + action)(text)


@pytest.mark.parametrize(
    "text,action",
    [
        ("started but not started", "start"),
        ("بدأت ومبدأتش", "start"),
        ("مبدأتش", "start"),
        ("I didn't stop it", "stop"),
        ("I have not stopped it", "stop"),
        ("موقفتش", "stop"),
        ("لسه مغيرتش", "change"),
        ("I haven't changed it", "change"),
        ("I have not taken the new dose", "change"),
        ("I didn't take the new dose", "change"),
        ("I haven't taken it but started is written on the card", "start"),
        ("should I stop it?", "stop"),
    ],
)
def test_negative_reports(text: str, action: str) -> None:
    assert not getattr(reports, "is_" + action)(text)


@pytest.mark.parametrize(
    "text,kind",
    [
        ("I can't afford it", "cost"),
        ("غالي", "cost"),
        ("معيش فلوس", "cost"),
        ("not available", "availability"),
        ("مش لاقي", "availability"),
        ("مش موجود", "availability"),
        ("خلص من الصيدلية", "availability"),
        ("I forgot", "forgot"),
        ("نسيت", "forgot"),
        ("I don't understand", "confusion"),
        ("مش فاهم", "confusion"),
        ("ازاي اخده", "confusion"),
        ("dizzy", "side_effect_experience"),
        ("بيتعبني", "side_effect_experience"),
        ("دوخة", "side_effect_experience"),
        ("غثيان", "side_effect_experience"),
        ("I can't do it", "other"),
        ("مش هقدر", "other"),
    ],
)
def test_barrier_seeds(text: str, kind: str) -> None:
    assert reports.recognize_barrier(text) == kind


@pytest.mark.parametrize(
    "text", ["Why is it not available?", "what if I forgot?", "هل الدوا غالي؟"]
)
def test_barrier_inside_question_is_not_a_report(text: str) -> None:
    assert reports.recognize_barrier(text) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("today", "2026-09-08"),
        ("yesterday", "2026-09-07"),
        ("day before yesterday", "2026-09-06"),
        ("أول امبارح", "2026-09-06"),
        ("Thursday", "2026-09-03"),
        ("Tuesday", "2026-09-08"),
        ("يوم الخميس", "2026-09-03"),
        ("2026-09-02", "2026-09-02"),
        ("3 days ago", "2026-09-05"),
        ("من 3 أيام", "2026-09-05"),
        ("tomorrow", None),
        ("2026-09-09", None),
        ("2026-02-30", None),
    ],
)
def test_cairo_calendar(text: str, expected: str | None) -> None:
    # 22 UTC is already the following date in Cairo.
    now = datetime(2026, 9, 7, 22, tzinfo=UTC)
    found, instant = reports.reported_date(text, now, "Africa/Cairo")
    assert found and reports.date_only(text)
    assert (
        instant.astimezone(ZoneInfo("Africa/Cairo")).date().isoformat() if instant else None
    ) == expected


def test_no_ingestion_claim_and_draft_policy() -> None:
    assert OWNER_REVIEW_PENDING
    assert DRAFT_CONCIERGE_POLICY.barrier_resume_after.days == 1
    for language in ("en", "ar"):
        line = render("doctor_medication_done", language, title="Synthetic medicine")
        assert ("as reported by the patient" if language == "en" else "حسب كلام المريض") in line
    for folder in ("concierge", "contact"):
        for path in (Path("src/sanad") / folder).rglob("*.py"):
            text = path.read_text()
            assert "verified adherence" not in text and "اتأكدنا إنه خد" not in text
