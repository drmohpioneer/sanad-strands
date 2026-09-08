"""Pure schedule arithmetic and the shared text/photo coverage predicate."""

import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sanad.concierge.records import Reading
from sanad.domain import VersionRef
from sanad.domain.deadlines import utc_instant
from sanad.domain.entities import MonitorDetails, MonitorReading
from sanad.domain.predicates import PredicateResult
from sanad.monitor import policy

_METRICS = {
    "blood pressure": ("bp", "blood pressure", "ضغط الدم", "الضغط", "ضغط"),
    "blood glucose": ("glucose", "blood glucose", "blood sugar", "سكر", "السكر", "سكري", "جلوكوز"),
    "weight": ("weight", "وزن", "وزني", "الوزن"),
    "pulse": ("pulse", "heart rate", "نبض", "نبضي", "النبض"),
}
UNITS = {"blood pressure": "mmHg", "blood glucose": "mg/dL", "weight": "kg", "pulse": "bpm"}


def metric(text: str) -> str | None:
    normalized = " ".join(text.casefold().split())
    return next((name for name, aliases in _METRICS.items() if normalized in aliases), None)


def requested_metric(text: str) -> str | None:
    matches = {
        name
        for name, aliases in _METRICS.items()
        if any(re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", text, re.I) for a in aliases)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def generate(
    confirmed_at: datetime,
    timezone: str,
    times_per_day: int,
    days: int,
    *,
    start: date | None = None,
) -> tuple[datetime, ...]:
    confirmed_at = utc_instant(confirmed_at)
    if type(times_per_day) is not int or times_per_day not in policy.slot_hours:
        raise ValueError("monitor_frequency_unclear")
    if type(days) is not int or not 1 <= days <= policy.max_days:
        raise ValueError("monitor_duration_unclear")
    zone = ZoneInfo(timezone)
    first = start or (confirmed_at.astimezone(zone).date() + timedelta(days=1))
    # As with computed contact clocks, a fold/gap chooses the later UTC instant.
    return tuple(
        max(local.replace(tzinfo=zone, fold=f).astimezone(UTC) for f in (0, 1))
        for day in range(days)
        for hour in policy.slot_hours[times_per_day]
        for local in (datetime.combine(first + timedelta(days=day), time(hour)),)
    )


def slot_for(details: MonitorDetails, observed_at: datetime) -> int | None:
    instant = utc_instant(observed_at)
    if not details.slots:
        return None
    # Consider occupied slots too: a duplicate must replace its closest slot,
    # never overflow into the next empty one. Ties choose the earlier slot.
    index = min(range(len(details.slots)), key=lambda i: (abs(details.slots[i] - instant), i))
    return index if abs(details.slots[index] - instant) <= policy.slot_tolerance else None


def value_for(details: MonitorDetails, reading: Reading) -> str | None:
    name = metric(details.metric)
    if not name or metric(reading.analyte) != name or reading.judgment == "implausible":
        return None
    unit = (reading.raw_unit or "").casefold().replace(" ", "")
    unit = {"كيلو": "kg", "ملجم/دل": "mg/dl"}.get(unit, unit)
    if unit != details.unit.casefold().replace(" ", "") or details.unit != UNITS[name]:
        return None
    raw = reading.raw_value.replace(",", ".").replace("٫", ".")
    if name == "blood pressure":
        return raw if re.fullmatch(r"\d{2,3}/\d{2,3}", raw) else None
    try:
        number = Decimal(raw)
        return format(number.normalize(), "f") if number.is_finite() and number > 0 else None
    except InvalidOperation:
        return None


def attach(
    details: MonitorDetails,
    values: tuple[Reading, ...],
    ref: VersionRef,
    observed_at: datetime,
    received_at: datetime,
) -> MonitorDetails:
    entries = list(details.readings)
    for index, reading in enumerate(values):
        value = value_for(details, reading)
        if value is None or any(e.source_ref == ref and e.reading_index == index for e in entries):
            continue
        entries.append(
            MonitorReading(
                source_ref=ref,
                reading_index=index,
                observed_at=observed_at,
                received_at=received_at,
                slot=slot_for(details, observed_at),
                value=value,
            )
        )
    return details.model_copy(update={"readings": tuple(entries)})


def filled(details: MonitorDetails) -> dict[int, MonitorReading]:
    result: dict[int, MonitorReading] = {}
    for reading in details.readings:
        if reading.slot is not None and reading.slot < len(details.slots):
            result[reading.slot] = reading
    return result


def coverage(details: MonitorDetails, now: datetime) -> PredicateResult:
    occupied = filled(details)
    missing = tuple(f"slot:{i}" for i in range(len(details.slots)) if i not in occupied)
    satisfied = len(occupied) >= details.required_coverage
    return PredicateResult(
        satisfied=satisfied,
        missing=() if satisfied else missing,
        detail=f"{len(occupied)}/{details.required_coverage} required readings recorded.",
        evaluated_at=now,
    )
