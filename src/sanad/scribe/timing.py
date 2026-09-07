"""Explicit, bounded time parsing; unknown expressions stay clarification."""

import re
from datetime import datetime, timedelta

from sanad.domain import DoctorTimingPolicy, MissionKind
from sanad.domain.deadlines import (
    ExplicitTiming,
    NeedsClarification,
    ResolvedTiming,
    resolve_timing,
)
from sanad.scribe.extract import DictationCandidate, ProposalIssue
from sanad.scribe.proposal import ItemTiming

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_RELATIVE = re.compile(
    r"(?:بعد|خلال|in|within)\s+(\d+)\s*(ساع(?:ة|ه|ات)|أيام|ايام|يوم|days?|hours?|أسابيع|اسابيع|أسبوع|اسبوع|weeks?)",
    re.IGNORECASE,
)
_WORDS = {
    "بكرة": timedelta(days=1),
    "بكره": timedelta(days=1),
    "غدا": timedelta(days=1),
    "tomorrow": timedelta(days=1),
    "بعد يوم": timedelta(days=1),
    "بعد يومين": timedelta(days=2),
    "بعد أسبوع": timedelta(days=7),
    "بعد اسبوع": timedelta(days=7),
    "بعد أسبوعين": timedelta(days=14),
    "بعد اسبوعين": timedelta(days=14),
    "بعد أربع ساعات": timedelta(hours=4),
    "بعد اربع ساعات": timedelta(hours=4),
    "بعد ساعة": timedelta(hours=1),
    "بعد ساعتين": timedelta(hours=2),
}
_DURATION_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "واحد": 1,
    "اثنين": 2,
    "اتنين": 2,
    "ثلاث": 3,
    "ثلاثه": 3,
    "اربع": 4,
    "اربعه": 4,
    "خمس": 5,
    "خمسه": 5,
    "ست": 6,
    "سته": 6,
    "سبع": 7,
    "سبعه": 7,
}


def resolve_expression(
    expression: str | None,
    kind: MissionKind,
    anchor: datetime,
    policy: DoctorTimingPolicy,
) -> ResolvedTiming | NeedsClarification:
    explicit = None
    if expression:
        text = expression.translate(_DIGITS).strip().lower()
        delta = _WORDS.get(text)
        if kind == MissionKind.TASK:
            from sanad.scribe.names import normalize

            duration = re.fullmatch(
                r"(?:for|لمده|مده)\s+(\w+)\s*(days?|hours?|weeks?|ايام|يوم|ساعات|ساعه|اسابيع|اسبوع)",
                normalize(text),
            )
            if duration:
                count = (
                    int(duration[1])
                    if duration[1].isdigit()
                    else _DURATION_NUMBERS.get(duration[1])
                )
                if count is not None and count > 0:
                    unit = duration[2]
                    delta = (
                        timedelta(hours=count)
                        if unit.startswith(("hour", "ساع"))
                        else timedelta(weeks=count)
                        if unit.startswith(("week", "اسب"))
                        else timedelta(days=count)
                    )
        match = _RELATIVE.fullmatch(text)
        if match:
            n, unit = int(match[1]), match[2]
            delta = (
                timedelta(hours=n)
                if unit.startswith(("ساع", "hour"))
                else timedelta(weeks=n)
                if unit.startswith(("أسب", "اسب", "week"))
                else timedelta(days=n)
            )
        instant = anchor + delta if delta is not None else None
        if instant is None:
            try:
                parsed = datetime.fromisoformat(expression.translate(_DIGITS).strip())
                if parsed.tzinfo is not None:
                    instant = parsed
            except ValueError:
                pass
        if instant is None:
            return NeedsClarification(
                reason_code="timing_expression_unclear", message="وضح الموعد والتوقيت في التعديل."
            )
        explicit = ExplicitTiming(
            instant=instant, original_expression=expression, timezone=policy.timezone
        )
    return resolve_timing(kind, anchor, policy, explicit=explicit, state_hint="proposed")


def candidate_timings(
    candidate: DictationCandidate,
    anchor: datetime,
    policy: DoctorTimingPolicy,
) -> tuple[tuple[ItemTiming, ...], tuple[ProposalIssue, ...]]:
    timings, issues = [], []
    from sanad.scribe.monitoring import duration_expression, task_request

    values = [
        (
            f"mission:{i}",
            MissionKind(m.kind),
            m.timing_expression
            or (duration_expression(m.text) if m.kind == "TASK" and task_request(m.text) else None),
        )
        for i, m in enumerate(candidate.missions)
    ]
    # Medication timing is the prescribed dosing schedule, not an acknowledgment deadline.
    values += [
        (f"order:{i}", MissionKind.MEDICATION, None)
        for i, order in enumerate(candidate.orders)
        if order.action in {"start", "change", "stop"}
    ]
    values += [
        (f"{label}:{i}", MissionKind.MEDICATION, expression)
        for i, order in enumerate(candidate.orders)
        for label, expression in (
            ("effective", order.effective_expression),
            ("checkin", order.checkin_expression),
        )
        if expression
    ]
    for item, kind, expression in values:
        resolved = resolve_expression(expression, kind, anchor, policy)
        if isinstance(resolved, NeedsClarification):
            issues.append(
                ProposalIssue(
                    item=("order:" + item.split(":")[1])
                    if item.startswith(("effective:", "checkin:"))
                    else item,
                    code="timing_unclear",
                )
            )
        else:
            timings.append(ItemTiming(item=item, resolved=resolved))
    return tuple(timings), tuple(issues)
