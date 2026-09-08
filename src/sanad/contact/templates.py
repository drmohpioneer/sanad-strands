"""Checked Egyptian Arabic/English contact templates, pending owner review."""

from string import Formatter
from typing import Literal

from sanad.domain import FollowUpTask, Mission, PatientScope
from sanad.domain.deadlines import format_local
from sanad.domain.language import default_language
from sanad.safety import validate_patient_output
from sanad.safety.models import OrderSummary, OutputContext
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT
from sanad.safety.validator import typed_numbers
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.records import CareOrderVersion
from sanad.store.protocol import Store
from sanad.store.records import Patient, from_record

OWNER_REVIEW_PENDING = True
TEMPLATES = {
    "patient_chase_test": (
        "فاكر التحليل اللي الدكتور طلبه؟ {title}، الميعاد {due_local}",
        "Remember the test your doctor requested? {title}, due {due_local}",
    ),
    "patient_chase_visit": (
        "فاكر الزيارة اللي الدكتور طلبها؟ {title}، الميعاد {due_local}",
        "Remember the visit your doctor requested? {title}, due {due_local}",
    ),
    "patient_chase_task": (
        "فاكر المطلوب من الدكتور؟ {title}، الميعاد {due_local}",
        "Remember your doctor's requested task? {title}, due {due_local}",
    ),
    "patient_chase_send_records": (
        "فاكر الورق اللي الدكتور طلبه؟ {title}، الميعاد {due_local}",
        "Remember the records your doctor requested? {title}, due {due_local}",
    ),
    "patient_chase_medication_start": (
        'بدأت {drug}؟ ابعتلي "بدأت" لما تبدأ',
        'Have you begun {drug}? Send "started" once you have begun.',
    ),
    "patient_monitor_prompt": (
        "وقت قياس {metric}؛ ابعت الرقم زي ما هو",
        "It is time to measure {metric}; send the number as shown.",
    ),
    "patient_day3_prompt": (
        "عدّى 3 أيام على {drug}، عامل إيه معاه؟ لو في حاجة مقلقاك قولي",
        "It has been 3 days since starting {drug}. How are you getting on? "
        "Tell me if anything concerns you.",
    ),
    "doctor_objective_done": (
        "اكتمل المطلوب المسجّل: {title}. ده مش تأكيد مراجعة طبية.",
        "The recorded objective was fulfilled: {title}. This does not confirm clinical review.",
    ),
    "doctor_objective_deadline": (
        "مطلوب لسه ما اكتملش: {title}، الميعاد {due_local}.",
        "An objective remains unfinished: {title}, due {due_local}.",
    ),
    "doctor_weekly_bundle": (
        "بنود لسه محتاجة متابعة:\n{lines}",
        "Items still needing follow-up:\n{lines}",
    ),
    "doctor_medication_done": (
        "اكتمل المطلوب حسب كلام المريض: {title}. ده مش تأكيد مراجعة طبية.",
        "The objective was fulfilled as reported by the patient: {title}. "
        "This does not confirm clinical review.",
    ),
    "doctor_medication_anchor_unknown": (
        "تاريخ البداية غير مؤكد.",
        "The start date is uncertain.",
    ),
    "doctor_medication_barrier": (
        "عائق: {type} — «{text}»",
        'Barrier: {type} - "{text}"',
    ),
    "doctor_barrier_attempt": (
        "محاولة المساعدة: {summary}",
        "Barrier attempt: {summary}",
    ),
    "patient_visit_brief": (
        "الزيارة المطلوبة: {title}، بتاريخ {due_local}.\n{lines}",
        "Your requested visit: {title}, on {due_local}.\n{lines}",
    ),
    "patient_visit_bring_test": (
        "خد معاك: نتيجة تحليل {title}.",
        "Bring with you: test results for {title}.",
    ),
    "patient_visit_bring_records": (
        "خد معاك: {title}.",
        "Bring with you: {title}.",
    ),
    "doctor_task_done": (
        "بلاغ إتمام المطلوب للمريض {patient}: {title}. ده بلاغ من المريض؛ لسه مستني قبولك.",
        "Task completion report for {patient}: {title}. Self-reported; pending doctor acceptance.",
    ),
    "doctor_task_accept_button": ("تمام ✅", "Accept ✅"),
    "doctor_task_reopen_button": ("مش كفاية ↩", "Not enough ↩"),
}


def render(key: str, language: str = default_language, **fields: str) -> str:
    template = TEMPLATES[key][language == "en"]
    expected = {field for _, field, _, _ in Formatter().parse(template) if field}
    if expected != set(fields):
        raise ValueError("contact_template_fields")
    return template.format(**fields)


def patient_text(
    store: Store, source: Mission | FollowUpTask, patient: Patient, template: str
) -> str:
    from sanad.coordinator.integration import selected_text

    if selected := selected_text(store, source, patient, template):
        return selected
    scope = PatientScope(doctor_id=source.doctor_id, patient_id=source.patient_id)
    summaries = []
    drugs = []
    for ref in source.order_refs:
        row = store.get(scope, "care_order_version", f"{ref.id}:{ref.version}")
        if row:
            order = from_record(row, CareOrderVersion)
            if isinstance(order.structured_instruction, OrderCandidate):
                drug = order.structured_instruction.drug
                drugs.append(drug)
                summaries.append(OrderSummary(order_ref=ref, drug_names=(drug,)))
    fields: dict[str, str]
    allowed: list[str] = []
    if template in {"patient_day3_prompt", "patient_chase_medication_start"}:
        if len(drugs) != 1:
            raise ValueError("contact_drug_source_required")
        fields = {"drug": drugs[0]}
        if isinstance(source, FollowUpTask) and source.prompt_at and source.anchor_time:
            days = (source.prompt_at - source.anchor_time).days
            allowed.extend((str(days), f"{days} أيام", f"{days} days"))
    elif isinstance(source, Mission) and source.details.kind == "MONITOR":
        fields = {"metric": source.details.metric}
    elif isinstance(source, Mission):
        fields = {"title": source.title, "due_local": format_local(source.due_at, patient.timezone)}
    else:
        raise ValueError("contact_template_source")
    text = render(template, patient.language, **fields)
    if any(kind in {"dose", "count", "frequency"} for _, kind, _ in typed_numbers(text)):
        # Contact wording never repeats doses, even when a source title contains one.
        raise ValueError("contact_template_validation")
    language: Literal["ar", "en"] = patient.language
    verdict = validate_patient_output(
        text,
        context=OutputContext(
            active_orders=tuple(summaries),
            allowed_numbers=tuple((*fields.values(), *allowed)),
            mode="plan_explanation",
            language=language,
        ),
        policy=SAFETY_POLICY_V1_CARDIOLOGY_DRAFT,
    )
    if not verdict.ok:
        raise ValueError("contact_template_validation")
    return text
