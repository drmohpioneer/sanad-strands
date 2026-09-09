"""Accepted coordinator wording; legacy fragments and punctuation are intentional."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanad.presentation.catalog import Catalog, OpaqueValue, render, validate_catalog
from sanad.presentation.context import PresentationContext

if TYPE_CHECKING:
    from sanad.coordinator.permitted import Permitted

CATALOG: Catalog = {
    "coordinator.title": {
        "ar": "المطلوب المسجّل: {value}.",
        "en": "Recorded request: {value}.",
    },
    "coordinator.due": {
        "ar": "الميعاد: {value}.",
        "en": "Due: {value}.",
    },
    "coordinator.slots": {
        "ar": "قراءات لسه مش مسجلة لمواعيد: {value}.",
        "en": "Readings not yet recorded for: {value}.",
    },
    "coordinator.categories": {
        "ar": "أنواع الورق للطلب ده: {value}.",
        "en": "Document types for this request: {value}.",
    },
    "coordinator.more_one": {
        "ar": "و{value} كمان",
        "en": "and {value} additional item",
    },
    "coordinator.more": {
        "ar": "و{value} كمان",
        "en": "and {value} additional items",
    },
    "coordinator.arrange": {
        "ar": "المطلوب هو ترتيب الزيارة.",
        "en": "The request is to arrange the visit.",
    },
    "coordinator.booking_reported": {
        "ar": "المطلوب هو بلاغ حجز الزيارة.",
        "en": "The request needs a booking report.",
    },
    "coordinator.attendance_reported": {
        "ar": "المطلوب هو بلاغ حضور الزيارة.",
        "en": "The request needs an attendance report.",
    },
    "coordinator.task": {
        "ar": "المطلوب لسه مستني بلاغ إتمامك.",
        "en": "The request is awaiting your completion report.",
    },
    "coordinator.lab_result": {
        "ar": "نتيجة التحليل",
        "en": "lab result",
    },
    "coordinator.imaging_report": {
        "ar": "تقرير الأشعة",
        "en": "imaging report",
    },
    "coordinator.discharge_summary": {
        "ar": "ملخص الخروج",
        "en": "discharge summary",
    },
    "coordinator.prescription": {
        "ar": "روشتة",
        "en": "prescription",
    },
    "coordinator.medication_list": {
        "ar": "قائمة الأدوية",
        "en": "medication list",
    },
    "coordinator.monitor_screen": {
        "ar": "صورة شاشة القياس",
        "en": "monitor screen",
    },
    "coordinator.other": {
        "ar": "تقرير",
        "en": "report",
    },
}

FIELDS = validate_catalog("coordinator", CATALOG)


def compose(
    bundle: Permitted,
    fact_ids: tuple[str, ...],
    context: PresentationContext,
    max_named_items: int,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Preserve accepted fragment composition and overflow, using one context."""

    def fragment(key: str, value: str = "") -> str:
        full_key = "coordinator." + key
        fields = {"value": OpaqueValue(value)} if "value" in FIELDS[full_key] else {}
        return render(CATALOG, full_key, context, **fields)

    by_id = {f.id: f for f in bundle.facts}
    selected = [by_id[id] for id in fact_ids]
    lines: list[str] = []
    consumed: set[str] = set()
    numbers: list[str] = []
    named = 0
    for fact in selected:
        if fact.id in consumed:
            continue
        if fact.kind in {"slot", "category"}:
            group = [f for f in selected if f.kind == fact.kind]
            remaining = max(0, max_named_items - named)
            shown = group[:remaining]
            named += len(shown)
            values = [fragment(f.value) if f.kind == "category" else f.value for f in shown]
            numbers.extend(f.value for f in shown if f.kind == "slot")
            extra = len(group) - len(shown)
            if extra:
                values.append(fragment("more_one" if extra == 1 else "more", str(extra)))
                numbers.append(str(extra))
            lines.append(
                fragment("slots" if fact.kind == "slot" else "categories", ", ".join(values))
            )
            consumed.update(f.id for f in group)
        elif fact.kind == "visit":
            lines.append(fragment(fact.value))
        elif fact.kind == "task":
            lines.append(fragment("task"))
        else:
            lines.append(fragment(fact.kind, fact.value))
            numbers.append(fact.value)
    text = " ".join(lines)
    return text, tuple(numbers), tuple(lines)
