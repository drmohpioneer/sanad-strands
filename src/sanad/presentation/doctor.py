"""Accepted doctor wording; legacy fragments and punctuation are intentional."""

from sanad.presentation.catalog import Catalog, validate_catalog

CATALOG: Catalog = {
    "doctor.application_received": {
        "ar": (
            "طلب التسجيل كدكتور وصل، ومستني مراجعة الإدارة.\nYour doctor "
            "registration application was received and is awaiting admin review."
        ),
        "en": "Your doctor registration application was received and is awaiting admin review.",
    },
    "doctor.admin_new_application": {
        "ar": (
            "طلب تسجيل دكتور جديد. البيانات دي مقدمة من صاحب الطلب ولسه مش متحققة:\n"
            "الاسم: {name}\nالتخصص: {specialty}\nالمدينة: {city}\nNew doctor "
            "application. These applicant-provided details are unverified:\nName: "
            "{name}\nSpecialty: {specialty}\nCity: {city}"
        ),
        "en": (
            "New doctor application. These applicant-provided details are "
            "unverified:\nName: {name}\nSpecialty: {specialty}\nCity: {city}"
        ),
    },
    "doctor.doctor_approved": {
        "ar": (
            "تمت الموافقة على حسابك كدكتور في سند.\nYour doctor account in Sanad has been approved."
        ),
        "en": "Your doctor account in Sanad has been approved.",
    },
    "doctor.application_rejected": {
        "ar": (
            "طلب التسجيل مش مقبول حاليًا.\nYour registration application is not "
            "approved at this time."
        ),
        "en": "Your registration application is not approved at this time.",
    },
    "doctor.doctor_welcome_back": {
        "ar": (
            "أهلًا برجوعك. حسابك كدكتور في سند معتمد.\nWelcome back. Your doctor "
            "account in Sanad is approved."
        ),
        "en": "Welcome back. Your doctor account in Sanad is approved.",
    },
    "doctor.doctor_capability_pending": {
        "ar": (
            "رسالتك وصلت. التعامل مع خطط المرضى والملفات مش متاح هنا لسه.\nYour "
            "message was received. Patient plans and files cannot be handled here "
            "yet."
        ),
        "en": "Your message was received. Patient plans and files cannot be handled here yet.",
    },
    "doctor.callback_refused": {
        "ar": "الإجراء ده مش متاح من الزر ده.\nThis action is unavailable from this button.",
        "en": "This action is unavailable from this button.",
    },
    "doctor.doctor_suspended_notice": {
        "ar": (
            "تم تعليق صلاحيات حسابك كدكتور في سند.\nYour doctor account permissions "
            "in Sanad have been suspended."
        ),
        "en": "Your doctor account permissions in Sanad have been suspended.",
    },
    "doctor.doctor_login_link": {
        "ar": (
            "رابط دخولك لسند صالح لمدة عشر دقايق ولمرة واحدة. اضغط متابعة لإكمال "
            "الدخول.\nYour Sanad login link is valid for ten minutes and one use. "
            "Press Continue to sign in.\n{link}"
        ),
        "en": (
            "رابط دخولك لسند صالح لمدة عشر دقايق ولمرة واحدة. اضغط متابعة لإكمال "
            "الدخول.\nYour Sanad login link is valid for ten minutes and one use. "
            "Press Continue to sign in.\n{link}"
        ),
    },
    "doctor.consent_request": {
        "ar": (
            "سند مساعد بالذكاء الاصطناعي لمتابعة تعليمات د. {doctor}. الربط محتاج "
            "موافقتك وتأكيد الدكتور إنك الشخص المقصود.\nهنعالج رسائلك وصوتك وصورك "
            "وبيانات حسابك وخطة الدكتور للشرح والتذكير وجمع المتابعة. المعالجة "
            "تشمل مقدم قناة تيليجرام ومقدمي الاستضافة السحابية ونماذج الذكاء "
            "الاصطناعي. رسائل البوت مش محادثة طبية مشفرة من الطرف للطرف.\nالمتابعة "
            "الروتينية بحد أقصى رسالة متابعة واحدة في اليوم، خارج ساعات الهدوء "
            "{quiet_start}–{quiet_end} بتوقيت {timezone}. أي تذكير بمواعيد محددة "
            "أو أثناء الهدوء محتاج موافقة منفصلة.\nسند ممكن يغلط، ومش بيشخص أو "
            "بيكتب علاج أو بيغير تعليمات الدكتور. مش خدمة طوارئ ومفيش وعد بوقت رد "
            "الدكتور. تقدر ترفض دلوقتي أو تتواصل مع العيادة لوقف التواصل الروتيني "
            "وسحب الموافقة. ده مش بيغير العلاج.\nالاحتفاظ بالبيانات: {retention}\n"
            "التواصل مع العيادة: {clinic_contact}\nSanad is an AI assistant "
            "following Dr {doctor}'s instructions. Linking requires your consent "
            "and the doctor's confirmation of your identity.\nWe process your "
            "messages, voice, images, account details and doctor's plan for "
            "explanations, reminders and follow-up collection. Processing involves "
            "the Telegram channel provider, cloud hosting providers and AI model "
            "providers. Bot messages are not end-to-end encrypted clinical "
            "communication.\nRoutine follow-up is limited to one chase message per "
            "day, outside quiet hours {quiet_start}–{quiet_end} in {timezone}. "
            "Scheduled reminders and quiet-hour exceptions require separate "
            "consent.\nSanad can make mistakes; it does not diagnose, prescribe or "
            "change the doctor's instructions. It is not an emergency service and "
            "does not promise a doctor response time. You may decline now or "
            "contact the clinic to stop routine contact and withdraw consent. This "
            "does not change treatment.\nData retention: {retention}\nClinic "
            "contact: {clinic_contact}"
        ),
        "en": (
            "سند مساعد بالذكاء الاصطناعي لمتابعة تعليمات د. {doctor}. الربط محتاج "
            "موافقتك وتأكيد الدكتور إنك الشخص المقصود.\nهنعالج رسائلك وصوتك وصورك "
            "وبيانات حسابك وخطة الدكتور للشرح والتذكير وجمع المتابعة. المعالجة "
            "تشمل مقدم قناة تيليجرام ومقدمي الاستضافة السحابية ونماذج الذكاء "
            "الاصطناعي. رسائل البوت مش محادثة طبية مشفرة من الطرف للطرف.\nالمتابعة "
            "الروتينية بحد أقصى رسالة متابعة واحدة في اليوم، خارج ساعات الهدوء "
            "{quiet_start}–{quiet_end} بتوقيت {timezone}. أي تذكير بمواعيد محددة "
            "أو أثناء الهدوء محتاج موافقة منفصلة.\nسند ممكن يغلط، ومش بيشخص أو "
            "بيكتب علاج أو بيغير تعليمات الدكتور. مش خدمة طوارئ ومفيش وعد بوقت رد "
            "الدكتور. تقدر ترفض دلوقتي أو تتواصل مع العيادة لوقف التواصل الروتيني "
            "وسحب الموافقة. ده مش بيغير العلاج.\nالاحتفاظ بالبيانات: {retention}\n"
            "التواصل مع العيادة: {clinic_contact}\nSanad is an AI assistant "
            "following Dr {doctor}'s instructions. Linking requires your consent "
            "and the doctor's confirmation of your identity.\nWe process your "
            "messages, voice, images, account details and doctor's plan for "
            "explanations, reminders and follow-up collection. Processing involves "
            "the Telegram channel provider, cloud hosting providers and AI model "
            "providers. Bot messages are not end-to-end encrypted clinical "
            "communication.\nRoutine follow-up is limited to one chase message per "
            "day, outside quiet hours {quiet_start}–{quiet_end} in {timezone}. "
            "Scheduled reminders and quiet-hour exceptions require separate "
            "consent.\nSanad can make mistakes; it does not diagnose, prescribe or "
            "change the doctor's instructions. It is not an emergency service and "
            "does not promise a doctor response time. You may decline now or "
            "contact the clinic to stop routine contact and withdraw consent. This "
            "does not change treatment.\nData retention: {retention}\nClinic "
            "contact: {clinic_contact}"
        ),
    },
    "doctor.consent_recorded_wait_doctor": {
        "ar": (
            "موافقتك اتسجلت. مستنيين الدكتور يؤكد إنك الشخص المقصود قبل تفعيل "
            "الربط.\nYour consent was recorded. The doctor must confirm your "
            "identity before linking is activated."
        ),
        "en": (
            "موافقتك اتسجلت. مستنيين الدكتور يؤكد إنك الشخص المقصود قبل تفعيل "
            "الربط.\nYour consent was recorded. The doctor must confirm your "
            "identity before linking is activated."
        ),
    },
    "doctor.consent_declined_ack": {
        "ar": (
            "تم تسجيل عدم الموافقة. الربط مش هيتفعل.\nYour decline was recorded. "
            "Linking will not be activated."
        ),
        "en": (
            "تم تسجيل عدم الموافقة. الربط مش هيتفعل.\nYour decline was recorded. "
            "Linking will not be activated."
        ),
    },
    "doctor.claim_refused": {
        "ar": (
            "مش ممكن نكمل الربط من الدعوة دي. تواصل مع العيادة.\nThis invitation "
            "cannot complete linking. Contact the clinic."
        ),
        "en": (
            "مش ممكن نكمل الربط من الدعوة دي. تواصل مع العيادة.\nThis invitation "
            "cannot complete linking. Contact the clinic."
        ),
    },
    "doctor.claim_awaiting_doctor": {
        "ar": (
            "صاحب حساب تيليجرام {claimant} وافق على الربط. تأكد من هويته من "
            "المقابلة أو وسيلة تواصل موثوقة قبل التأكيد.\nTelegram account "
            "{claimant} consented to linking. Verify this is the intended patient "
            "through the encounter or an established contact method before "
            "confirming."
        ),
        "en": (
            "صاحب حساب تيليجرام {claimant} وافق على الربط. تأكد من هويته من "
            "المقابلة أو وسيلة تواصل موثوقة قبل التأكيد.\nTelegram account "
            "{claimant} consented to linking. Verify this is the intended patient "
            "through the encounter or an established contact method before "
            "confirming."
        ),
    },
    "doctor.claim_declined_doctor": {
        "ar": (
            "صاحب طلب الربط رفض الموافقة. الربط لم يتفعل؛ يمكنك إصدار دعوة جديدة.\n"
            "The claimant declined consent. Linking was not activated; you may "
            "issue a new invitation."
        ),
        "en": (
            "صاحب طلب الربط رفض الموافقة. الربط لم يتفعل؛ يمكنك إصدار دعوة جديدة.\n"
            "The claimant declined consent. Linking was not activated; you may "
            "issue a new invitation."
        ),
    },
    "doctor.claim_rejected": {
        "ar": (
            "طلب الربط لم يتم تأكيده. تواصل مع العيادة للحصول على دعوة جديدة.\nThe "
            "linking request was not confirmed. Contact the clinic for a new "
            "invitation."
        ),
        "en": (
            "طلب الربط لم يتم تأكيده. تواصل مع العيادة للحصول على دعوة جديدة.\nThe "
            "linking request was not confirmed. Contact the clinic for a new "
            "invitation."
        ),
    },
    "doctor.binding_confirmed": {
        "ar": (
            "الدكتور أكد هويتك وتم تفعيل ربط حسابك بسند بناءً على موافقتك.\nThe "
            "doctor confirmed your identity. Your Sanad account link is active "
            "with your consent."
        ),
        "en": (
            "الدكتور أكد هويتك وتم تفعيل ربط حسابك بسند بناءً على موافقتك.\nThe "
            "doctor confirmed your identity. Your Sanad account link is active "
            "with your consent."
        ),
    },
    "doctor.invitation_expired_doctor": {
        "ar": (
            "انتهت صلاحية دعوة الربط بدون تفعيل. يمكنك إصدار دعوة جديدة؛ مواعيد "
            "الخطة لم تتغير.\nThe linking invitation expired without activation. "
            "You may issue a new invitation; plan deadlines are unchanged."
        ),
        "en": (
            "انتهت صلاحية دعوة الربط بدون تفعيل. يمكنك إصدار دعوة جديدة؛ مواعيد "
            "الخطة لم تتغير.\nThe linking invitation expired without activation. "
            "You may issue a new invitation; plan deadlines are unchanged."
        ),
    },
    "doctor.patient_login_link": {
        "ar": (
            "رابط دخولك لسند صالح لمدة عشر دقايق ولمرة واحدة. اضغط متابعة لإكمال "
            "الدخول.\nYour Sanad login link is valid for ten minutes and one use. "
            "Press Continue to sign in.\n{link}"
        ),
        "en": (
            "رابط دخولك لسند صالح لمدة عشر دقايق ولمرة واحدة. اضغط متابعة لإكمال "
            "الدخول.\nYour Sanad login link is valid for ten minutes and one use. "
            "Press Continue to sign in.\n{link}"
        ),
    },
    "doctor.doctor_photo_unreadable": {
        "ar": "مش قادر أقرا الصورة: {reason}. صوّر من فوق في نور كويس وابعتها تاني.",
        "en": (
            "I could not read the image: {reason}. Photograph it from above in "
            "good light and resend it."
        ),
    },
    "doctor.scribe_intake_pending": {
        "ar": "الصورة محفوظة عندك. اختار المريض، أو مريض جديد، أو مش دلوقتي.",
        "en": "Your image is saved. Choose a patient, New patient, or Not now.",
    },
    "doctor.scribe_amendment_line": {
        "ar": "{drug}: {old} ← {new}",
        "en": "{drug}: {old} → {new}",
    },
    "doctor.scribe_card": {
        "ar": "{body}",
        "en": "{body}",
    },
    "doctor.scribe_confirmed": {
        "ar": "اتسجل:\n{body}",
        "en": "Recorded:\n{body}",
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
        "ar": (
            "افتح اللينك أو امسح الكود، وبعدها وافق على الربط واستنى تأكيد "
            "الدكتور.\nصالح 24 ساعة\n{link}"
        ),
        "en": (
            "Open the link or scan the code, consent to linking, and wait for the "
            "doctor's confirmation.\nValid for 24 hours\n{link}"
        ),
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
            "أوامر سند:\n/start — ترحيب\n/help — المساعدة\n/new الاسم — مريض جديد\n"
            "/find الاسم — بحث\n/qr الاسم — دعوة ربط\n/cancel — إلغاء الكارت\nابعت "
            "التعليمات كتابة أو بصوتك، وراجع الكارت قبل ✅ تمام."
        ),
        "en": (
            "Sanad commands:\n/start — welcome\n/help — help\n/new name — new patient\n"
            "/find name — search\n/qr name — linking invitation\n/cancel — cancel "
            "card\n/intake — saved images\n/lang en | ar — language\nSend "
            "instructions by text or voice and review the card before tapping ✅ "
            "Confirm.\nContest mode is English; Arabic is a declared upgrade."
        ),
    },
}

FIELDS = validate_catalog("doctor", CATALOG)
