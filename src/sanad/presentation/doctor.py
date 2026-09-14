"""Accepted doctor wording; legacy fragments and punctuation are intentional."""

from sanad.presentation.catalog import Catalog, validate_catalog

CATALOG: Catalog = {
    "doctor.dashboard_signed_out": {
        "ar": "تم تسجيل الخروج من لوحة المتابعة.",
        "en": "Signed out of the dashboard.",
    },
    "doctor.account_suspended": {
        "ar": "حسابك موقوف. تواصل مع الإدارة.",
        "en": "Your account is suspended. Contact the administrator.",
    },
    "doctor.patient_not_linked": {
        "ar": "لسه مش مرتبط بدكتور. افتح رابط الدعوة اللي بعته الدكتور.",
        "en": "You are not linked to a doctor yet. Open the invitation link your doctor sent you.",
    },
    "doctor.login_refused": {
        "ar": "تعذر الدخول. تواصل مع الإدارة.",
        "en": "Sign-in was refused. Contact the administrator.",
    },
    "doctor.scribe_digest": {
        "en": (
            "Patient questions are collected and sent to you once a day at {time} Cairo "
            "(packing: {packing}). {waiting}"
        ),
        "ar": (
            "أسئلة المرضى بتتجمع وبتتبعتلك مرة في اليوم الساعة {time} بتوقيت القاهرة "
            "(التجميع: {packing}). {waiting}"
        ),
    },
    "doctor.scribe_digest_usage": {
        "en": "Use /digest to see your setting. Set a time with "
        "/digest 20:00, choose /digest one or /digest each, or "
        "combine them with /digest 21:30 each.",
        "ar": "استخدم /digest لعرض الإعداد. غيّر الوقت باستخدام "
        "/digest 20:00، أو التجميع باستخدام /digest one أو "
        "/digest each، أو الاتنين باستخدام /digest 21:30 each.",
    },
    "doctor.doctor_question_digest": {
        "en": "These questions are waiting for your answer.\n\n{lines}",
        "ar": "الأسئلة دي مستنية إجابتك.\n\n{lines}",
    },
    "doctor.application_received": {
        "ar": "طلب التسجيل كدكتور وصل، ومستني مراجعة الإدارة.",
        "en": "Your doctor registration application was received and is awaiting admin review.",
    },
    "doctor.admin_new_application": {
        "ar": "طلب تسجيل دكتور جديد. البيانات دي مقدمة من صاحب "
        "الطلب ولسه مش متحققة:\n"
        "الاسم: {name}\n"
        "التخصص: {specialty}\n"
        "المدينة: {city}",
        "en": "New doctor application. These applicant-provided "
        "details are unverified:\n"
        "Name: {name}\n"
        "Specialty: {specialty}\n"
        "City: {city}",
    },
    "doctor.doctor_approved": {
        "ar": "تمت الموافقة على حسابك كدكتور في سند. {name}",
        "en": "Your doctor account in Sanad has been approved. {name}",
    },
    "doctor.application_rejected": {
        "ar": "طلب التسجيل مش مقبول حاليًا.",
        "en": "Your registration application is not approved at this time.",
    },
    "doctor.doctor_welcome_back": {
        "ar": "أهلًا برجوعك. اكتب أو سجّل اللي عايز تعمله للمريض "
        "بكلامك، أو ابعت صورة الروشتة. اكتب /help لعرض الأوامر.",
        "en": "Welcome back. Write or record what you want to do for "
        "a patient, in your own words. You can also send a "
        "prescription photo. Type /help for the commands.",
    },
    "doctor.doctor_capability_pending": {
        "ar": "رسالتك وصلت. التعامل مع خطط المرضى والملفات مش متاح هنا لسه.",
        "en": "Your message was received. Patient plans and files cannot be handled here yet.",
    },
    "doctor.callback_refused": {
        "ar": "الإجراء ده مش متاح من الزر ده.",
        "en": "This action is unavailable from this button.",
    },
    "doctor.doctor_suspended_notice": {
        "ar": "تم تعليق صلاحيات حسابك كدكتور في سند.",
        "en": "Your doctor account permissions in Sanad have been suspended.",
    },
    "doctor.admin_login_link": {
        "ar": "رابط دخول الإدارة صالح لعشر دقائق ولمرة واحدة.\n{link}",
        "en": "Administrator sign-in: valid for ten minutes and one use. Press Continue.\n{link}",
    },
    "doctor.admin_no_sessions": {
        "ar": "لا توجد جلسات إدارة.",
        "en": "There are no administrator sessions.",
    },
    "doctor.doctor_login_link": {
        "ar": "لينك دخولك لسند صالح لعشر دقايق ولمرة واحدة. انسخه والصقه في المتصفح، "
        "ومتدوسش عليه جوه تيليجرام.\n{link}",
        "en": "Your one-time sign-in link, valid for ten minutes. Copy it and paste it "
        "into your browser. Do not tap it inside Telegram.\n"
        "{link}",
    },
    "doctor.consent_request": {
        "ar": "سند مساعد بالذكاء الاصطناعي لمتابعة تعليمات د. {doctor}. "
        "الربط محتاج موافقتك وتأكيد الدكتور إنك الشخص المقصود.\n"
        "هنعالج رسائلك وصوتك وصورك وبيانات حسابك وخطة الدكتور للشرح "
        "والتذكير وجمع المتابعة. المعالجة تشمل مقدم قناة تيليجرام "
        "ومقدمي الاستضافة السحابية ونماذج الذكاء الاصطناعي. رسائل "
        "البوت مش محادثة طبية مشفرة من الطرف للطرف.\n"
        "المتابعة الروتينية بحد أقصى رسالة متابعة واحدة في اليوم، "
        "خارج ساعات الهدوء {quiet_start}, {quiet_end} بتوقيت "
        "{timezone}. أي تذكير بمواعيد محددة أو أثناء الهدوء محتاج "
        "موافقة منفصلة.\n"
        "سند ممكن يغلط، ومش بيشخص أو بيكتب علاج أو بيغير تعليمات "
        "الدكتور. مش خدمة طوارئ ومفيش وعد بوقت رد الدكتور. تقدر "
        "ترفض دلوقتي أو تتواصل مع العيادة لوقف التواصل الروتيني "
        "وسحب الموافقة. ده مش بيغير العلاج.\n"
        "الاحتفاظ بالبيانات: {retention}\n"
        "التواصل مع العيادة: {clinic_contact}",
        "en": "Sanad is an AI assistant following Dr {doctor}'s "
        "instructions. Linking requires your consent and the "
        "doctor's confirmation of your identity.\n"
        "We process your messages, voice, images, account details "
        "and doctor's plan for explanations, reminders and "
        "follow-up collection. Processing involves the Telegram "
        "channel provider, cloud hosting providers and AI model "
        "providers. Bot messages are not end-to-end encrypted "
        "clinical communication.\n"
        "Routine follow-up is limited to one chase message per day, "
        "outside quiet hours {quiet_start}, {quiet_end} in "
        "{timezone}. Scheduled reminders and quiet-hour exceptions "
        "require separate consent.\n"
        "Sanad can make mistakes; it does not diagnose, prescribe "
        "or change the doctor's instructions. It is not an "
        "emergency service and does not promise a doctor response "
        "time. You may decline now or contact the clinic to stop "
        "routine contact and withdraw consent. This does not change "
        "treatment.\n"
        "Data retention: {retention}\n"
        "Clinic contact: {clinic_contact}",
    },
    "doctor.consent_recorded_wait_doctor": {
        "ar": "موافقتك اتسجلت. مستنيين الدكتور يؤكد إنك الشخص المقصود قبل تفعيل الربط.",
        "en": "Your consent was recorded. The doctor must "
        "confirm your identity before linking is "
        "activated.",
    },
    "doctor.consent_declined_ack": {
        "ar": "تم تسجيل عدم الموافقة. الربط مش هيتفعل.",
        "en": "Your decline was recorded. Linking will not be activated.",
    },
    "doctor.claim_refused": {
        "ar": "مش ممكن نكمل الربط من الدعوة دي. تواصل مع العيادة.",
        "en": "This invitation cannot complete linking. Contact the clinic.",
    },
    "doctor.claim_awaiting_doctor": {
        "ar": "صاحب حساب تيليجرام {claimant} وافق على الربط. تأكد "
        "من هويته من المقابلة أو وسيلة تواصل موثوقة قبل "
        "التأكيد.",
        "en": "Telegram account {claimant} consented to linking. "
        "Verify this is the intended patient through the "
        "encounter or an established contact method before "
        "confirming.",
    },
    "doctor.claim_declined_doctor": {
        "ar": "صاحب طلب الربط رفض الموافقة. الربط لم يتفعل؛ يمكنك إصدار دعوة جديدة.",
        "en": "The claimant declined consent. Linking was not "
        "activated; you may issue a new invitation.",
    },
    "doctor.claim_rejected": {
        "ar": "طلب الربط لم يتم تأكيده. تواصل مع العيادة للحصول على دعوة جديدة.",
        "en": "The linking request was not confirmed. Contact the clinic for a new invitation.",
    },
    "doctor.binding_confirmed": {
        "ar": "الدكتور أكد هويتك وتم تفعيل ربط حسابك بسند بناءً على موافقتك.",
        "en": "The doctor confirmed your identity. Your Sanad account "
        "link is active with your consent.",
    },
    "doctor.invitation_expired_doctor": {
        "ar": "انتهت صلاحية دعوة الربط بدون تفعيل. يمكنك إصدار دعوة جديدة؛ مواعيد الخطة لم تتغير.",
        "en": "The linking invitation expired without "
        "activation. You may issue a new invitation; plan "
        "deadlines are unchanged.",
    },
    "doctor.patient_login_link": {
        "ar": "لينك دخولك لسند صالح لعشر دقايق ولمرة واحدة. انسخه والصقه في المتصفح، "
        "ومتدوسش عليه جوه تيليجرام.\n{link}",
        "en": "Your one-time sign-in link, valid for ten minutes. Copy it and paste it "
        "into your browser. Do not tap it inside Telegram.\n"
        "{link}",
    },
    "doctor.doctor_photo_unreadable": {
        "ar": "مش قادر أقرا الصورة: {reason}. صوّر من فوق في نور كويس وابعتها تاني.",
        "en": "I could not read the image: {reason}. Photograph "
        "it from above in good light and resend it.",
    },
    "doctor.scribe_intake_pending": {
        "ar": "الصورة محفوظة عندك. اختار المريض، أو مريض جديد، أو مش دلوقتي.",
        "en": "Your image is saved. Choose a patient, New patient, or Not now.",
    },
    "doctor.scribe_amendment_line": {"ar": "{drug}: {old} ← {new}", "en": "{drug}: {old} → {new}"},
    "doctor.scribe_brand_change_line": {
        "ar": "{old_drug} {old} ← {new_drug} {new}",
        "en": "{old_drug} {old} → {new_drug} {new}",
    },
    "doctor.scribe_card": {"ar": "{body}", "en": "{body}"},
    "doctor.scribe_confirmed": {"ar": "اتسجل:\n{body}", "en": "Recorded:\n{body}"},
    "doctor.monitor_schedule_changed": {
        "ar": "مواعيد القياس اتغيرت. ابعت التعليمات من جديد.",
        "en": "The reading times changed. Please send the instruction again.",
    },
    "doctor.scribe_stale": {
        "ar": "الكارت اتغير أو مبقاش صالح. ابعت التعليمات من جديد.",
        "en": "The card changed or is no longer valid. Send the instructions again.",
    },
    "doctor.scribe_discarded": {
        "ar": "تمام، لغيت الكارت ومفيش تعليمات اتسجلت.",
        "en": "Card cancelled. No instructions were recorded.",
    },
    "doctor.scribe_expired": {
        "ar": "صلاحية الكارت انتهت. ابعت التعليمات من جديد.",
        "en": "The card expired. Send the instructions again.",
    },
    "doctor.scribe_edit": {
        "ar": "ابعت التعديل كتابة أو بصوتك؛ الكارت القديم مش هيتأكد.",
        "en": "Send your correction by text or voice. The previous card cannot be confirmed.",
    },
    "doctor.scribe_invitation": {
        "ar": "افتح اللينك أو امسح الكود، وبعدها وافق على الربط واستنى "
        "تأكيد الدكتور.\n"
        "صالح 24 ساعة\n"
        "{link}\nأي /qr جديد بيلغي اللينك ده.",
        "en": "Open the link or scan the code, consent to linking, and "
        "wait for the doctor's confirmation.\n"
        "Valid for 24 hours\n"
        "{link}\nA new /qr replaces this link.",
    },
    "doctor.doctor_voice_unreadable": {
        "ar": "مش قادر أسمع التسجيل. ابعته تاني أو اكتب الكلام.",
        "en": "I could not hear the recording. Send it again or type the instructions.",
    },
    "doctor.doctor_model_unavailable": {
        "ar": "مش قادر أقرأ دلوقتي، ابعت تاني بعد شوية",
        "en": "I cannot read this right now. Please try again shortly.",
    },
    "doctor.doctor_patient_not_found": {
        "ar": "ملقيتش المريض ده عندك. اكتب /new وبعدها الاسم لو مريض جديد.",
        "en": "I could not find this patient. Use /new followed by the name for a new patient.",
    },
    "doctor.doctor_help": {
        "ar": (
            "أوامر سند:\n"
            "/start: ترحيب\n"
            "/help: المساعدة\n"
            "/new الاسم: مريض جديد\n"
            "/find الاسم: بحث\n"
            "/qr الاسم: دعوة ربط\n"
            "/cancel: إلغاء الكارت\n"
            "ابعت التعليمات كتابة أو بصوتك، وراجع الكارت قبل ✅ تمام."
        ),
        "en": (
            "Sanad commands:\n"
            "/start: welcome\n"
            "/help: help\n"
            "/new name: new patient\n"
            "/find name: search\n"
            "/qr name: linking invitation\n"
            "/cancel: cancel card\n"
            "/intake: saved images\n"
            "/lang en | ar: language\n"
            "/login: sign in\n"
            "/logout: sign out\n"
            "/digest: question digest settings\n"
            "/questions: list questions\n"
            "/answer N text: answer and queue to patient\n"
            "/send N: send a proposed reply\n"
            "/reuse: save a reusable answer\n"
            "/inbox: unresolved reviews\n"
            "/corrections: correct a record\n"
            "/name: set your display name\n"
            "Send instructions by text or voice and review the card before tapping ✅ Confirm.\n"
            "Contest mode is English; Arabic is a declared upgrade."
        ),
    },
    "doctor.doctor_document_too_many_pages": {
        "ar": "ابعت لحد ١٠ صفحات لو سمحت، الصفحات اللي فيها النتائج بس.",
        "en": "Please send up to 10 pages: just the pages with the results.",
    },
    "doctor.doctor_document_too_large": {
        "ar": "حجم الملف كبير. ابعت ملف أصغر أو صور لصفحات النتائج لو سمحت.",
        "en": "This file is too large. Please send a smaller file or photos of the result pages.",
    },
    "doctor.doctor_document_encrypted": {
        "ar": "الملف محمي بكلمة سر. ابعته من غير كلمة سر أو على شكل صور لو سمحت.",
        "en": "This file is password protected. Please send it without a password or as photos.",
    },
    "doctor.doctor_document_invalid": {
        "ar": "مش قادرين نفتح الملف. ابعته تاني أو على شكل صور لو سمحت.",
        "en": "This file could not be opened. Please send it again or as photos.",
    },
    "doctor.doctor_document_unreadable": {
        "ar": "مش قادرين نقرا الملف. ابعت صور لصفحات النتائج لو سمحت.",
        "en": "This file could not be read. Please send photos of the result pages.",
    },
    "doctor.doctor_document_blank": {
        "ar": "الملف باين فاضي. ابعت الصفحات اللي فيها النتائج لو سمحت.",
        "en": "This file looks empty. Please send the pages with the results.",
    },
    "doctor.doctor_document_too_detailed": {
        "ar": "المستند فيه نتائج كتير مش هنعرف نعرضها كلها. راجع الصفحات لو سمحت.",
        "en": "This document has too many results to list. Please review the pages.",
    },
    "doctor.doctor_document_word_unsupported": {
        "ar": "لسه مش بنقرا ملفات Word. ابعته PDF أو صور لصفحات النتائج لو سمحت.",
        "en": (
            "Word files can't be read yet. Please send it as a PDF "
            "or as photos of the result pages."
        ),
    },
}

FIELDS = validate_catalog("doctor", CATALOG)
