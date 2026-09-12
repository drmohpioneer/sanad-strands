"""Accepted concierge wording; legacy fragments and punctuation are intentional."""

from sanad.presentation.catalog import Catalog, validate_catalog

CATALOG: Catalog = {
    "concierge.doctor_question_deferred": {
        "en": "The question is deferred and its review is re-armed.",
        "ar": "The question is deferred and its review is re-armed.",
    },
    "concierge.doctor_answer_reused": {
        "en": "The answer is saved for similar questions.",
        "ar": "The answer is saved for similar questions.",
    },
    "concierge.patient_photo_pending": {
        "ar": "الصورة محفوظة ومستنية مراجعة المحتوى، لسه مفيش نتيجة اتأكدت منها.",
        "en": "Your image is saved for content review; no result has been verified.",
    },
    "concierge.patient_voice_unreadable": {
        "ar": "مسمعتش كويس، ابعت تاني أو اكتب.",
        "en": "I could not hear clearly. Please send it again or type it.",
    },
    "concierge.patient_treatment_change_relay": {
        "ar": "ده قرار الدكتور، بعتّله سؤالك.",
        "en": "That decision belongs to your doctor. Your question is in the doctor’s queue.",
    },
    "concierge.patient_stop_ack": {
        "ar": "تمام، مش هبعتلك تذكيرات تاني. لو احتجت حاجة ابعتلي.",
        "en": "Reminders are stopped. You can still message me when you need help.",
    },
    "concierge.patient_snooze_ack": {
        "ar": "تمام، أجّلت التذكيرات لحد {until}.",
        "en": "Reminders are snoozed until {until}.",
    },
    "concierge.patient_snooze_clarify": {
        "ar": "تحب تأجّل كام ساعة أو كام يوم؟ أقصى مدة 7 أيام.",
        "en": "How many hours or days? The maximum is 7 days.",
    },
    "concierge.patient_resume_ask": {
        "ar": "تحب أرجع أبعتلك التذكيرات؟",
        "en": "Would you like me to send reminders again?",
    },
    "concierge.patient_resume_ack": {
        "ar": "تمام، وافقت إن التذكيرات ترجع.",
        "en": "You have agreed to receive reminders again.",
    },
    "concierge.patient_quiet_ack": {
        "ar": "سجلت ساعات الهدوء. المواعيد اللي الدكتور حددها زي ما هي.",
        "en": "Quiet hours are saved. Your doctor’s clinical times are unchanged.",
    },
    "concierge.patient_quiet_clarify": {
        "ar": "اكتب ساعات الهدوء كده: من 22:00 لحد 08:00.",
        "en": "Please write a quiet-hours window, such as 22:00 to 08:00.",
    },
    "concierge.patient_start_recorded": {
        "ar": "سجلت إنك بلّغت ببداية الدوا. المتابعة بعد 3 أيام من البداية، ومعادها المسجل:",
        "en": (
            "Your start report is recorded. The check-in is 3 days after the "
            "start; its recorded time:"
        ),
    },
    "concierge.patient_start_stopped": {
        "ar": "سجلت إنك بلّغت ببداية الدوا. التذكيرات لسه موقوفة حسب طلبك.",
        "en": "Your start report is recorded. Reminders remain stopped as you requested.",
    },
    "concierge.patient_start_review": {
        "ar": "سجلت إنك بلّغت ببداية الدوا. المتابعة دي لسه محتاجة مراجعة الدكتور.",
        "en": "Your start report is recorded. This check-in still needs your doctor’s review.",
    },
    "concierge.patient_start_choose": {
        "ar": "بدأت أنهي دوا من اللي الدكتور سجّلهم؟",
        "en": "Which of the recorded medicines did you start?",
    },
    "concierge.patient_start_missing": {
        "ar": "مفيش دوا جديد مسجل يطابق كلامك، هسأل الدكتور.",
        "en": (
            "No new medication start matches your report. Your question is in the doctor’s queue."
        ),
    },
    "concierge.patient_start_date": {
        "ar": (
            "بدأت إمتى بالضبط؟ ابعت بدأت واسم الدوا ومعاهم النهارده أو امبارح، أو "
            "وضّح التاريخ للدكتور."
        ),
        "en": (
            "When exactly did you start? Tell us today or yesterday, or clarify "
            "the date with your doctor."
        ),
    },
    "concierge.patient_day3_recorded": {
        "ar": "سجلت ردك على متابعة اليوم الثالث.",
        "en": "Your day-three check-in response is recorded.",
    },
    "concierge.patient_reading_recorded": {
        "ar": "سجلت القراءة اللي بلّغت بيها: {value}. ده تسجيل لكلامك، مش تفسير للنتيجة.",
        "en": (
            "Your reported reading is saved: {value}. This records your report; it "
            "does not interpret the result."
        ),
    },
    "concierge.patient_bp_incomplete": {
        "ar": "قراءة الضغط فيها رقمين. ابعت الرقم التاني ووحدة القياس اللي على الجهاز.",
        "en": (
            "Blood pressure needs two numbers. Please send the second number and "
            "the unit on your device."
        ),
    },
    "concierge.patient_reading_verify": {
        "ar": "القراءة محتاجة تأكيد، راجع الرقمين على الجهاز وابعتهم تاني.",
        "en": (
            "The reading needs verification. Please check both numbers on the "
            "device and send them again."
        ),
    },
    "concierge.patient_question_forwarded": {
        "ar": "وصّلت سؤالك للدكتور.",
        "en": "Your question is in the doctor’s queue.",
    },
    "concierge.patient_safe_fallback": {
        "ar": "مش قادر أجاوب على ده بدقة، وصّلت سؤالك للدكتور.",
        "en": "I cannot answer this accurately. Your question is in the doctor’s queue.",
    },
    "concierge.patient_unavailable": {
        "ar": "مش قادر أجاوب على ده بدقة دلوقتي. ابعت سؤالك في رسالة.",
        "en": "I cannot answer accurately right now. Please send your question in a message.",
    },
    "concierge.patient_callback_stale": {
        "ar": "الاختيار ده مبقاش متاح. ابعت طلبك في رسالة جديدة.",
        "en": "That choice is no longer available. Please send a new message.",
    },
    "concierge.patient_stop_recorded": {
        "ar": "سجلت إنك بلّغت بتنفيذ طلب الدكتور بإيقاف الدوا.",
        "en": "Your report of carrying out your doctor's medication stop is recorded.",
    },
    "concierge.patient_change_recorded": {
        "ar": "سجلت إنك بلّغت بتنفيذ تعديل الدوا اللي الدكتور طلبه.",
        "en": "Your report of carrying out your doctor's medication change is recorded.",
    },
    "concierge.patient_stop_choose": {
        "ar": "وقفت أنهي دوا من اللي الدكتور طلب إيقافهم؟",
        "en": "Which medicine did you stop from those your doctor asked you to stop?",
    },
    "concierge.patient_change_choose": {
        "ar": "عدّلت أنهي دوا من اللي الدكتور طلب تعديلهم؟",
        "en": "Which medicine did you change from those your doctor asked you to change?",
    },
    "concierge.patient_stop_missing": {
        "ar": "مفيش طلب إيقاف مسجل يطابق كلامك. وصّلت كلامك للدكتور.",
        "en": "No recorded medication stop matches your report. It is in your doctor's queue.",
    },
    "concierge.patient_change_missing": {
        "ar": "مفيش تعديل دوا مسجل يطابق كلامك. وصّلت كلامك للدكتور.",
        "en": ("No recorded medication change matches your report. It is in your doctor's queue."),
    },
    "concierge.patient_stop_start_recorded": {
        "ar": "سجلت إنك بلّغت بإيقاف الدوا القديم وبداية الدوا الجديد حسب خطة الدكتور.",
        "en": (
            "Your reports of stopping the old medicine and starting the new medicine are recorded."
        ),
    },
    "concierge.patient_start_unknown": {
        "ar": "سجلت بلاغ البداية؛ تاريخ البداية غير مؤكد، ومتابعة اليوم الثالث مستنية تحديده.",
        "en": (
            "Your start report is recorded; the start date is uncertain. The "
            "day-three check-in still needs its start date."
        ),
    },
    "concierge.patient_start_date_expired": {
        "ar": "مفيش سؤال بداية دوا ساري. ابعت اسم الدوا وقلّي بدأت إمتى.",
        "en": (
            "There is no current medication start-date question. Send the medicine "
            "name and when you started."
        ),
    },
    "concierge.patient_barrier_recorded": {
        "ar": "سجلت إن عندك عائق مع {drug}؛ هوصل ده للدكتور. لو الحالة اتغيرت ابعتلي.",
        "en": (
            "Your difficulty with {drug} is recorded for your doctor. Message me if things change."
        ),
    },
    "concierge.patient_barrier_choose": {
        "ar": "العائق ده مع أنهي دوا من اللي الدكتور طلب تبدأهم؟",
        "en": "Which medicine your doctor asked you to start does this difficulty concern?",
    },
    "concierge.patient_barrier_missing": {
        "ar": "مفيش طلب بداية دوا ساري يطابق العائق. ابعت اسم الدوا وطلب الدكتور.",
        "en": (
            "No current medication start matches this difficulty. Send the "
            "medicine name and your doctor's instruction."
        ),
    },
    "concierge.patient_barrier_mission_choose": {
        "ar": "العائق ده يخص أنهي طلب من طلبات الدكتور؟",
        "en": "Which of your doctor's requests does this difficulty concern?",
    },
    "concierge.patient_visit_choose": {
        "ar": "تقصد أنهي زيارة من اللي الدكتور طلبهم؟",
        "en": "Which of your doctor's requested visits do you mean?",
    },
    "concierge.patient_visit_missing": {
        "ar": "مفيش زيارة مسجلة تطابق كلامك. سؤالك في قائمة الدكتور.",
        "en": "No recorded visit matches your report. Your question is in the doctor's queue.",
    },
    "concierge.patient_visit_booked": {
        "ar": "سجلت إنك بلّغت بحجز الزيارة.",
        "en": "Your report that you booked the visit is recorded.",
    },
    "concierge.patient_visit_booked_wait": {
        "ar": "سجلت الحجز؛ ابعتلي لما تروح.",
        "en": "Your booking is recorded. Please tell me when you attend.",
    },
    "concierge.patient_visit_attended": {
        "ar": "سجلت إنك بلّغت بحضور الزيارة.",
        "en": "Your report that you attended the visit is recorded.",
    },
    "concierge.patient_visit_report_needed": {
        "ar": "تمام؛ ابعت صورة تقرير الزيارة لما تستلمه.",
        "en": "Please send a photo of the visit report when you receive it.",
    },
    "concierge.patient_visit_not_attended": {
        "ar": "سجلت إنك مقدرتش تروح. الزيارة لسه ما اكتملتش.",
        "en": ("Your report that you could not attend is recorded. The visit remains unfinished."),
    },
    "concierge.patient_visit_booking_needed": {
        "ar": "سجلت كلامك عن الحضور. المطلوب المسجل هو تأكيد الحجز؛ وضّح الحجز في رسالة.",
        "en": (
            "Your attendance report is recorded. This request needs a booking "
            "report; please confirm the booking in a message."
        ),
    },
    "concierge.patient_visit_date": {
        "ar": "تاريخ الحجز مش واضح. ابعت تاريخ واحد بصيغة سنة-شهر-يوم.",
        "en": "The booking date is unclear. Please send one date as YYYY-MM-DD.",
    },
    "concierge.patient_visit_late_booking": {
        "ar": (
            "سجلت الحجز، لكن تاريخه بعد الميعاد المطلوب. الميعاد الأصلي زي ما هو "
            "ومستني قرار الدكتور."
        ),
        "en": (
            "Your booking is recorded, but its date is after the deadline. The "
            "original deadline remains for your doctor's decision."
        ),
    },
    "concierge.patient_task_choose": {
        "ar": "خلصت أنهي طلب من اللي الدكتور سجّلهم؟",
        "en": "Which of your doctor's recorded tasks did you finish?",
    },
    "concierge.patient_task_missing": {
        "ar": "مفيش طلب مسجل يطابق كلامك. سؤالك في قائمة الدكتور.",
        "en": "No recorded task matches your report. Your question is in the doctor's queue.",
    },
    "concierge.patient_task_recorded": {
        "ar": "سجلت إنك بلّغت بإتمام المطلوب؛ لسه مستني قبول الدكتور.",
        "en": "Your completion report is recorded; it is awaiting your doctor's acceptance.",
    },
    "concierge.patient_task_reopened": {
        "ar": ("الدكتور محتاج المطلوب يتعمل بشكل أوضح: {instruction}. الميعاد الجديد {due_local}."),
        "en": (
            "Your doctor needs the task completed more clearly: {instruction}. The "
            "new deadline is {due_local}."
        ),
    },
    "concierge.patient_question_answered": {
        "ar": "رد الدكتور على سؤالك: {answer}",
        "en": "Your doctor's answer to your question: {answer}",
    },
    "concierge.patient_question_closed": {
        "ar": "الدكتور قفل السؤال ده؛ اسأله في الزيارة.",
        "en": "Your doctor closed this question. Please ask about it at your visit.",
    },
    "concierge.patient_question_plan_pending": {
        "ar": "الدكتور شاف سؤالك وهيعدّل الخطة؛ التعديل هيوصلك لما يتأكد.",
        "en": (
            "Your doctor has reviewed your question and needs to update the plan. "
            "The change will follow once confirmed."
        ),
    },
    "concierge.patient_question_plan_updated": {
        "ar": 'الدكتور رد بتعديل خطتك. التعديل موجود في الخطة؛ ابعت "الخطة" علشان تشوفها.',
        "en": (
            "Your doctor answered by updating your plan. The change is in your "
            'plan; send "plan" to see it.'
        ),
    },
    "concierge.doctor_question_recorded": {
        "ar": "الرد متسجل وفي انتظار الإرسال للمريض.",
        "en": "The answer is recorded and queued for the patient.",
    },
    "concierge.doctor_question_delivery_pending": {
        "ar": "الرد متسجل ولسه موصلش للمريض.",
        "en": "The answer is recorded but has not reached the patient.",
    },
    "concierge.doctor_question_held": {
        "ar": "الرد ده بيغير العلاج؛ سجّل التعديل بالإملاء عشان يتبعت.",
        "en": ("This answer changes treatment. Dictate and confirm the plan amendment to send it."),
    },
    "concierge.doctor_question_refused": {
        "ar": "الرد ماتبعتش: {reason}.",
        "en": "The answer was not sent: {reason}.",
    },
    "concierge.doctor_question_list_stale": {
        "ar": "القائمة مش متاحة أو انتهت. ابعت /questions من جديد.",
        "en": "The listing is unavailable or expired. Run /questions again.",
    },
    "concierge.doctor_questions_empty": {
        "ar": "مفيش أسئلة مفتوحة في القائمة دي.",
        "en": "There are no open questions on this page.",
    },
    "concierge.doctor_questions_usage": {
        "ar": ("استخدم /questions أو /questions <page>، وبعدها /answer <n> <text> أو /close <n>."),
        "en": "Use /questions or /questions <page>, then /answer <n> <text> or /close <n>.",
    },
    "concierge.doctor_task_accepted": {
        "ar": "سجلت قبولك لبلاغ إتمام المطلوب.",
        "en": "Your acceptance of the task completion report is recorded.",
    },
    "concierge.doctor_task_reopened": {
        "ar": "فتحت المطلوب من جديد وسجلت الميعاد الجديد.",
        "en": "The task is reopened with its new deadline.",
    },
    "concierge.doctor_task_stale": {
        "ar": "الاختيار ده اتستخدم أو مبقاش صالح.",
        "en": "This choice has already been used or is no longer valid.",
    },
    "concierge.doctor_questions_page": {
        "ar": "الأسئلة: صفحة {page}/{pages}",
        "en": "Questions: page {page}/{pages}",
    },
    "concierge.doctor_questions_no_plan": {
        "ar": "مفيش أمر دوا نشط.",
        "en": "No active medication order.",
    },
    "concierge.doctor_questions_age": {
        "ar": "{hours} ساعة",
        "en": "{hours}h",
    },
    "concierge.doctor_questions_held": {
        "ar": "رد محفوظ فقط: {answer}",
        "en": "Held (record only): {answer}",
    },
    "concierge.question_title": {
        "ar": "سؤال للمراجعة",
        "en": "Question for review",
    },
}

FIELDS = validate_catalog("concierge", CATALOG)
