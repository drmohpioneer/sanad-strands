"""Literal bilingual monitoring wording and descriptive doctor tables."""

from datetime import date
from decimal import Decimal
from string import Formatter
from zoneinfo import ZoneInfo

from sanad.domain import Mission, PatientScope, VersionRef
from sanad.domain.entities import MonitorDetails
from sanad.domain.language import effective
from sanad.monitor import policy
from sanad.monitor.slots import filled
from sanad.store.protocol import Store

OWNER_REVIEW_PENDING = True
TEMPLATES = {
    "card": (
        "MONITOR: {metric}، {times} مرات في اليوم لمدة {days} أيام "
        "({count} قراءة، أول قياس {first})",
        "MONITOR: {metric}, {times} times a day for {days} days ({count} readings, first {first})",
    ),
    "recorded": (
        "سجلت: {value} لموعد {slot}. باقي {remaining} قراءة.",
        "Recorded: {value} for {slot}. {remaining} readings left.",
    ),
    "extra": (
        "سجلت: {value} خارج مواعيد الجدول. باقي {remaining} قراءة.",
        "Recorded: {value} outside the schedule windows. {remaining} readings left.",
    ),
    "choose": ("القراءة دي تخص أنهي جدول قياس؟", "Which monitoring schedule is this reading for?"),
    "verify": (
        "احتفظت بالقراءة. أكد القيمة والوحدة ووقت القياس عشان أسجلها في الجدول.",
        "The reading is retained. Confirm its value, unit and measurement time "
        "to file it in the schedule.",
    ),
    "header": (
        "جدول {metric} ({unit}) · {zone} · {year}",
        "{metric} table ({unit}) · {zone} · {year}",
    ),
    "missing": ("ناقص", "missing"),
    "extras": ("قراءات إضافية: {count}", "Extra readings: {count}"),
    "trend": (
        "من أول لآخر قراءة، {component}: {direction}؛ المدى {low}, {high} {unit}.",
        "First to last, {component}: {direction}; range {low}, {high} {unit}.",
    ),
    "up": ("زيادة", "increase"),
    "down": ("انخفاض", "decrease"),
    "same": ("بدون تغير", "unchanged"),
    "threshold": ("مصدر القرار: {decided_by}", "decided_by: {decided_by}"),
}
_AR_METRIC = {
    "blood pressure": "ضغط الدم",
    "blood glucose": "سكر الدم",
    "weight": "الوزن",
    "pulse": "النبض",
}


def render(key: str, language: str, **fields: str) -> str:
    language = effective(language, audience="doctor")
    template = TEMPLATES[key][language == "en"]
    if {name for _, name, _, _ in Formatter().parse(template) if name} != set(fields):
        raise ValueError("monitor_template_fields")
    if language != "en" and "metric" in fields:
        fields["metric"] = _AR_METRIC.get(fields["metric"], fields["metric"])
    return template.format(**fields)


def assignment_ack(details: MonitorDetails, source: VersionRef, language: str, today: date) -> str:
    language = effective(language, audience="patient")
    assert details.timezone is not None
    zone = ZoneInfo(details.timezone)
    days = (
        ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        if language == "en"
        else ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد")
    )
    lines = []
    for entry in details.readings:
        if entry.source_ref != source:
            continue
        if entry.slot is None:
            lines.append(
                "Recorded as an additional reading."
                if language == "en"
                else "سجلتها كقراءة إضافية."
            )
        else:
            local = details.slots[entry.slot].astimezone(zone)
            time = f"{local.hour:02d}:{local.minute:02d}"
            if local.date() != today:
                time = f"{days[local.weekday()]} {local:%Y-%m-%d} {time}"
            lines.append(
                f"Recorded as your {time} reading."
                if language == "en"
                else f"سجلتها كقراءة موعد {time}."
            )
    return "\n".join(lines)


def patient_reply(
    details: MonitorDetails,
    source: VersionRef,
    language: str,
    timezone: str,
    *,
    today: date | None = None,
) -> str:
    language = effective(language, audience="patient")
    from sanad.safety import validate_patient_output
    from sanad.safety.models import OutputContext
    from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT

    entries = [r for r in details.readings if r.source_ref == source]
    remaining = str(max(0, details.required_coverage - len(filled(details))))
    lines = []
    allowed = [remaining]
    for entry in entries:
        fields = {"value": entry.value, "remaining": remaining}
        if entry.slot is not None:
            fields["slot"] = (
                details.slots[entry.slot].astimezone(ZoneInfo(timezone)).strftime("%m-%d %H:%M")
            )
        allowed.extend(fields.values())
        lines.append(render("recorded" if entry.slot is not None else "extra", language, **fields))
    if details.slot_rule == "window-next-v1" and entries:
        local_today = (
            today
            or max(e.received_at for e in entries)
            .astimezone(ZoneInfo(details.timezone or timezone))
            .date()
        )
        acknowledgement = assignment_ack(details, source, language, local_today)
        lines = [
            acknowledgement,
            f"{remaining} readings left." if language == "en" else f"باقي {remaining} قراءة.",
        ]
        allowed.append(acknowledgement)
    text = "\n".join(lines)
    checked = validate_patient_output(
        text,
        context=OutputContext(
            mode="plan_explanation",
            language="en" if language == "en" else "ar",
            allowed_numbers=tuple(allowed),
        ),
        policy=SAFETY_POLICY_V1_CARDIOLOGY_DRAFT,
    )
    return text if checked.ok else render("verify", language)


def table(details: MonitorDetails, timezone: str, language: str) -> str:
    language = effective(language, audience="doctor")
    readings = filled(details)
    zone = ZoneInfo(timezone)
    years = sorted({str(at.astimezone(zone).year) for at in details.slots})
    lines = [
        render(
            "header",
            language,
            metric=details.metric,
            unit=details.unit,
            zone=timezone,
            year="/".join(years),
        )
    ]
    for index, at in enumerate(details.slots):
        value = readings[index].value if index in readings else render("missing", language)
        lines.append(at.astimezone(zone).strftime("%m-%d %H:%M") + " | " + value)
    lines.append(
        render("extras", language, count=str(sum(r.slot is None for r in details.readings)))
    )
    if len(readings) >= policy.trend_minimum:
        ordered = [readings[i].value.split("/") for i in sorted(readings)]
        for component in range(len(ordered[0])):
            numbers = [Decimal(value[component]) for value in ordered]
            direction = (
                "up" if numbers[-1] > numbers[0] else "down" if numbers[-1] < numbers[0] else "same"
            )
            name = (
                (("systolic", "diastolic") if language == "en" else ("انقباضي", "انبساطي"))[
                    component
                ]
                if len(ordered[0]) == 2
                else (
                    _AR_METRIC.get(details.metric, details.metric)
                    if language != "en"
                    else details.metric
                )
            )
            lines.append(
                render(
                    "trend",
                    language,
                    component=name,
                    direction=render(direction, language),
                    low=str(min(numbers)),
                    high=str(max(numbers)),
                    unit=details.unit,
                )
            )
    return "\n".join(lines)


def doctor_table(store: Store, scope: PatientScope, mission: Mission, language: str) -> str:
    from sanad.monitor.executor import current_details
    from sanad.steward.types import records

    details = current_details(store, scope, mission)
    sources = set()
    for reading in details.readings:
        row = store.get(scope, reading.source_ref.entity_type, reading.source_ref.id)
        provenance = row.body.get("provenance") if row else None
        if isinstance(provenance, dict):
            sources.add(provenance.get("source_observation_id"))
    lines = [table(details, mission.timezone, language)]
    thresholds = []
    for row in records(store, scope, "incident"):
        facts = row.body.get("facts")
        if not isinstance(facts, dict):
            continue
        source = facts.get("source")
        if not isinstance(source, dict) or source.get("observation_id") not in sources:
            continue
        verdict = facts.get("verdict")
        provenance = (
            str(verdict.get("decided_by") or facts.get("rule_id"))
            if isinstance(verdict, dict)
            else str(facts.get("rule_id"))
        )
        thresholds.append(
            render(
                "threshold",
                language,
                decided_by=provenance
                + " · "
                + str(facts.get("rule_id"))
                + " · "
                + str(facts.get("policy_version")),
            )
        )
        hits = facts.get("patient_alerts")
        if isinstance(hits, list):
            for hit in hits:
                if isinstance(hit, dict):
                    thresholds.append(
                        render(
                            "threshold",
                            language,
                            decided_by="patient_alert:"
                            + str(hit.get("head_id"))
                            + " · "
                            + " ".join(
                                str(hit.get(k) or "")
                                for k in ("metric", "comparator", "threshold", "unit")
                            ),
                        )
                    )
    return "\n".join((*lines, *dict.fromkeys(thresholds)))
