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
FIELDS = {
    key: frozenset({"name", "specialty", "city"}) if key == "admin_new_application" else frozenset()
    for key in TEMPLATES
}


def check_templates() -> None:
    for key, text in TEMPLATES.items():
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
    if template_id not in TEMPLATES or set(fields) != FIELDS[template_id]:
        raise ValueError("account template requires exactly its declared fields")
    return TEMPLATES[template_id].format(**{k: untrusted(v) for k, v in fields.items()})


check_templates()
