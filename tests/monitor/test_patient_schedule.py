"""Contract 29: dates, fixed assignments and independent schedule evidence."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from domain_fixtures import mission

from sanad.concierge.records import ScheduleReading
from sanad.domain import EvidencePredicate, Mission, MissionState, VersionRef
from sanad.domain.entities import MonitorDetails, MonitorReading
from sanad.monitor.reschedule import (
    Refused,
    cited_time,
    local_instant,
    project,
    slot_id,
    verify_readers,
)
from sanad.monitor.slots import coverage, generate, slot_for

ZONE = "Africa/Cairo"


def local(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(ZONE)).astimezone(UTC)


def scheduled(
    *, first: str = "2026-09-13T07:00", state: MissionState = MissionState.open
) -> Mission:
    slots = generate(local(first), ZONE, 2, 4)
    return mission(
        state=state,
        kind="MONITOR",
        order_refs=(),
        title="Blood pressure",
        timezone=ZONE,
        due_at=slots[-1] + timedelta(hours=1),
        escalation_at=slots[-1] + timedelta(hours=1),
        objective_predicate=EvidencePredicate(evaluator="monitor"),
        details=MonitorDetails(
            metric="blood pressure",
            unit="mmHg",
            slots=slots,
            required_coverage=len(slots),
            timezone=ZONE,
            times_per_day=2,
            slot_rule="window-next-v1",
        ),
    )


def change(
    m: Mission, times: tuple[str, ...] = ("09:00", "21:00"), now: str = "2026-09-14T12:00"
) -> Mission:
    at = local(now)
    return project(
        m, times, at.astimezone(ZoneInfo(ZONE)).date() + timedelta(days=1), "receipt-29", at
    )


def test_next_day_history_and_preserved_coverage() -> None:
    m = scheduled()
    assert isinstance(m.details, MonitorDetails)
    reading = MonitorReading(
        source_ref=VersionRef(entity_type="clinical_fact", id="reading", version=1),
        reading_index=0,
        observed_at=m.details.slots[0],
        received_at=m.details.slots[0],
        slot=0,
        value="120/80",
    )
    m = m.model_copy(update={"details": m.details.model_copy(update={"readings": (reading,)})})
    n = change(m)
    assert isinstance(n.details, MonitorDetails) and isinstance(m.details, MonitorDetails)
    assert n.details.slots[:4] == m.details.slots[:4]
    assert n.details.slots[4] == local("2026-09-15T09:00")
    assert n.details.readings == m.details.readings
    assert coverage(n.details, local("2026-09-14T12:00")) == coverage(
        m.details, local("2026-09-14T12:00")
    )
    assert (n.due_at, n.escalation_at, n.review_at) == (m.due_at, m.escalation_at, m.review_at)
    assert n.details.time_history[0].model_dump(mode="json") == {
        "effective_date": "2026-09-15",
        "old_times": ["10:00", "22:00"],
        "new_times": ["09:00", "21:00"],
        "moved": [4, 5, 6, 7],
        "generation": 1,
        "source_receipt_id": "receipt-29",
    }
    assert slot_for(n.details, local("2026-09-15T08:59")) == 3
    assert slot_for(n.details, local("2026-09-15T09:00")) == 4
    assert slot_id(n, 0, prompt=True) == "monitor:synthetic-mission:0"
    assert slot_id(n, 4, prompt=True) == "monitor:synthetic-mission:r1-4"


@pytest.mark.parametrize(
    "times",
    [
        ("09:00",),
        ("21:00", "09:00"),
        ("09:00", "10:59"),
        ("05:59", "21:00"),
        ("09:00", "23:31"),
        ("bad", "21:00"),
    ],
)
def test_time_rules(times: tuple[str, ...]) -> None:
    with pytest.raises(Refused, match="rules"):
        change(scheduled(), times)


@pytest.mark.parametrize(
    "state",
    [
        MissionState.blocked,
        MissionState.unreachable,
        MissionState.awaiting_link,
        MissionState.fulfilled,
        MissionState.cancelled,
        MissionState.closed_unfulfilled,
        MissionState.superseded,
        MissionState.proposed,
    ],
)
def test_ineligible_states(state: MissionState) -> None:
    with pytest.raises(Refused, match="refused"):
        change(scheduled(state=state))


def test_legacy_noop_deadline_and_midnight() -> None:
    m = scheduled()
    with pytest.raises(Refused, match="unchanged"):
        change(m, ("10:00", "22:00"))
    with pytest.raises(Refused, match="past_end"):
        change(m, ("09:00", "23:30"))
    with pytest.raises(Refused, match="stale"):
        project(m, ("09:00", "21:00"), date(2026, 9, 15), "r", local("2026-09-15T00:01"))
    assert isinstance(m.details, MonitorDetails)
    with pytest.raises(Refused, match="refused"):
        change(
            m.model_copy(
                update={"details": m.details.model_copy(update={"slot_rule": "tolerance-3h"})}
            )
        )


def test_partial_day_repeated_edits_and_minutes_final_window() -> None:
    m = scheduled(first="2026-09-13T21:00")
    n = change(m)
    assert isinstance(n.details, MonitorDetails)
    assert n.details.slots[3] == local("2026-09-15T09:00")
    second = change(n, ("09:00", "20:30"))
    assert isinstance(second.details, MonitorDetails)
    assert second.details.time_history[-1].old_times == ("09:00", "21:00")
    assert slot_id(second, 3) == slot_id(n, 3)
    assert slot_id(second, 4).endswith(":r2-4")
    with pytest.raises(Refused, match="unchanged"):
        change(second, ("09:00", "20:30"))
    assert slot_for(second.details, local("2026-09-17T20:29")) == 7
    assert slot_for(second.details, local("2026-09-17T20:30")) is None


def test_filled_future_frozen_and_spacing_refused() -> None:
    m = scheduled()
    assert isinstance(m.details, MonitorDetails)
    reading = MonitorReading(
        source_ref=VersionRef(entity_type="clinical_fact", id="f", version=1),
        reading_index=0,
        observed_at=m.details.slots[3],
        received_at=m.details.slots[3],
        slot=4,
        value="120/80",
    )
    m = m.model_copy(update={"details": m.details.model_copy(update={"readings": (reading,)})})
    n = change(m)
    assert isinstance(n.details, MonitorDetails)
    assert isinstance(m.details, MonitorDetails)
    assert n.details.slots[4] == m.details.slots[4]
    assert n.details.readings == (reading,)
    assert 4 not in n.details.time_history[0].moved
    with pytest.raises(Refused, match="filled"):
        change(m, ("08:00", "11:00"))


@pytest.mark.parametrize(
    "zone,day,clock,expected",
    [
        ("America/New_York", date(2026, 3, 8), "02:30", "2026-03-08T07:00:00+00:00"),
        ("America/New_York", date(2026, 11, 1), "01:30", "2026-11-01T06:30:00+00:00"),
    ],
)
def test_gap_rounds_forward_and_fold_uses_later(
    zone: str, day: date, clock: str, expected: str
) -> None:
    assert local_instant(day, clock, zone).isoformat() == expected


@pytest.mark.parametrize(
    "quote,value",
    [
        ("09:00", "09:00"),
        ("21", "21:00"),
        ("9am", "09:00"),
        ("9 at night", "21:00"),
        ("9 بالليل", "21:00"),
    ],
)
def test_unambiguous_citations(quote: str, value: str) -> None:
    assert cited_time(quote) == value


def test_reader_agreement_citations_negation_and_ambiguous_hour() -> None:
    left = ScheduleReading.model_validate(
        {
            "asserted": True,
            "mission_id": "m",
            "times": [{"value": "09:00", "quote": "9am"}, {"value": "21:00", "quote": "9pm"}],
        }
    )
    assert verify_readers("change to 9am and 9pm", (left, left)) == ("m", ("09:00", "21:00"))
    with pytest.raises(Refused, match="rules"):
        verify_readers("change to 9am", (left, left))
    with pytest.raises(Refused, match="rules"):
        verify_readers(
            "change to 9am and 9pm", (left, left.model_copy(update={"mission_id": "other"}))
        )
    assert verify_readers("don't change to 9am and 9pm", (left, left)) is None
    with pytest.raises(Refused, match="which_half:9"):
        cited_time("9")


@pytest.mark.parametrize(
    "name,unit",
    [("blood pressure", "mmHg"), ("blood glucose", "mg/dL"), ("weight", "kg"), ("pulse", "bpm")],
)
def test_every_supported_metric_keeps_count_and_duration(name: str, unit: str) -> None:
    m = scheduled()
    assert isinstance(m.details, MonitorDetails)
    m = m.model_copy(
        update={"details": m.details.model_copy(update={"metric": name, "unit": unit})}
    )
    result = change(m)
    assert isinstance(result.details, MonitorDetails)
    assert len(result.details.slots) == 8
    assert result.details.required_coverage == 8
    assert result.details.metric == name and result.details.unit == unit
    assert result.due_at == m.due_at


def test_no_remaining_slots_and_elapsed_overdue_deadline() -> None:
    with pytest.raises(Refused, match="refused"):
        change(scheduled(), now="2026-09-20T12:00")
    m = scheduled(state=MissionState.overdue)
    assert change(m).state == MissionState.overdue
    m = m.model_copy(
        update={"due_at": local("2026-09-14T11:00"), "escalation_at": local("2026-09-14T11:00")}
    )
    with pytest.raises(Refused, match="past_end"):
        change(m)


def test_all_approved_sentences_are_not_treatment_changes() -> None:
    from sanad.presentation.concierge import CATALOG
    from sanad.safety.validator import wants_treatment_change

    for key, words in CATALOG.items():
        if key.startswith("concierge.patient_schedule_"):
            assert wants_treatment_change(words["en"].format(date="2026-09-15", hour="9")) is False
    assert (
        wants_treatment_change(
            "Patient changed reading times from 10:00, 22:00 to 09:00, 21:00, starting 2026-09-15."
        )
        is False
    )


def test_repeated_edit_preserves_first_slot_position_when_filled() -> None:
    m = scheduled(first="2026-09-16T07:00")
    n = change(m)
    assert isinstance(n.details, MonitorDetails)
    entry = MonitorReading(
        source_ref=VersionRef(entity_type="clinical_fact", id="frozen", version=1),
        reading_index=0,
        observed_at=n.details.slots[0],
        received_at=local("2026-09-14T12:00"),
        slot=0,
        value="120/80",
    )
    n = n.model_copy(update={"details": n.details.model_copy(update={"readings": (entry,)})})
    changed = change(n, ("08:00", "20:00"))
    again = change(changed, ("07:00", "19:00"))
    assert isinstance(again.details, MonitorDetails)
    assert again.details.slots[0] == local("2026-09-16T09:00")
    assert again.details.slots[2] == local("2026-09-17T07:00")
