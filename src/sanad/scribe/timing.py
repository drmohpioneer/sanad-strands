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
    values = [
        (f"mission:{i}", MissionKind(m.kind), m.timing_expression)
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
