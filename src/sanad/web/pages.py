"""Bilingual forms only, with stdlib templates and escaping at every substitution."""

from html import escape
from string import Template

OWNER_REVIEW_PENDING = True
PAGE_STRINGS = {
    "service": "سند / Sanad",
    "continue": "متابعة / Continue",
    "continue_text": "اضغط متابعة لإكمال الدخول. / Press Continue to sign in.",
    "refused": "مش ممكن نكمل الإجراء ده. / This action cannot be completed.",
    "invitation": "دعوة للربط بسند / Invitation to link with Sanad",
    "doctor_label": "الدكتور / Doctor",
    "telegram_instruction": "افتح تيليجرام واضغط Start. / Open Telegram and press Start.",
    "telegram_link": "فتح تيليجرام / Open Telegram",
    "doctor_home": "حساب الدكتور / Doctor account",
    "id_label": "معرّف الدكتور / Doctor ID",
    "name_label": "الاسم / Name",
    "status_label": "حالة الحساب / Account status",
    "approved": "معتمد / Approved",
    "patient_home": "حساب المريض / Patient account",
    "consent_label": "نسخة الموافقة / Consent version",
}
SHELL = Template("""<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title></head><body><main>$content</main></body></html>""")
CONTINUE = Template("""<h1>$title</h1><p>$message</p>
<form method="post" action="$action"><input type="hidden" name="csrf" value="$csrf">
<button type="submit">$button</button></form>""")
LANDING = Template("""<h1>$title</h1><p>$doctor_label: <bdi>$doctor</bdi></p>
<p>$instruction</p><a href="$link" rel="noreferrer">$button</a>""")
DOCTOR_HOME = Template("""<h1>$title</h1><dl><dt>$id_label</dt><dd><bdi>$id</bdi></dd>
<dt>$name_label</dt><dd><bdi>$name</bdi></dd><dt>$status_label</dt><dd>$status</dd></dl>""")
PATIENT_HOME = Template("""<h1>$title</h1><dl><dt>$name_label</dt><dd><bdi>$name</bdi></dd>
<dt>$consent_label</dt><dd>$consent</dd></dl>""")


def render(template: Template, **values: str) -> str:
    return template.substitute({k: escape(v, quote=True) for k, v in values.items()})


def shell(title: str, content: str) -> str:
    # Content is markup rendered exclusively by the escaping functions in this module.
    return SHELL.substitute(title=escape(title, quote=True), content=content)


def continue_page(action: str, csrf: str) -> str:
    title = PAGE_STRINGS["continue"]
    return shell(
        title,
        render(
            CONTINUE,
            title=title,
            message=PAGE_STRINGS["continue_text"],
            action=action,
            csrf=csrf,
            button=title,
        ),
    )


def refused_page() -> str:
    return shell(PAGE_STRINGS["service"], "<p>" + escape(PAGE_STRINGS["refused"]) + "</p>")


def invitation_page(doctor: str, link: str) -> str:
    title = PAGE_STRINGS["invitation"]
    return shell(
        title,
        render(
            LANDING,
            title=title,
            doctor_label=PAGE_STRINGS["doctor_label"],
            doctor=doctor,
            instruction=PAGE_STRINGS["telegram_instruction"],
            link=link,
            button=PAGE_STRINGS["telegram_link"],
        ),
    )


def doctor_home(id: str, name: str) -> str:
    title = PAGE_STRINGS["doctor_home"]
    return shell(
        title,
        render(
            DOCTOR_HOME,
            title=title,
            id_label=PAGE_STRINGS["id_label"],
            id=id,
            name_label=PAGE_STRINGS["name_label"],
            name=name,
            status_label=PAGE_STRINGS["status_label"],
            status=PAGE_STRINGS["approved"],
        ),
    )


def patient_home(name: str, consent: int) -> str:
    title = PAGE_STRINGS["patient_home"]
    return shell(
        title,
        render(
            PATIENT_HOME,
            title=title,
            name_label=PAGE_STRINGS["name_label"],
            name=name,
            consent_label=PAGE_STRINGS["consent_label"],
            consent=str(consent),
        ),
    )
