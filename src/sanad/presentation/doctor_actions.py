"""Doctor record actions and truthful, immutable decision outcomes."""

from sanad.presentation.catalog import Catalog, opaque_fields, render, validate_catalog
from sanad.presentation.context import resolve

CATALOG: Catalog = {
    "doctor_actions.answer": {"en": "Answer", "ar": "رد"},
    "doctor_actions.send_answer": {"en": "Send answer", "ar": "ابعت الرد"},
    "doctor_actions.defer": {"en": "Later", "ar": "بعدين"},
    "doctor_actions.close": {"en": "Close without answer", "ar": "اقفل من غير رد"},
    "doctor_actions.extend": {"en": "Give more time", "ar": "ادي وقت أكتر"},
    "doctor_actions.close_unfulfilled": {"en": "Close as not done", "ar": "اقفل من غير ما يتم"},
    "doctor_actions.acknowledge": {"en": "Mark as seen", "ar": "سجل إنك شفته"},
    "doctor_actions.review": {"en": "Record review decision", "ar": "سجل قرار المراجعة"},
    "doctor_actions.resolve_incident": {
        "en": "Record danger decision",
        "ar": "سجل قرار بلاغ الخطر",
    },
    "doctor_actions.resolve_extend": {"en": "Record decision to extend", "ar": "سجل قرار المد"},
    "doctor_actions.resolve_close": {"en": "Record decision to close", "ar": "سجل قرار القفل"},
    "doctor_actions.resolve_cancel": {"en": "Record decision to cancel", "ar": "سجل قرار الإلغاء"},
    "doctor_actions.resolve_answer": {"en": "Record answer decision", "ar": "سجل قرار الرد"},
    "doctor_actions.dispose": {"en": "Record how this was handled", "ar": "سجل إزاي اتعاملت معاه"},
    "doctor_actions.restore_coverage": {"en": "Record who will follow up", "ar": "سجل مين هيتابع"},
    "doctor_actions.associate": {"en": "Record document decision", "ar": "سجل قرار المستند"},
    "doctor_actions.clarify": {"en": "Record clarification", "ar": "سجل التوضيح"},
    "doctor_actions.saved": {"en": "Saved.", "ar": "اتحفظ."},
    "doctor_actions.queued": {
        "en": "Queued for the patient.",
        "ar": "الرسالة مستنية تتبعت للمريض.",
    },
    "doctor_actions.held": {"en": "Held for your review.", "ar": "مستني مراجعتك."},
    "doctor_actions.suppressed": {"en": "Not sent: {reason}.", "ar": "متبعتش: {reason}."},
    "doctor_actions.patient_removed": {"en": "this patient was removed", "ar": "المريض ده اتشال"},
    "doctor_actions.question_delivery_pending": {
        "en": "the patient cannot receive this answer yet",
        "ar": "المريض مش هيقدر يستلم الرد دلوقتي",
    },
    "doctor_actions.delivery_unavailable": {
        "en": "the patient cannot receive this message",
        "ar": "المريض مش هيقدر يستلم الرسالة دي",
    },
    "doctor_actions.stale": {
        "en": "This item changed. Reload to see it.",
        "ar": "البند ده اتغير. حدث الصفحة عشان تشوفه.",
    },
    "doctor_actions.refused": {
        "en": "This action could not be saved. Check the item and try again.",
        "ar": "معرفناش نحفظ الإجراء. راجع البند وحاول تاني.",
    },
    "doctor_actions.show_more": {"en": "Show more", "ar": "اعرض أكتر"},
    "doctor_actions.preview": {"en": "Preview answer", "ar": "راجع الرد"},
    "doctor_actions.your_answer": {"en": "Your answer", "ar": "ردك"},
    "doctor_actions.reason": {"en": "Reason", "ar": "السبب"},
    "doctor_actions.deadline": {
        "en": "New deadline (your browser timezone)",
        "ar": "الموعد الجديد (بتوقيت المتصفح)",
    },
    "doctor_actions.deadline_preview": {
        "en": "New deadline: {due}. Escalation time: {escalation}.",
        "ar": "الموعد الجديد: {due}. ميعاد التنبيه: {escalation}.",
    },
    "doctor_actions.confirm": {"en": "Confirm", "ar": "أكد"},
    "doctor_actions.cancel_form": {"en": "Cancel", "ar": "إلغاء"},
    "doctor_actions.decision_only": {
        "en": (
            "This records your decision. "
            "It does not send a message or change a request or treatment."
        ),
        "ar": "ده بيسجل قرارك. مش بيبعت رسالة ولا بيغير طلب أو علاج.",
    },
    "doctor_actions.preview_deadline": {"en": "Review new deadline", "ar": "راجع الموعد الجديد"},
}
validate_catalog("doctor_actions", CATALOG)


def text(key: str, language: str = "en", **fields: str) -> str:
    return render(
        CATALOG, "doctor_actions." + key, resolve(language, "doctor"), **opaque_fields(fields)
    )


def disposition(action: str, language: str = "en") -> str:
    return text(
        "resolve_" + action if action in {"extend", "close", "cancel", "answer"} else action,
        language,
    )


def outcome(label: str | None, reason: str | None, language: str = "en") -> str:
    if label != "suppressed":
        return text(label or "saved", language)
    key = (
        reason
        if reason in {"patient_removed", "question_delivery_pending"}
        else "delivery_unavailable"
    )
    return text("suppressed", language, reason=text(key, language))
