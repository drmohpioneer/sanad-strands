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
    "patient_stop_recorded": (
        "سجلت إنك بلّغت بتنفيذ طلب الدكتور بإيقاف الدوا.",
        "Your report of carrying out your doctor's medication stop is recorded.",
    ),
    "patient_change_recorded": (
        "سجلت إنك بلّغت بتنفيذ تعديل الدوا اللي الدكتور طلبه.",
        "Your report of carrying out your doctor's medication change is recorded.",
    ),
    "patient_stop_choose": (
        "وقفت أنهي دوا من اللي الدكتور طلب إيقافهم؟",
        "Which medicine did you stop from those your doctor asked you to stop?",
    ),
    "patient_change_choose": (
        "عدّلت أنهي دوا من اللي الدكتور طلب تعديلهم؟",
        "Which medicine did you change from those your doctor asked you to change?",
    ),
    "patient_stop_missing": (
        "مفيش طلب إيقاف مسجل يطابق كلامك. وصّلت كلامك للدكتور.",
        "No recorded medication stop matches your report. It is in your doctor's queue.",
    ),
    "patient_change_missing": (
        "مفيش تعديل دوا مسجل يطابق كلامك. وصّلت كلامك للدكتور.",
        "No recorded medication change matches your report. It is in your doctor's queue.",
    ),
    "patient_stop_start_recorded": (
        "سجلت إنك بلّغت بإيقاف الدوا القديم وبداية الدوا الجديد حسب خطة الدكتور.",
        "Your reports of stopping the old medicine and starting the new medicine are recorded.",
    ),
    "patient_start_unknown": (
        "سجلت بلاغ البداية؛ تاريخ البداية غير مؤكد، ومتابعة اليوم الثالث مستنية تحديده.",
        "Your start report is recorded; the start date is uncertain. "
        "The day-three check-in still needs its start date.",
    ),
    "patient_start_date_expired": (
        "مفيش سؤال بداية دوا ساري. ابعت اسم الدوا وقلّي بدأت إمتى.",
        "There is no current medication start-date question. "
        "Send the medicine name and when you started.",
    ),
    "patient_barrier_recorded": (
        "سجلت إن عندك عائق مع {drug}؛ هوصل ده للدكتور. لو الحالة اتغيرت ابعتلي.",
        "Your difficulty with {drug} is recorded for your doctor. Message me if things change.",
    ),
    "patient_barrier_choose": (
        "العائق ده مع أنهي دوا من اللي الدكتور طلب تبدأهم؟",
        "Which medicine your doctor asked you to start does this difficulty concern?",
    ),
    "patient_barrier_missing": (
        "مفيش طلب بداية دوا ساري يطابق العائق. ابعت اسم الدوا وطلب الدكتور.",
        "No current medication start matches this difficulty. "
        "Send the medicine name and your doctor's instruction.",
    ),
    "patient_barrier_mission_choose": (
        "العائق ده يخص أنهي طلب من طلبات الدكتور؟",
        "Which of your doctor's requests does this difficulty concern?",
    ),
    "patient_visit_choose": (
        "تقصد أنهي زيارة من اللي الدكتور طلبهم؟",
        "Which of your doctor's requested visits do you mean?",
    ),
    "patient_visit_missing": (
        "مفيش زيارة مسجلة تطابق كلامك. سؤالك في قائمة الدكتور.",
        "No recorded visit matches your report. Your question is in the doctor's queue.",
    ),
    "patient_visit_booked": (
        "سجلت إنك بلّغت بحجز الزيارة.",
        "Your report that you booked the visit is recorded.",
    ),
    "patient_visit_booked_wait": (
        "سجلت الحجز؛ ابعتلي لما تروح.",
        "Your booking is recorded. Please tell me when you attend.",
    ),
    "patient_visit_attended": (
        "سجلت إنك بلّغت بحضور الزيارة.",
        "Your report that you attended the visit is recorded.",
    ),
    "patient_visit_report_needed": (
        "تمام؛ ابعت صورة تقرير الزيارة لما تستلمه.",
        "Please send a photo of the visit report when you receive it.",
    ),
    "patient_visit_not_attended": (
        "سجلت إنك مقدرتش تروح. الزيارة لسه ما اكتملتش.",
        "Your report that you could not attend is recorded. The visit remains unfinished.",
    ),
    "patient_visit_booking_needed": (
        "سجلت كلامك عن الحضور. المطلوب المسجل هو تأكيد الحجز؛ وضّح الحجز في رسالة.",
        "Your attendance report is recorded. This request needs a booking report; "
        "please confirm the booking in a message.",
    ),
    "patient_visit_date": (
        "تاريخ الحجز مش واضح. ابعت تاريخ واحد بصيغة سنة-شهر-يوم.",
        "The booking date is unclear. Please send one date as YYYY-MM-DD.",
    ),
    "patient_visit_late_booking": (
        "سجلت الحجز، لكن تاريخه بعد الميعاد المطلوب. الميعاد الأصلي زي ما هو ومستني قرار الدكتور.",
        "Your booking is recorded, but its date is after the deadline. "
        "The original deadline remains for your doctor's decision.",
    ),
    "patient_task_choose": (
        "خلصت أنهي طلب من اللي الدكتور سجّلهم؟",
        "Which of your doctor's recorded tasks did you finish?",
    ),
    "patient_task_missing": (
        "مفيش طلب مسجل يطابق كلامك. سؤالك في قائمة الدكتور.",
        "No recorded task matches your report. Your question is in the doctor's queue.",
    ),
    "patient_task_recorded": (
        "سجلت إنك بلّغت بإتمام المطلوب؛ لسه مستني قبول الدكتور.",
        "Your completion report is recorded; it is awaiting your doctor's acceptance.",
    ),
    "patient_task_reopened": (
        "الدكتور محتاج المطلوب يتعمل بشكل أوضح: {instruction}. الميعاد الجديد {due_local}.",
        "Your doctor needs the task completed more clearly: {instruction}. "
        "The new deadline is {due_local}.",
    ),
    "patient_question_answered": (
        "رد الدكتور على سؤالك: {answer}",
        "Your doctor's answer to your question: {answer}",
    ),
    "patient_question_closed": (
        "الدكتور قفل السؤال ده؛ اسأله في الزيارة.",
        "Your doctor closed this question. Please ask about it at your visit.",
    ),
    "patient_question_plan_pending": (
        "الدكتور شاف سؤالك وهيعدّل الخطة؛ التعديل هيوصلك لما يتأكد.",
        "Your doctor has reviewed your question and needs to update the plan. "
        "The change will follow once confirmed.",
    ),
    "patient_question_plan_updated": (
        'الدكتور رد بتعديل خطتك. التعديل موجود في الخطة؛ ابعت "الخطة" علشان تشوفها.',
        "Your doctor answered by updating your plan. The change is in your plan; "
        'send "plan" to see it.',
    ),
    "doctor_question_recorded": (
        "الرد متسجل وفي انتظار الإرسال للمريض.",
        "The answer is recorded and queued for the patient.",
    ),
    "doctor_question_delivery_pending": (
        "الرد متسجل ولسه موصلش للمريض.",
        "The answer is recorded but has not reached the patient.",
    ),
    "doctor_question_held": (
        "الرد ده بيغير العلاج؛ سجّل التعديل بالإملاء عشان يتبعت.",
        "This answer changes treatment. Dictate and confirm the plan amendment to send it.",
    ),
    "doctor_question_refused": (
        "الرد ماتبعتش: {reason}.",
        "The answer was not sent: {reason}.",
    ),
    "doctor_question_list_stale": (
        "القائمة مش متاحة أو انتهت. ابعت /questions من جديد.",
        "The listing is unavailable or expired. Run /questions again.",
    ),
    "doctor_questions_empty": (
        "مفيش أسئلة مفتوحة في القائمة دي.",
        "There are no open questions on this page.",
    ),
    "doctor_questions_usage": (
        "استخدم /questions أو /questions <page>، وبعدها /answer <n> <text> أو /close <n>.",
        "Use /questions or /questions <page>, then /answer <n> <text> or /close <n>.",
    ),
    "doctor_task_accepted": (
        "سجلت قبولك لبلاغ إتمام المطلوب.",
        "Your acceptance of the task completion report is recorded.",
    ),
    "doctor_task_reopened": (
        "فتحت المطلوب من جديد وسجلت الميعاد الجديد.",
        "The task is reopened with its new deadline.",
    ),
    "doctor_task_stale": (
        "الاختيار ده اتستخدم أو مبقاش صالح.",
        "This choice has already been used or is no longer valid.",
    ),
    "doctor_questions_page": (
        "الأسئلة — صفحة {page}/{pages}",
        "Questions — page {page}/{pages}",
    ),
    "doctor_questions_no_plan": (
        "مفيش أمر دوا نشط.",
        "No active medication order.",
    ),
    "doctor_questions_age": (
        "{hours} ساعة",
        "{hours}h",
    ),
    "doctor_questions_held": (
        "رد محفوظ فقط: {answer}",
        "Held (record only): {answer}",
    ),
    "question_title": (
        "سؤال للمراجعة",
        "Question for review",
    ),
}


def render(key: str, language: str = default_language, **fields: str) -> str:
    if key.startswith("patient_evidence_"):
        from sanad.evidence.templates import render as evidence_render

        return evidence_render(key, language, **fields)
    return TEMPLATES[key][1 if language == "en" else 0].format(**fields)
