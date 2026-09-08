"""Literal schedule, assignment, parser and report outcomes; no providers."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from sanad.concierge.reports import reading
from sanad.domain import DRAFT_POLICY_2026_09, VersionRef
from sanad.domain.deadlines import NeedsClarification, ResolvedTiming
from sanad.domain.entities import MonitorDetails
from sanad.monitor.executor import reading_time
from sanad.monitor.report import TEMPLATES, patient_reply, table
from sanad.monitor.slots import attach, coverage, filled, generate, slot_for
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as SAFETY
from sanad.scribe.extract import DictationCandidate, MissionCandidate
from sanad.scribe.monitoring import card_line, compile_schedule, timing
from sanad.scribe.timing import candidate_timings

NOW = datetime(2026, 9, 6, 20, 50, tzinfo=UTC)  # Sunday 23:50 Cairo.


@pytest.mark.parametrize(
    "name,unit",
    [("blood pressure", "mmHg"), ("blood glucose", "mg/dL"), ("weight", "kg"), ("pulse", "bpm")],
)
def test_supported_request_and_default(name: str, unit: str) -> None:
    text = f"Measure {name} three times a day for five days"
    schedule = compile_schedule(text)
    assert schedule and (schedule.metric, schedule.unit, schedule.times_per_day, schedule.days) == (
        name,
        unit,
        3,
        5,
    )
    assert MissionCandidate(kind="TASK", text=text).kind == "MONITOR"
    details = schedule.details(NOW, "Africa/Cairo")
    assert len(details.slots) == details.required_coverage == 15
    assert details.slots[0] == datetime(2026, 9, 7, 5, tzinfo=UTC)
    assert details.slots[-1] == datetime(2026, 9, 11, 17, tzinfo=UTC)
    resolved = timing(text, None, NOW, DRAFT_POLICY_2026_09)
    assert isinstance(resolved, ResolvedTiming)
    assert resolved.due_at == datetime(2026, 9, 12, 7, tzinfo=UTC)
    assert resolved.timing_anchor.kind == "schedule_end"
    assert resolved.escalation_at == resolved.due_at


@pytest.mark.parametrize(
    "zone,start",
    [
        ("Africa/Cairo", "2026-09-07T05:00:00+00:00"),
        ("America/New_York", "2026-09-07T12:00:00+00:00"),
        ("Asia/Kolkata", "2026-09-08T02:30:00+00:00"),
    ],
)
def test_local_dates(zone: str, start: str) -> None:
    assert generate(NOW, zone, 2, 2)[0] == datetime.fromisoformat(start)


@pytest.mark.parametrize(
    "zone,anchor,expected",
    [
        ("Africa/Cairo", "2026-10-28T12:00:00+00:00", 25),
        ("America/New_York", "2026-03-06T12:00:00+00:00", 23),
    ],
)
def test_dst_keeps_local_hours(zone: str, anchor: str, expected: int) -> None:
    slots = generate(datetime.fromisoformat(anchor), zone, 1, 3)
    assert [s.astimezone(ZoneInfo(zone)).hour for s in slots] == [8, 8, 8]
    assert (slots[1] - slots[0]) == timedelta(hours=expected)


@pytest.mark.parametrize("times,days", [(0, 5), (5, 5), (1, 31), (1, 0), (True, 1)])
def test_schedule_bounds(times: int, days: int) -> None:
    with pytest.raises(ValueError):
        generate(NOW, "Africa/Cairo", times, days)


@pytest.mark.parametrize(
    "text",
    [
        "Measure oxygen 3 times a day for 5 days",
        "Measure blood pressure 5 times a day for 5 days",
        "Measure blood pressure 3 times a day",
        "Measure blood pressure 3 times a day for 31 days",
        "Measure blood pressure 2-3 times a day for 5 days",
        "Measure blood pressure 2 times a day or 3 times a day for 5 days",
        "Measure blood pressure 3 times a day for 5 days or 10 days",
    ],
)
def test_fallback_and_clarification(text: str) -> None:
    assert compile_schedule(text) is None
    candidate = DictationCandidate(missions=(MissionCandidate(kind="TEST", text=text),))
    assert candidate.missions[0].kind == "TASK"
    _, issues = candidate_timings(candidate, NOW, DRAFT_POLICY_2026_09)
    assert bool(issues) == ("blood pressure" in text)


def test_explicit_deadline_and_start() -> None:
    text = "Measure pulse 2 times a day for 5 days starting today"
    schedule = compile_schedule(text)
    assert schedule
    assert schedule.details(NOW, "Africa/Cairo").slots[0] < NOW
    result = timing(text, "in 4 hours", NOW, DRAFT_POLICY_2026_09)
    assert isinstance(result, ResolvedTiming) and result.due_at == NOW + timedelta(hours=4)
    assert result.due_source == "doctor"
    assert isinstance(timing(text, "sometime", NOW, DRAFT_POLICY_2026_09), NeedsClarification)


def test_invalid_explicit_start_uses_existing_clarification() -> None:
    text = "Measure pulse 2 times a day for 5 days starting 2026-99-99"
    assert isinstance(timing(text, None, NOW, DRAFT_POLICY_2026_09), NeedsClarification)
    assert card_line(text, NOW, "Africa/Cairo", "en") == "MONITOR: " + text


def details() -> MonitorDetails:
    return MonitorDetails(
        metric="blood pressure",
        unit="mmHg",
        slots=generate(NOW, "Africa/Cairo", 3, 1),
        required_coverage=3,
    )


@pytest.mark.parametrize(
    "hours,index",
    [(-3, 0), (-3.001, None), (0, 0), (3, 0), (3.001, 1), (6, 1), (12, 2), (15, 2), (15.001, None)],
)
def test_tolerance_and_overlap(hours: float, index: int | None) -> None:
    d = details()
    assert slot_for(d, d.slots[0] + timedelta(hours=hours)) == index


def test_duplicates_extras_coverage_and_trend() -> None:
    d = details()
    for i, (delta, value) in enumerate(
        [(0, "120/80"), (1, "130/85"), (6, "140/90"), (20, "150/95")]
    ):
        at = d.slots[0] + timedelta(hours=delta)
        ref = VersionRef(entity_type="clinical_fact", id=f"reading-{i}", version=1)
        d = attach(d, reading(value, SAFETY).values, ref, at, at)
    assert len(filled(d)) == 2 and len(d.readings) == 4
    assert filled(d)[0].value == "130/85"
    assert coverage(d, NOW).missing == ("slot:2",)
    assert "missing" in table(d, "Africa/Cairo", "en")
    assert "First to last" not in table(d, "Africa/Cairo", "en")
    at = d.slots[2]
    ref = VersionRef(entity_type="clinical_fact", id="last", version=1)
    d = attach(d, reading("160/100", SAFETY).values, ref, at, at)
    assert attach(d, reading("160/100", SAFETY).values, ref, at, at) == d
    assert coverage(d, at).satisfied
    rendered = table(d, "Africa/Cairo", "en")
    assert "Extra readings: 1" in rendered
    assert "systolic: increase; range 130–160 mmHg" in rendered
    assert "0 readings left" in patient_reply(d, ref, "en", "Africa/Cairo")


@pytest.mark.parametrize(
    "text", ["300/280", "pulse 70 bpm", "BP 150", "glucose 100", "weight 70 mg/dL"]
)
def test_incompatible_reading_never_fills(text: str) -> None:
    d = details()
    assert (
        attach(
            d,
            reading(text, SAFETY).values,
            VersionRef(entity_type="clinical_fact", id="invalid", version=1),
            d.slots[0],
            d.slots[0],
        )
        == d
    )


@pytest.mark.parametrize("language", ["en", "ar"])
def test_large_card_and_table(language: str) -> None:
    text = "Measure blood pressure 4 times a day for 30 days"
    schedule = compile_schedule(text)
    assert schedule
    d = schedule.details(NOW, "Africa/Cairo")
    assert len(d.slots) == 120
    card = card_line(text, NOW, "Africa/Cairo", language)
    assert "120" in card and "08:00" in card and len(card) < 300
    rendered = table(d, "Africa/Cairo", language)
    assert len(rendered.splitlines()) == 122 and len(rendered) < 3500
    assert ("missing" if language == "en" else "ناقص") in rendered
    assert len(TEMPLATES) == 13 and all(len(pair) == 2 for pair in TEMPLATES.values())


@pytest.mark.parametrize(
    "text,expected",
    [
        ("BP 120/80 at 22:00", "2026-09-06T19:00:00+00:00"),
        ("BP 120/80 at 25:00", None),
        ("BP 120/80 2026-09-06T18:00Z", "2026-09-06T18:00:00+00:00"),
        ("BP 120/80 2026-09-07T18:00Z", None),
    ],
)
def test_own_reading_time(text: str, expected: str | None) -> None:
    result = reading_time(text, NOW, "Africa/Cairo")
    assert result == (datetime.fromisoformat(expected) if expected else None)
