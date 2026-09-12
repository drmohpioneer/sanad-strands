"""Code-rendered evidence wording. Every string is OWNER_REVIEW_PENDING."""

from sanad.domain.language import default_language, effective
from sanad.presentation.context import PresentationContext

OWNER_REVIEW_PENDING = True
TEXT = {
    "patient_evidence_received_pending": (
        "وصلت الصورة، بقراها وهرد عليك",
        "I received the photo. I will read it and reply.",
    ),
    "patient_evidence_unreadable": (
        "الصورة مش واضحة، صوّرها تاني في نور كويس",
        "The photo is unclear. Please take it again in good light.",
    ),
    "patient_evidence_stale": (
        "الاختيار ده انتهى أو اتغيّر. ابعت المستند أو اطلب توضيح تاني.",
        "This choice expired or changed. Send the document or ask for clarification again.",
    ),
    "patient_evidence_duplicate": ("الصورة دي وصلت قبل كده", "This photo was received before."),
    "patient_evidence_which": (
        "الصورة دي تخص أنهي طلب من الدكتور؟",
        "Which doctor's request is this photo for?",
    ),
    "patient_evidence_kept": (
        "حفظت الصورة في ملفك وهعرضها على الدكتور",
        "I saved the photo in your record for the doctor.",
    ),
    "patient_evidence_name_check": (
        "الاسم المكتوب على الورقة مش هو اسمك، دي بتاعتك؟",
        "The name on the paper does not match yours. Is this your document?",
    ),
    "patient_evidence_accepted": (
        "وصل تحليل {title} وسجلته للدكتور؛ الدكتور هو اللي بيقيّم النتيجة",
        "I received {title} and recorded it for the doctor; the doctor evaluates the result.",
    ),
    "patient_evidence_partial": ("لسه ناقص: {missing}", "Still missing: {missing}"),
    "patient_evidence_one_per_photo": (
        "صوّر كل ورقة لوحدها وابعتها في صورة منفصلة",
        "Photograph each paper separately and send a separate photo.",
    ),
    "doctor_objective_deadline_pending_verification": (
        "{title}، الميعاد {due_local}: وصل ملف قبل الميعاد ولسه بيتراجع",
        "{title}, due {due_local}: a file arrived before the deadline and verification is pending.",
    ),
    "doctor_evidence_card": (
        "مستند محتاج قرار: {title}\n{details}",
        "Evidence needs a decision: {title}\n{details}",
    ),
    "doctor_evidence_already_handled": (
        "الاختيار ده اتنفذ بالفعل.",
        "This evidence decision is already handled.",
    ),
    "doctor_evidence_stale": (
        "الاختيار ده انتهى أو اتغير، افتح /evidence تاني",
        "This choice expired or changed. Open /evidence again.",
    ),
    "doctor_evidence_empty": ("مفيش مستندات مستنية ربط", "No evidence is waiting for association."),
    "doctor_evidence_action_recorded": (
        "اتسجّل قرارك بخصوص المستند",
        "Your evidence decision was recorded.",
    ),
    "doctor_evidence_identity": (
        "الاسم على الورقة مقدرتش أقراه، دي ورقة {patient}؟",
        "The name on this paper could not be read. Is this {patient}'s paper?",
    ),
    "history_label": ("تاريخ، مش أمر حالي", "History, not a current order"),
    "alert_refused": (
        "حد التنبيه ده أضعف من حد الخطر الثابت أو وحدته مش قابلة للمقارنة؛ اكتب حد أوضح وأشد.",
        "This alert is weaker than the fixed danger threshold or its unit cannot be compared; "
        "enter a clearer, stricter threshold.",
    ),
    "context_heading": ("السياق المسجّل:", "Recorded context:"),
    "previous_prefix": ("السابق: ", "Previous: "),
    "alert_prefix": ("حد التنبيه الخاص: ", "Patient alert: "),
    "danger_fulfillment": (
        "وصل المستند المطلوب؛ مراجعة الدكتور للنتيجة لسه مستقلة.",
        "The requested evidence was received; the doctor's result review remains independent.",
    ),
    "danger_associated": (
        "الصورة اللي اتنبهت لها قبل كده اتربطت بالمريض.\n",
        "The previously alerted photo was associated with this patient.\n",
    ),
    "unverified": ("غير متحقق", "unverified"),
}
MISSING = {
    "K": "البوتاسيوم",
    "Na": "الصوديوم",
    "Glucose": "السكر",
    "HCO3": "البيكربونات",
    "Hb": "الهيموجلوبين",
    "Platelets": "الصفائح",
    "Potassium": "البوتاسيوم",
    "Creatinine": "الكرياتينين",
    "Sodium": "الصوديوم",
    "collection_date": "تاريخ عينة داخل الفترة المطلوبة",
    "printed_date": "تاريخ الورقة",
    "period": "ورق من الفترة المطلوبة",
    "category": "نوع الورق المطلوب",
    "verification": "مراجعة قراءة الورقة",
    "identity": "تأكيد الدكتور لاسم صاحب الورقة",
    "readable_document": "صورة مقروءة",
    "doctor_acceptance": "قبول الدكتور للمستند",
    "one_document_per_photo": "صورة منفصلة لكل ورقة",
    "slot_assignment": "ربط القياس بميعاده",
}
MISSING_EN = {
    "K": "potassium",
    "Na": "sodium",
    "HCO3": "bicarbonate",
    "Hb": "hemoglobin",
    "collection_date": "a sample date within the requested period",
    "printed_date": "the document date",
    "period": "documents from the requested period",
    "category": "the requested document category",
    "verification": "verification of the document reading",
    "identity": "the doctor's confirmation of whose document this is",
    "readable_document": "a readable photo",
    "doctor_acceptance": "the doctor's acceptance of the document",
    "one_document_per_photo": "a separate photo for each paper",
    "slot_assignment": "the measurement's scheduled time",
}
BUTTONS = {
    "yes": ("نعم", "Yes"),
    "no": ("لا", "No"),
    "other": ("حاجة تانية", "Something else"),
    "associate": ("ربط", "Associate"),
    "accept": ("قبول", "Accept"),
    "reject": ("رفض", "Reject"),
    "confirm_identity": ("ورقة المريض ده ✅", "This patient's paper ✅"),
    "reject_identity": ("مش ورقته ❌", "Not this patient ❌"),
}


def button(key: str, language: str) -> str:
    return BUTTONS[key][effective(language, audience="doctor") == "en"]


def missing_text(missing: tuple[str, ...], language: str) -> str:
    if effective(language, audience="doctor") == "en":
        return ", ".join(
            MISSING_EN.get(
                v,
                "the required number of documents"
                if v.startswith("documents:")
                else "the requested document category"
                if v.startswith("category:")
                else v,
            )
            for v in missing
        )
    return "، ".join(
        MISSING.get(
            v,
            "عدد المستندات المطلوبة"
            if v.startswith("documents:")
            else "نوع الورق المطلوب"
            if v.startswith("category:")
            else v,
        )
        for v in missing
    )


def render(key: str, language: str | PresentationContext = default_language, **fields: str) -> str:
    from sanad.presentation import evidence
    from sanad.presentation.catalog import opaque_fields
    from sanad.presentation.catalog import render as catalog_render
    from sanad.presentation.context import resolve

    context = resolve(language, "patient" if key.startswith("patient_") else "doctor")
    catalog_key = "evidence." + key
    wanted = evidence.FIELDS[catalog_key]
    if wanted != fields.keys() or any(
        not v.strip() or "{" in v or "}" in v for v in fields.values()
    ):
        raise ValueError("evidence_template_fields")
    return catalog_render(evidence.CATALOG, catalog_key, context, **opaque_fields(fields))
