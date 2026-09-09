"""Owner-review-pending Egyptian Arabic / English account wording, plain text."""

import html
import string
import unicodedata
from typing import Literal

from sanad.domain.language import effective as contest_language
from sanad.presentation.context import PresentationContext

OWNER_REVIEW_PENDING = True

type TemplateId = Literal[
    "application_received",
    "admin_new_application",
    "doctor_approved",
    "application_rejected",
    "doctor_welcome_back",
    "doctor_capability_pending",
    "callback_refused",
    "doctor_suspended_notice",
]

TEMPLATES: dict[str, tuple[str, str]] = {
    "application_received": (
        "طلب التسجيل كدكتور وصل، ومستني مراجعة الإدارة.\n"
        "Your doctor registration application was received and is awaiting admin review.",
        "Your doctor registration application was received and is awaiting admin review.",
    ),
    "admin_new_application": (
        "طلب تسجيل دكتور جديد. البيانات دي مقدمة من صاحب الطلب ولسه مش متحققة:\n"
        "الاسم: {name}\nالتخصص: {specialty}\nالمدينة: {city}\n"
        "New doctor application. These applicant-provided details are unverified:\n"
        "Name: {name}\nSpecialty: {specialty}\nCity: {city}",
        "New doctor application. These applicant-provided details are unverified:\n"
        "Name: {name}\nSpecialty: {specialty}\nCity: {city}",
    ),
    "doctor_approved": (
        "تمت الموافقة على حسابك كدكتور في سند.\nYour doctor account in Sanad has been approved.",
        "Your doctor account in Sanad has been approved.",
    ),
    "application_rejected": (
        "طلب التسجيل مش مقبول حاليًا.\nYour registration application is not approved at this time.",
        "Your registration application is not approved at this time.",
    ),
    "doctor_welcome_back": (
        "أهلًا برجوعك. حسابك كدكتور في سند معتمد.\n"
        "Welcome back. Your doctor account in Sanad is approved.",
        "Welcome back. Your doctor account in Sanad is approved.",
    ),
    "doctor_capability_pending": (
        "رسالتك وصلت. التعامل مع خطط المرضى والملفات مش متاح هنا لسه.\n"
        "Your message was received. Patient plans and files cannot be handled here yet.",
        "Your message was received. Patient plans and files cannot be handled here yet.",
    ),
    "callback_refused": (
        "الإجراء ده مش متاح من الزر ده.\nThis action is unavailable from this button.",
        "This action is unavailable from this button.",
    ),
    "doctor_suspended_notice": (
        "تم تعليق صلاحيات حسابك كدكتور في سند.\n"
        "Your doctor account permissions in Sanad have been suspended.",
        "Your doctor account permissions in Sanad have been suspended.",
    ),
}
APPROVE_BUTTON = "موافقة / Approve"
REJECT_BUTTON = "رفض / Reject"
CONSENT_TEXT_VERSION = "consent-draft-2026-09-06-v1"
CONSENT_ACCEPT_BUTTON = "أوافق / Accept"
CONSENT_DECLINE_BUTTON = "لا أوافق / Decline"
CLAIM_CONFIRM_BUTTON = "أؤكد هوية المريض / Confirm patient identity"
ENROLLMENT_TEMPLATES = {
    "doctor_login_link": (
        "رابط دخولك لسند صالح لمدة عشر دقايق ولمرة واحدة. اضغط متابعة لإكمال الدخول.\n"
        "Your Sanad login link is valid for ten minutes and one use. Press Continue to "
        "sign in.\n{link}"
    ),
    "consent_request": (
        "سند مساعد بالذكاء الاصطناعي لمتابعة تعليمات د. {doctor}. الربط محتاج موافقتك "
        "وتأكيد الدكتور إنك الشخص المقصود.\n"
        "هنعالج رسائلك وصوتك وصورك وبيانات حسابك وخطة الدكتور للشرح والتذكير وجمع المتابعة. "
        "المعالجة تشمل مقدم قناة تيليجرام ومقدمي الاستضافة السحابية ونماذج الذكاء الاصطناعي. "
        "رسائل البوت مش محادثة طبية مشفرة من الطرف للطرف.\n"
        "المتابعة الروتينية بحد أقصى رسالة متابعة واحدة في اليوم، خارج ساعات الهدوء "
        "{quiet_start}–{quiet_end} "
        "بتوقيت {timezone}. أي تذكير بمواعيد محددة أو أثناء الهدوء محتاج موافقة منفصلة.\n"
        "سند ممكن يغلط، ومش بيشخص أو بيكتب علاج أو بيغير تعليمات الدكتور. مش خدمة "
        "طوارئ ومفيش وعد بوقت رد الدكتور. "
        "تقدر ترفض دلوقتي أو تتواصل مع العيادة لوقف التواصل الروتيني وسحب الموافقة. ده "
        "مش بيغير العلاج.\n"
        "الاحتفاظ بالبيانات: {retention}\nالتواصل مع العيادة: {clinic_contact}\n"
        "Sanad is an AI assistant following Dr {doctor}'s instructions. Linking "
        "requires your consent and the doctor's confirmation of your identity.\n"
        "We process your messages, voice, images, account details and doctor's plan "
        "for explanations, reminders and follow-up collection. "
        "Processing involves the Telegram channel provider, cloud hosting providers "
        "and AI model providers. "
        "Bot messages are not end-to-end encrypted clinical communication.\n"
        "Routine follow-up is limited to one chase message per day, outside quiet "
        "hours {quiet_start}–{quiet_end} in {timezone}. "
        "Scheduled reminders and quiet-hour exceptions require separate consent.\n"
        "Sanad can make mistakes; it does not diagnose, prescribe or change the "
        "doctor's instructions. "
        "It is not an emergency service and does not promise a doctor response time. "
        "You may decline now or contact the clinic to stop routine contact and "
        "withdraw consent. This does not change treatment.\n"
        "Data retention: {retention}\nClinic contact: {clinic_contact}"
    ),
    "consent_recorded_wait_doctor": (
        "موافقتك اتسجلت. مستنيين الدكتور يؤكد إنك الشخص المقصود قبل تفعيل الربط.\n"
        "Your consent was recorded. The doctor must confirm your identity before "
        "linking is activated."
    ),
    "consent_declined_ack": (
        "تم تسجيل عدم الموافقة. الربط مش هيتفعل.\nYour decline was recorded. Linking "
        "will not be activated."
    ),
    "claim_refused": (
        "مش ممكن نكمل الربط من الدعوة دي. تواصل مع العيادة.\n"
        "This invitation cannot complete linking. Contact the clinic."
    ),
    "claim_awaiting_doctor": (
        "صاحب حساب تيليجرام {claimant} وافق على الربط. تأكد من هويته من المقابلة أو "
        "وسيلة تواصل موثوقة قبل التأكيد.\n"
        "Telegram account {claimant} consented to linking. Verify this is the intended "
        "patient through the encounter or an established contact method before "
        "confirming."
    ),
    "claim_declined_doctor": (
        "صاحب طلب الربط رفض الموافقة. الربط لم يتفعل؛ يمكنك إصدار دعوة جديدة.\n"
        "The claimant declined consent. Linking was not activated; you may issue a new invitation."
    ),
    "claim_rejected": (
        "طلب الربط لم يتم تأكيده. تواصل مع العيادة للحصول على دعوة جديدة.\n"
        "The linking request was not confirmed. Contact the clinic for a new invitation."
    ),
    "binding_confirmed": (
        "الدكتور أكد هويتك وتم تفعيل ربط حسابك بسند بناءً على موافقتك.\n"
        "The doctor confirmed your identity. Your Sanad account link is active with your consent."
    ),
    "invitation_expired_doctor": (
        "انتهت صلاحية دعوة الربط بدون تفعيل. يمكنك إصدار دعوة جديدة؛ مواعيد الخطة لم تتغير.\n"
        "The linking invitation expired without activation. You may issue a new "
        "invitation; plan deadlines are unchanged."
    ),
    "patient_login_link": (
        "رابط دخولك لسند صالح لمدة عشر دقايق ولمرة واحدة. اضغط متابعة لإكمال الدخول.\n"
        "Your Sanad login link is valid for ten minutes and one use. Press Continue to "
        "sign in.\n{link}"
    ),
}
# Preserve the accepted account catalog; enrollment has its own public catalog.
SCRIBE_TEMPLATES = {
    "doctor_photo_unreadable": (
        "مش قادر أقرا الصورة: {reason}. صوّر من فوق في نور كويس وابعتها تاني.",
        "I could not read the image: {reason}. "
        "Photograph it from above in good light and resend it.",
    ),
    "scribe_intake_pending": (
        "الصورة محفوظة عندك. اختار المريض، أو مريض جديد، أو مش دلوقتي.",
        "Your image is saved. Choose a patient, New patient, or Not now.",
    ),
    "scribe_amendment_line": ("{drug}: {old} ← {new}", "{drug}: {old} → {new}"),
    "scribe_card": ("{body}", "{body}"),
    "scribe_confirmed": ("اتسجل:\n{body}", "Recorded:\n{body}"),
    "scribe_stale": (
        "الكارت اتغير أو مبقاش صالح. ابعت التعليمات من جديد.",
        "The card changed or is no longer valid. Send the instructions again.",
    ),
    "scribe_discarded": (
        "تمام، لغيت الكارت ومفيش تعليمات اتسجلت.",
        "Card cancelled. No instructions were recorded.",
    ),
    "scribe_expired": (
        "صلاحية الكارت انتهت. ابعت التعليمات من جديد.",
        "The card expired. Send the instructions again.",
    ),
    "scribe_edit": (
        "ابعت التعديل كتابة أو بصوتك؛ الكارت القديم مش هيتأكد.",
        "Send your correction by text or voice. The previous card cannot be confirmed.",
    ),
    "scribe_invitation": (
        "افتح اللينك أو امسح الكود، وبعدها وافق على الربط واستنى تأكيد الدكتور.\n"
        "صالح 24 ساعة\n{link}",
        "Open the link or scan the code, consent to linking, "
        "and wait for the doctor's confirmation.\n"
        "Valid for 24 hours\n{link}",
    ),
    "doctor_voice_unreadable": (
        "مش قادر أسمع التسجيل. ابعته تاني أو اكتب الكلام.",
        "I could not hear the recording. Send it again or type the instructions.",
    ),
    "doctor_model_unavailable": (
        "مش قادر أقرأ دلوقتي، ابعت تاني بعد شوية",
        "I cannot read this right now. Please try again shortly.",
    ),
    "doctor_patient_not_found": (
        "ملقيتش المريض ده عندك. اكتب /new وبعدها الاسم لو مريض جديد.",
        "I could not find this patient. Use /new followed by the name for a new patient.",
    ),
    "doctor_help": (
        "أوامر سند:\n/start — ترحيب\n/help — المساعدة\n/new الاسم — مريض جديد\n"
        "/find الاسم — بحث\n/qr الاسم — دعوة ربط\n/cancel — إلغاء الكارت\n"
        "ابعت التعليمات كتابة أو بصوتك، وراجع الكارت قبل ✅ تمام.",
        "Sanad commands:\n/start — welcome\n/help — help\n/new name — new patient\n"
        "/find name — search\n/qr name — linking invitation\n/cancel — cancel card\n"
        "/intake — saved images\n/lang en | ar — language\n"
        "Send instructions by text or voice and review the card before tapping ✅ Confirm.\n"
        "Contest mode is English; Arabic is a declared upgrade.",
    ),
}
ALL_TEMPLATES = TEMPLATES | ENROLLMENT_TEMPLATES | SCRIBE_TEMPLATES

# Existing fixed button/call-site labels, separate from outbound template ids.
BUTTONS = {
    "confirm": ("✅ تمام", "✅ Confirm"),
    "edit": ("✏️ تعديل", "✏️ Edit"),
    "reject": ("❌ إلغاء", "❌ Cancel"),
    "new": ("مريض جديد", "New patient"),
    "correct_reply": ("تعديل للكارت", "Update card"),
    "new_reply": ("مريض جديد", "New patient"),
    "later": ("مش دلوقتي", "Not now"),
}
LABELS = {
    "no_intake": ("مفيش صور مستنية اختيار مريض.", "No images are waiting for a patient selection."),
    "choose_reply": ("ده تعديل للكارت ولا مريض جديد؟", "Is this a card update or a new patient?"),
    "patient": ("المريض: ", "Patient: "),
    "alerted": ("⚠️ تم تنبيهك", "⚠️ You have been alerted"),
    "intake_danger": (
        "⚠️ نتيجة في صورة لسه مش مرتبطة بمريض محتاجة مراجعتك فورًا.",
        "⚠️ A result in an image not yet linked to a patient needs your immediate review.",
    ),
    "reading": ("قراءة {number}: ", "Reading {number}: "),
    "unreadable": ("غير مقروء", "unreadable"),
    "unchanged": (": زي ما هو", ": unchanged"),
    "stop": ("إيقاف", "stop"),
}


def button(action: str, language: str) -> str:
    return BUTTONS[action][language == "en"]


def label(key: str, language: str) -> str:
    return LABELS[key][contest_language(language) == "en"]


FIELDS = {
    key: frozenset({"name", "specialty", "city"}) if key == "admin_new_application" else frozenset()
    for key in TEMPLATES
}
FIELDS.update(
    {
        "doctor_login_link": frozenset({"link"}),
        "patient_login_link": frozenset({"link"}),
        "consent_request": frozenset(
            {"doctor", "quiet_start", "quiet_end", "timezone", "retention", "clinic_contact"}
        ),
        "claim_awaiting_doctor": frozenset({"claimant"}),
        "consent_recorded_wait_doctor": frozenset(),
        "consent_declined_ack": frozenset(),
        "claim_refused": frozenset(),
        "claim_declined_doctor": frozenset(),
        "claim_rejected": frozenset(),
        "binding_confirmed": frozenset(),
        "invitation_expired_doctor": frozenset(),
    }
)
FIELDS.update(
    {
        key: frozenset({"body"})
        if key in {"scribe_card", "scribe_confirmed"}
        else frozenset({"link"})
        if key == "scribe_invitation"
        else frozenset()
        for key in SCRIBE_TEMPLATES
    }
)

FIELDS.update(
    {
        "doctor_photo_unreadable": frozenset({"reason"}),
        "scribe_amendment_line": frozenset({"drug", "old", "new"}),
    }
)


def check_templates() -> None:
    for key, value in ALL_TEMPLATES.items():
        texts: tuple[str, ...]
        if key in ENROLLMENT_TEMPLATES:
            if not isinstance(value, str):
                raise ValueError("enrollment wording must remain unchanged")
            texts = (value,)
        else:
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or any(not isinstance(text, str) or not text for text in value)
            ):
                raise ValueError("account template requires both languages")
            texts = value
        for text in texts:
            fields = set()
            for _, name, spec, conversion in string.Formatter().parse(text):
                if name is not None:
                    if name not in FIELDS[key] or spec or conversion:
                        raise ValueError("unsafe account template placeholder")
                    fields.add(name)
            if fields != FIELDS[key]:
                raise ValueError("account template placeholder mismatch")


def untrusted(value: str) -> str:
    # Prevent a claimed name from manufacturing a second label or a bidi override.
    value = " ".join(
        "".join(c for c in value if not unicodedata.category(c).startswith("C")).split()
    )[:160]
    return html.escape(value, quote=True).replace("{", "&#123;").replace("}", "&#125;")


def render(template_id: str, language: str | PresentationContext, **fields: str) -> str:
    from sanad.presentation import doctor
    from sanad.presentation.catalog import opaque_fields
    from sanad.presentation.catalog import render as catalog_render
    from sanad.presentation.context import resolve

    context = resolve(
        language,
        "patient"
        if template_id
        in {
            "consent_request",
            "consent_recorded_wait_doctor",
            "consent_declined_ack",
            "claim_refused",
            "claim_rejected",
            "binding_confirmed",
            "patient_login_link",
        }
        else "doctor",
    )
    if template_id not in ALL_TEMPLATES or set(fields) != FIELDS[template_id]:
        raise ValueError("account template requires exactly its declared fields")
    sanitised = {
        k: v
        if k == "link" or (k == "body" and template_id in {"scribe_card", "scribe_confirmed"})
        else untrusted(v)
        for k, v in fields.items()
    }
    return catalog_render(
        doctor.CATALOG, "doctor." + template_id, context, **opaque_fields(sanitised)
    )


check_templates()
