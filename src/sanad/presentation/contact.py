"""Accepted contact wording; legacy fragments and punctuation are intentional."""

from sanad.presentation.catalog import Catalog, validate_catalog

CATALOG: Catalog = {
    "contact.patient_chase_test": {
        "ar": "فاكر التحليل اللي الدكتور طلبه؟ {title}، الميعاد {due_local}",
        "en": "Remember the test your doctor requested? {title}, due {due_local}",
    },
    "contact.patient_chase_visit": {
        "ar": "فاكر الزيارة اللي الدكتور طلبها؟ {title}، الميعاد {due_local}",
        "en": "Remember the visit your doctor requested? {title}, due {due_local}",
    },
    "contact.patient_chase_task": {
        "ar": "فاكر المطلوب من الدكتور؟ {title}، الميعاد {due_local}",
        "en": "Remember your doctor's requested task? {title}, due {due_local}",
    },
    "contact.patient_chase_send_records": {
        "ar": "فاكر الورق اللي الدكتور طلبه؟ {title}، الميعاد {due_local}",
        "en": "Remember the records your doctor requested? {title}, due {due_local}",
    },
    "contact.patient_chase_medication_start": {
        "ar": 'بدأت {drug}؟ ابعتلي "بدأت" لما تبدأ',
        "en": 'Have you begun {drug}? Send "started" once you have begun.',
    },
    "contact.patient_monitor_prompt": {
        "ar": "وقت قياس {metric}؛ ابعت الرقم زي ما هو",
        "en": "It is time to measure {metric}; send the number as shown.",
    },
    "contact.patient_day3_prompt": {
        "ar": "عدّى 3 أيام على {drug}، عامل إيه معاه؟ لو في حاجة مقلقاك قولي",
        "en": (
            "It has been 3 days since starting {drug}. How are you getting on? "
            "Tell me if anything concerns you."
        ),
    },
    "contact.doctor_objective_done": {
        "ar": "اكتمل المطلوب المسجّل: {title}. ده مش تأكيد مراجعة طبية.",
        "en": (
            "The recorded objective was fulfilled: {title}. This does not confirm clinical review."
        ),
    },
    "contact.doctor_objective_deadline": {
        "ar": "مطلوب لسه ما اكتملش: {title}، الميعاد {due_local}.",
        "en": "An objective remains unfinished: {title}, due {due_local}.",
    },
    "contact.doctor_weekly_bundle": {
        "ar": "بنود لسه محتاجة متابعة:\n{lines}",
        "en": "Items still needing follow-up:\n{lines}",
    },
    "contact.doctor_medication_done": {
        "ar": "اكتمل المطلوب حسب كلام المريض: {title}. ده مش تأكيد مراجعة طبية.",
        "en": (
            "The objective was fulfilled as reported by the patient: {title}. This "
            "does not confirm clinical review."
        ),
    },
    "contact.doctor_medication_anchor_unknown": {
        "ar": "تاريخ البداية غير مؤكد.",
        "en": "The start date is uncertain.",
    },
    "contact.doctor_medication_barrier": {
        "ar": "عائق: {type} — «{text}»",
        "en": 'Barrier: {type} - "{text}"',
    },
    "contact.doctor_barrier_attempt": {
        "ar": "محاولة المساعدة: {summary}",
        "en": "Barrier attempt: {summary}",
    },
    "contact.patient_visit_brief": {
        "ar": "الزيارة المطلوبة: {title}، بتاريخ {due_local}.\n{lines}",
        "en": "Your requested visit: {title}, on {due_local}.\n{lines}",
    },
    "contact.patient_visit_bring_test": {
        "ar": "خد معاك: نتيجة تحليل {title}.",
        "en": "Bring with you: test results for {title}.",
    },
    "contact.patient_visit_bring_records": {
        "ar": "خد معاك: {title}.",
        "en": "Bring with you: {title}.",
    },
    "contact.doctor_task_done": {
        "ar": ("بلاغ إتمام المطلوب للمريض {patient}: {title}. ده بلاغ من المريض؛ لسه مستني قبولك."),
        "en": (
            "Task completion report for {patient}: {title}. Self-reported; pending "
            "doctor acceptance."
        ),
    },
    "contact.doctor_task_accept_button": {
        "ar": "تمام ✅",
        "en": "Accept ✅",
    },
    "contact.doctor_task_reopen_button": {
        "ar": "مش كفاية ↩",
        "en": "Not enough ↩",
    },
}

FIELDS = validate_catalog("contact", CATALOG)
