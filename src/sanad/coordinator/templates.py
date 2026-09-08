"""Bilingual code fragments; model output is never interpolated into a sentence."""

from sanad.agents.hygiene import patient_failure
from sanad.coordinator import policy
from sanad.coordinator.permitted import Permitted
from sanad.safety import screen_text
from sanad.safety.models import OutputContext
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as SAFETY
from sanad.safety.validator import typed_numbers
from sanad.store.records import Patient

TEMPLATES = {
    "title": ("المطلوب المسجّل: {value}.", "Recorded request: {value}."),
    "due": ("الميعاد: {value}.", "Due: {value}."),
    "slots": ("قراءات لسه مش مسجلة لمواعيد: {value}.", "Readings not yet recorded for: {value}."),
    "categories": ("أنواع الورق للطلب ده: {value}.", "Document types for this request: {value}."),
    "more_one": ("و{value} كمان", "and {value} additional item"),
    "more": ("و{value} كمان", "and {value} additional items"),
    "arrange": ("المطلوب هو ترتيب الزيارة.", "The request is to arrange the visit."),
    "booking_reported": ("المطلوب هو بلاغ حجز الزيارة.", "The request needs a booking report."),
    "attendance_reported": (
        "المطلوب هو بلاغ حضور الزيارة.",
        "The request needs an attendance report.",
    ),
    "task": ("المطلوب لسه مستني بلاغ إتمامك.", "The request is awaiting your completion report."),
    "lab_result": ("نتيجة التحليل", "lab result"),
    "imaging_report": ("تقرير الأشعة", "imaging report"),
    "discharge_summary": ("ملخص الخروج", "discharge summary"),
    "prescription": ("روشتة", "prescription"),
    "medication_list": ("قائمة الأدوية", "medication list"),
    "monitor_screen": ("صورة شاشة القياس", "monitor screen"),
    "other": ("تقرير", "report"),
}


def render(bundle: Permitted, fact_ids: tuple[str, ...], patient: Patient) -> str:
    language = patient.language

    def fragment(key: str, value: str = "") -> str:
        return TEMPLATES[key][language == "en"].format(value=value)

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
            remaining = max(0, policy.max_named_items - named)
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
    if any(kind in {"dose", "count", "frequency"} for _, kind, _ in typed_numbers(text)):
        raise ValueError("output_validation")
    # These are code-owned fragments from the closed fact bundle, never model
    # text. Keep their labels so the unchanged validator can classify dates.
    if patient_failure(
        text,
        OutputContext(
            mode="plan_explanation", language=language, allowed_numbers=tuple((*numbers, *lines))
        ),
        SAFETY,
    ):
        raise ValueError("output_validation")
    if screen_text(text, policy=SAFETY).level != "none":
        raise ValueError("safety_kernel")
    return text
