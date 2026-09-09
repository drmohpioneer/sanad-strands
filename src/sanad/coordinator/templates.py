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
    from sanad.presentation.context import resolve
    from sanad.presentation.coordinator import compose

    context = resolve(patient.language, "patient")
    language = context.locale
    text, numbers, lines = compose(bundle, fact_ids, context, policy.max_named_items)
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
