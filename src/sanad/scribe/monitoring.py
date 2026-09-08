"""Compile supported schedules; preserve the 11e TASK fallback verbatim."""

import re
from dataclasses import dataclass
from datetime import date, datetime

from sanad.domain import DoctorTimingPolicy, MissionKind
from sanad.domain.deadlines import NeedsClarification, ResolvedTiming, resolve_timing
from sanad.domain.entities import MonitorDetails
from sanad.monitor import policy
from sanad.monitor.slots import UNITS, generate, requested_metric
from sanad.scribe.names import normalize

_COUNT = (
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"واحد|اثنين|اتنين|ثلاث|ثلاثة|ثلاثه|اربع|اربعة|اربعه|خمس|خمسة|خمسه|ست|سته|سبع|سبعه)"
)
_FREQUENCY = re.compile(
    rf"{_COUNT}\s+(?:times?\s+(?:(?:a|per|each)\s+day|daily)|مرات\s+(?:في\s+اليوم|يوميا))",
    re.I,
)
_DURATION = re.compile(
    rf"(?:for|لمده|مده)\s+({_COUNT})\s*(days?|hours?|weeks?|ايام|يوم|ساعات|ساعه|اسابيع|اسبوع)",
    re.I,
)

_ENGLISH_COUNTS = dict(
    zip(
        "one two three four five six seven eight nine ten".split(),
        (str(n) for n in range(1, 11)),
        strict=True,
    )
)


def spoken_counts(text: str) -> set[str]:
    """Word counts for presentation coverage only; never clinical numeric support."""
    return {_ENGLISH_COUNTS[t] for t in normalize(text).split() if t in _ENGLISH_COUNTS}


def request_counts(text: str) -> set[str]:
    normalized = normalize(text)
    counts = {match[0].split()[0] for match in _FREQUENCY.finditer(normalized)} | {
        match[1] for match in _DURATION.finditer(normalized)
    }
    return {_ENGLISH_COUNTS.get(value, value) for value in counts}


def task_request(text: str) -> bool:
    text = normalize(text)
    return bool(
        re.search(r"\b(?:measure|record|chart|monitor)\b|قياس|قيس|سجل|جدول", text)
        and _FREQUENCY.search(text)
    )


def duration_expression(text: str) -> str | None:
    match = _DURATION.search(normalize(text))
    return match[0] if match else None


def task_instruction(text: str) -> str:
    """Translate the request grammar; resolve the metric through the shared vocabulary."""
    from sanad.scribe.resolver import resolve_name

    normalized = normalize(text)
    frequency = _FREQUENCY.search(normalized)
    if not frequency:
        return text
    metric = normalized[: frequency.start()].strip(" ,،:")
    metric = re.sub(
        r"^(?:i\s+)?(?:asked\s+(?:him|her)\s+to\s+)?(?:please\s+)?(?:do\s+(?:a\s+)?)?", "", metric
    )
    metric = re.sub(r"^(?:measure|record|monitor|chart|قياس|قيس|سجل|جدول)\s+", "", metric)
    metric = re.sub(r"\s+(?:chart|record)$", "", metric).strip(" ,،:")
    resolved = resolve_name(metric, "finding", text)
    metric = resolved.latin or metric
    words = {
        "واحد": "one",
        "اثنين": "two",
        "اتنين": "two",
        "ثلاث": "three",
        "ثلاثه": "three",
        "اربع": "four",
        "اربعه": "four",
        "خمس": "five",
        "خمسه": "five",
        "ست": "six",
        "سته": "six",
        "سبع": "seven",
        "سبعه": "seven",
    }
    count = frequency[0].split()[0]
    result = metric[:1].upper() + metric[1:] + " chart, " + words.get(count, count) + " times a day"
    duration = _DURATION.search(normalized)
    if duration:
        unit = {
            "ايام": "days",
            "يوم": "days",
            "ساعات": "hours",
            "ساعه": "hours",
            "اسابيع": "weeks",
            "اسبوع": "weeks",
        }.get(duration[2], duration[2])
        result += " for " + words.get(duration[1], duration[1]) + " " + unit
    return result


@dataclass(frozen=True)
class Schedule:
    metric: str
    unit: str
    times_per_day: int
    days: int
    spoken_text: str

    def details(self, anchor: datetime, timezone: str) -> MonitorDetails:
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        start = None
        beginning = re.search(
            r"\b(?:starting|start|from)\s+(today|tomorrow|\d{4}-\d{2}-\d{2})\b",
            self.spoken_text,
            re.I,
        )
        if beginning:
            expression = beginning[1].lower()
            start = (
                anchor.astimezone(ZoneInfo(timezone)).date()
                + timedelta(days=expression == "tomorrow")
                if expression in {"today", "tomorrow"}
                else date.fromisoformat(expression)
            )
        slots = generate(anchor, timezone, self.times_per_day, self.days, start=start)
        return MonitorDetails(
            metric=self.metric, unit=self.unit, slots=slots, required_coverage=len(slots)
        )


def compile_schedule(text: str) -> Schedule | None:
    from sanad.scribe.timing import _DURATION_NUMBERS

    normalized = normalize(text)
    frequency, duration = _FREQUENCY.search(normalized), _DURATION.search(normalized)
    name = requested_metric(normalized)
    if not task_request(text) or not frequency or not duration or not name:
        return None
    if len(_FREQUENCY.findall(normalized)) != 1 or len(_DURATION.findall(normalized)) != 1:
        return None
    if re.search(r"\b(?:or|maybe|about|approximately)\b|تقريبا|\bاو\b", normalized):
        return None
    if re.search(r"(?:[-/]|\bto)\s*$", normalized[: frequency.start()]):
        return None

    def count(word: str) -> int:
        return int(word) if word.isdigit() else _DURATION_NUMBERS.get(word, 0)

    times = count(frequency[0].split()[0])
    days = count(duration[1])
    if duration[2].startswith(("week", "اسب")):
        days *= 7
    elif duration[2].startswith(("hour", "ساع")):
        if days % 24:
            return None
        days //= 24
    if not 1 <= times <= policy.max_times_per_day or not 1 <= days <= policy.max_days:
        return None
    return Schedule(name, UNITS[name], times, days, text)


def _bare_duration(value: str) -> str:
    return re.sub(r"^(?:for|لمده|مده)\s+", "", normalize(value)).strip()


def timing(
    text: str, expression: str | None, anchor: datetime, clock_policy: DoctorTimingPolicy
) -> ResolvedTiming | NeedsClarification:
    from sanad.scribe.timing import resolve_expression

    schedule = compile_schedule(text)
    if schedule is None:
        return NeedsClarification(
            reason_code="monitor_schedule_unclear", message="Clarify the schedule."
        )
    # "five days" and "for five days" are the same duration; only a genuinely
    # different dictated expression is treated as an explicit deadline.
    if expression and _bare_duration(expression) != _bare_duration(duration_expression(text) or ""):
        return resolve_expression(expression, MissionKind.MONITOR, anchor, clock_policy)
    try:
        details = schedule.details(anchor, clock_policy.timezone)
    except ValueError:
        return NeedsClarification(
            reason_code="monitor_start_unclear", message="Clarify the start date."
        )
    return resolve_timing(
        MissionKind.MONITOR,
        anchor,
        clock_policy,
        schedule_end=details.slots[-1],
        state_hint="proposed",
    )


def card_line(text: str, anchor: datetime, timezone: str, language: str) -> str:
    from sanad.monitor.report import render

    schedule = compile_schedule(text)
    if schedule is None:
        return "MONITOR: " + text
    try:
        details = schedule.details(anchor, timezone)
    except ValueError:
        return "MONITOR: " + text
    from zoneinfo import ZoneInfo

    first = details.slots[0].astimezone(ZoneInfo(timezone)).strftime("%a %H:%M")
    if language == "ar":
        first = first.replace(
            details.slots[0].astimezone(ZoneInfo(timezone)).strftime("%a"),
            ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد")[
                details.slots[0].astimezone(ZoneInfo(timezone)).weekday()
            ],
        )
    return render(
        "card",
        language,
        metric=schedule.metric,
        times=str(schedule.times_per_day),
        days=str(schedule.days),
        count=str(len(details.slots)),
        first=first,
    )
