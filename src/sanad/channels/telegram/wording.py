"""Owner-review-pending Egyptian Arabic / English account wording, plain text."""

import html
import string
import unicodedata
from typing import Literal

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

TEMPLATES: dict[str, str] = {
    "application_received": (
        "طلب التسجيل كدكتور وصل، ومستني مراجعة الإدارة.\n"
        "Your doctor registration application was received and is awaiting admin review."
    ),
    "admin_new_application": (
        "طلب تسجيل دكتور جديد. البيانات دي مقدمة من صاحب الطلب ولسه مش متحققة:\n"
        "الاسم: {name}\nالتخصص: {specialty}\nالمدينة: {city}\n"
        "New doctor application. These applicant-provided details are unverified:\n"
        "Name: {name}\nSpecialty: {specialty}\nCity: {city}"
    ),
    "doctor_approved": (
        "تمت الموافقة على حسابك كدكتور في سند.\nYour doctor account in Sanad has been approved."
    ),
    "application_rejected": (
        "طلب التسجيل مش مقبول حاليًا.\nYour registration application is not approved at this time."
    ),
    "doctor_welcome_back": (
        "أهلًا برجوعك. حسابك كدكتور في سند معتمد.\n"
        "Welcome back. Your doctor account in Sanad is approved."
    ),
    "doctor_capability_pending": (
        "رسالتك وصلت. التعامل مع خطط المرضى والملفات مش متاح هنا لسه.\n"
        "Your message was received. Patient plans and files cannot be handled here yet."
    ),
    "callback_refused": (
        "الإجراء ده مش متاح من الزر ده.\nThis action is unavailable from this button."
    ),
    "doctor_suspended_notice": (
        "تم تعليق صلاحيات حسابك كدكتور في سند.\n"
        "Your doctor account permissions in Sanad have been suspended."
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
ALL_TEMPLATES = TEMPLATES | ENROLLMENT_TEMPLATES

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


def check_templates() -> None:
    for key, text in ALL_TEMPLATES.items():
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


def render(template_id: str, **fields: str) -> str:
    if template_id not in ALL_TEMPLATES or set(fields) != FIELDS[template_id]:
        raise ValueError("account template requires exactly its declared fields")
    return ALL_TEMPLATES[template_id].format(
        **{k: v if k == "link" else untrusted(v) for k, v in fields.items()}
    )


check_templates()
