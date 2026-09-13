"""Accepted evidence wording; legacy fragments and punctuation are intentional."""

from sanad.presentation.catalog import Catalog, validate_catalog

CATALOG: Catalog = {
    "evidence.doctor_evidence_refused": {
        "ar": "مقدرتش أسجل القرار. افتح /evidence وحاول تاني.",
        "en": "This evidence decision could not be recorded. Open /evidence and try again.",
    },
    "evidence.doctor_evidence_mission_closed": {
        "ar": "الطلب ده مقفول. افتح /evidence تاني.",
        "en": "This request is closed. Open /evidence again.",
    },
    "evidence.doctor_evidence_identity_required": {
        "ar": "تأكد الأول إن الورقة للمريض ده.",
        "en": "Identity is not confirmed. Confirm it is this patient first.",
    },
    "evidence.patient_evidence_received_pending": {
        "ar": "وصلت الصورة، بقراها وهرد عليك",
        "en": "I received the photo. I will read it and reply.",
    },
    "evidence.patient_evidence_unreadable": {
        "ar": "الصورة مش واضحة، صوّرها تاني في نور كويس",
        "en": "The photo is unclear. Please take it again in good light.",
    },
    "evidence.patient_evidence_stale": {
        "ar": "الاختيار ده انتهى أو اتغيّر. ابعت المستند أو اطلب توضيح تاني.",
        "en": ("This choice expired or changed. Send the document or ask for clarification again."),
    },
    "evidence.patient_evidence_duplicate": {
        "ar": "الصورة دي وصلت قبل كده",
        "en": "This photo was received before.",
    },
    "evidence.patient_evidence_which": {
        "ar": "الصورة دي تخص أنهي طلب من الدكتور؟",
        "en": "Which doctor's request is this photo for?",
    },
    "evidence.patient_evidence_kept": {
        "ar": "حفظت الصورة في ملفك وهعرضها على الدكتور",
        "en": "I saved the photo in your record for the doctor.",
    },
    "evidence.patient_evidence_name_check": {
        "ar": "الاسم المكتوب على الورقة مش هو اسمك، دي بتاعتك؟",
        "en": "The name on the paper does not match yours. Is this your document?",
    },
    "evidence.patient_evidence_accepted": {
        "ar": "وصل تحليل {title} وسجلته للدكتور؛ الدكتور هو اللي بيقيّم النتيجة",
        "en": (
            "I received {title} and recorded it for the doctor; the doctor evaluates the result."
        ),
    },
    "evidence.patient_evidence_partial": {
        "ar": "لسه ناقص: {missing}",
        "en": "Still missing: {missing}",
    },
    "evidence.patient_evidence_one_per_photo": {
        "ar": "صوّر كل ورقة لوحدها وابعتها في صورة منفصلة",
        "en": "Photograph each paper separately and send a separate photo.",
    },
    "evidence.doctor_objective_deadline_pending_verification": {
        "ar": "{title}، الميعاد {due_local}: وصل ملف قبل الميعاد ولسه بيتراجع",
        "en": (
            "{title}, due {due_local}: a file arrived before the deadline and "
            "verification is pending."
        ),
    },
    "evidence.doctor_evidence_card": {
        "ar": "مستند محتاج قرار: {title}\n{details}",
        "en": "Evidence needs a decision: {title}\n{details}",
    },
    "evidence.doctor_evidence_already_handled": {
        "ar": "الاختيار ده اتنفذ بالفعل.",
        "en": "This evidence decision is already handled.",
    },
    "evidence.doctor_evidence_stale": {
        "ar": "الاختيار ده انتهى أو اتغير، افتح /evidence تاني",
        "en": "This evidence changed since listing or the choice expired. Open /evidence again.",
    },
    "evidence.doctor_evidence_empty": {
        "ar": "مفيش مستندات مستنية ربط",
        "en": "No evidence is waiting for association.",
    },
    "evidence.doctor_evidence_action_recorded": {
        "ar": "اتسجّل قرارك بخصوص المستند",
        "en": "Your evidence decision was recorded.",
    },
    "evidence.doctor_evidence_identity": {
        "ar": "الاسم على الورقة مقدرتش أقراه، دي ورقة {patient}؟",
        "en": "The name on this paper could not be read. Is this {patient}'s paper?",
    },
    "evidence.history_label": {
        "ar": "تاريخ، مش أمر حالي",
        "en": "History, not a current order",
    },
    "evidence.alert_refused": {
        "ar": (
            "حد التنبيه ده أضعف من حد الخطر الثابت أو وحدته مش قابلة للمقارنة؛ اكتب حد أوضح وأشد."
        ),
        "en": (
            "This alert is weaker than the fixed danger threshold or its unit "
            "cannot be compared; enter a clearer, stricter threshold."
        ),
    },
    "evidence.context_heading": {
        "ar": "السياق المسجّل:",
        "en": "Recorded context:",
    },
    "evidence.previous_prefix": {
        "ar": "السابق: ",
        "en": "Previous: ",
    },
    "evidence.alert_prefix": {
        "ar": "حد التنبيه الخاص: ",
        "en": "Patient alert: ",
    },
    "evidence.danger_fulfillment": {
        "ar": "وصل المستند المطلوب؛ مراجعة الدكتور للنتيجة لسه مستقلة.",
        "en": (
            "The requested evidence was received; the doctor's result review remains independent."
        ),
    },
    "evidence.danger_associated": {
        "ar": "الصورة اللي اتنبهت لها قبل كده اتربطت بالمريض.\n",
        "en": "The previously alerted photo was associated with this patient.\n",
    },
    "evidence.unverified": {
        "ar": "غير متحقق",
        "en": "unverified",
    },
}

FIELDS = validate_catalog("evidence", CATALOG)
