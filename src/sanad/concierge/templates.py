"""Deterministic patient wording; operational dates never promise a doctor answer."""

from sanad.domain.language import default_language

TEMPLATES = {
    "patient_photo_pending": (
        "الصورة محفوظة ومستنية مراجعة المحتوى، لسه مفيش نتيجة اتأكدت منها.",
        "Your image is saved for content review; no result has been verified.",
    ),
    "patient_voice_unreadable": (
        "مسمعتش كويس، ابعت تاني أو اكتب.",
        "I could not hear clearly. Please send it again or type it.",
    ),
    "patient_treatment_change_relay": (
        "ده قرار الدكتور، بعتّله سؤالك.",
        "That decision belongs to your doctor. Your question is in the doctor’s queue.",
    ),
    "patient_stop_ack": (
        "تمام، مش هبعتلك تذكيرات تاني. لو احتجت حاجة ابعتلي.",
        "Reminders are stopped. You can still message me when you need help.",
    ),
    "patient_snooze_ack": (
        "تمام، أجّلت التذكيرات لحد {until}.",
        "Reminders are snoozed until {until}.",
    ),
    "patient_snooze_clarify": (
        "تحب تأجّل كام ساعة أو كام يوم؟ أقصى مدة 7 أيام.",
        "How many hours or days? The maximum is 7 days.",
    ),
    "patient_resume_ask": (
        "تحب أرجع أبعتلك التذكيرات؟",
        "Would you like me to send reminders again?",
    ),
    "patient_resume_ack": (
        "تمام، وافقت إن التذكيرات ترجع.",
        "You have agreed to receive reminders again.",
    ),
    "patient_quiet_ack": (
        "سجلت ساعات الهدوء. المواعيد اللي الدكتور حددها زي ما هي.",
        "Quiet hours are saved. Your doctor’s clinical times are unchanged.",
    ),
    "patient_quiet_clarify": (
        "اكتب ساعات الهدوء كده: من 22:00 لحد 08:00.",
        "Please write a quiet-hours window, such as 22:00 to 08:00.",
    ),
    "patient_start_recorded": (
        "سجلت إنك بلّغت ببداية الدوا. المتابعة بعد 3 أيام من البداية، ومعادها المسجل:",
        "Your start report is recorded. The check-in is 3 days after the start; its recorded time:",
    ),
    "patient_start_stopped": (
        "سجلت إنك بلّغت ببداية الدوا. التذكيرات لسه موقوفة حسب طلبك.",
        "Your start report is recorded. Reminders remain stopped as you requested.",
    ),
    "patient_start_review": (
        "سجلت إنك بلّغت ببداية الدوا. المتابعة دي لسه محتاجة مراجعة الدكتور.",
        "Your start report is recorded. This check-in still needs your doctor’s review.",
    ),
    "patient_start_choose": (
        "بدأت أنهي دوا من اللي الدكتور سجّلهم؟",
        "Which of the recorded medicines did you start?",
    ),
    "patient_start_missing": (
        "مفيش دوا جديد مسجل يطابق كلامك، هسأل الدكتور.",
        "No new medication start matches your report. Your question is in the doctor’s queue.",
    ),
    "patient_start_date": (
        "بدأت إمتى بالضبط؟ ابعت بدأت واسم الدوا ومعاهم النهارده أو امبارح، أو وضّح التاريخ للدكتور.",
        "When exactly did you start? Tell us today or yesterday, or clarify "
        "the date with your doctor.",
    ),
    "patient_day3_recorded": (
        "سجلت ردك على متابعة اليوم الثالث.",
        "Your day-three check-in response is recorded.",
    ),
    "patient_reading_recorded": (
        "سجلت القراءة اللي بلّغت بيها: {value}. ده تسجيل لكلامك، مش تفسير للنتيجة.",
        "Your reported reading is saved: {value}. This records your report; "
        "it does not interpret the result.",
    ),
    "patient_bp_incomplete": (
        "قراءة الضغط فيها رقمين. ابعت الرقم التاني ووحدة القياس اللي على الجهاز.",
        "Blood pressure needs two numbers. Please send the second number "
        "and the unit on your device.",
    ),
    "patient_reading_verify": (
        "القراءة محتاجة تأكيد، راجع الرقمين على الجهاز وابعتهم تاني.",
        "The reading needs verification. Please check both numbers on the "
        "device and send them again.",
    ),
    "patient_question_forwarded": (
        "وصّلت سؤالك للدكتور.",
        "Your question is in the doctor’s queue.",
    ),
    "patient_safe_fallback": (
        "مش قادر أجاوب على ده بدقة، وصّلت سؤالك للدكتور.",
        "I cannot answer this accurately. Your question is in the doctor’s queue.",
    ),
    "patient_unavailable": (
        "مش قادر أجاوب على ده بدقة دلوقتي. ابعت سؤالك في رسالة.",
        "I cannot answer accurately right now. Please send your question in a message.",
    ),
    "patient_callback_stale": (
        "الاختيار ده مبقاش متاح. ابعت طلبك في رسالة جديدة.",
        "That choice is no longer available. Please send a new message.",
    ),
}


def render(key: str, language: str = default_language, **fields: str) -> str:
    if key.startswith("patient_evidence_"):
        from sanad.evidence.templates import render as evidence_render

        return evidence_render(key, language, **fields)
    return TEMPLATES[key][1 if language == "en" else 0].format(**fields)
