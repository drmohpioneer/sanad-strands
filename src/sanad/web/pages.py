"""Single-language forms, with stdlib templates and escaping at every substitution."""

from html import escape
from pathlib import Path
from string import Template

from sanad.domain.language import effective

OWNER_REVIEW_PENDING = True
PAGE_STRINGS = {
    "service": {"ar": "سند", "en": "Sanad"},
    "continue": {"ar": "متابعة", "en": "Continue"},
    "continue_text": {"ar": "اضغط متابعة لإكمال الدخول.", "en": "Press Continue to sign in."},
    "refused": {"ar": "مش ممكن نكمل الإجراء ده.", "en": "This action cannot be completed."},
    "invitation": {"ar": "دعوة للربط بسند", "en": "Invitation to link with Sanad"},
    "doctor_label": {"ar": "الدكتور", "en": "Doctor"},
    "telegram_instruction": {
        "ar": "افتح تيليجرام واضغط Start.",
        "en": "Open Telegram and press Start.",
    },
    "telegram_link": {"ar": "فتح تيليجرام", "en": "Open Telegram"},
    "doctor_home": {"ar": "حساب الدكتور", "en": "Doctor account"},
    "id_label": {"ar": "معرّف الدكتور", "en": "Doctor ID"},
    "name_label": {"ar": "الاسم", "en": "Name"},
    "status_label": {"ar": "حالة الحساب", "en": "Account status"},
    "approved": {"ar": "معتمد", "en": "Approved"},
    "patient_home": {"ar": "حساب المريض", "en": "Patient account"},
    "consent_label": {"ar": "نسخة الموافقة", "en": "Consent version"},
}


def inline_logo(instance: str) -> str:
    """Embed the locked outlines; only namespace and approved theme paint vary."""
    source = (
        Path(__file__).with_name("static") / "logo" / "sanad-lockup-compact-navy.svg"
    ).read_text()
    return (
        source.replace("<svg ", '<svg class="sanad-lockup" aria-hidden="true" ', 1)
        .replace(' role="img" aria-label="Sanad"', "")
        .replace('id="g2"', f'id="{instance}-g2"')
        .replace("url(#g2)", f"url(#{instance}-g2)")
        .replace("#6D7BFF", "var(--logo-a)")
        .replace("#22D3EE", "var(--logo-b)")
        .replace("#EEF1FA", "var(--logo-ink)")
    )


ADMIN_SHELL = Template("""<!doctype html>
<html lang="$language" dir="$direction" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<link rel="icon" type="image/svg+xml" href="/assets/favicon.svg">
<title>$title</title><script src="/assets/theme.js"></script>
<link rel="stylesheet" href="/assets/browser.css"></head>
<body class="standalone"$demo>
<div class="aurora" aria-hidden="true">
<i>
</i>
<i>
</i>
<i>
</i>
</div>
<main>
<header class="page-heading">
<span class="identity mark">
$logo
<span class="visually-hidden">$service</span></span>
<span id="appearance-label" class="visually-hidden">Appearance</span>
<div id="theme" class="toggle" role="group" aria-labelledby="appearance-label">
<button type="button" data-theme-set="dark"
aria-pressed="true">Dark</button>
<button type="button" data-theme-set="light"
aria-pressed="false">Light</button></div></header>
$content</main></body></html>""")
SHELL = Template("""<!doctype html>
<html lang="$language" dir="$direction" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<link rel="icon" type="image/svg+xml" href="/assets/favicon.svg">
<title>$title</title><link rel="stylesheet" href="/assets/browser.css"></head>
<body class="standalone">
<div class="aurora" aria-hidden="true">
<i>
</i>
<i>
</i>
<i>
</i>
</div>
<main>
<header class="page-heading">
<span class="identity mark">
$logo
<span class="visually-hidden">$service</span></span>
</header>$content</main></body></html>""")
CONTINUE = Template("""<h1>$title</h1><p>$message</p>
<form method="post" action="$action"><input type="hidden" name="csrf" value="$csrf">
<button type="submit">$button</button></form>""")
LANDING = Template("""<h1>$title</h1><p>$doctor_label: <bdi>$doctor</bdi></p>
<p>$instruction</p><a href="$link" rel="noreferrer">$button</a>""")
DOCTOR_HOME = Template("""<h1>$title</h1><dl>
<dt>$name_label</dt><dd><bdi>$name</bdi></dd><dt>$status_label</dt><dd>$status</dd></dl>
<details class="support"><summary>Details for support</summary>
<bdi>$id</bdi></details>""")
PATIENT_HOME = Template("""<h1>$title</h1><dl><dt>$name_label</dt><dd><bdi>$name</bdi></dd>
</dl>""")


def render(template: Template, **values: str) -> str:
    return template.substitute({k: escape(v, quote=True) for k, v in values.items()})


def demo_banner() -> str:
    return (
        '<div class="demo-banner"><strong>Synthetic demonstration · Fictional records only</strong>'
        "<p>This view uses a separate static data source. It cannot open a clinical account.</p>"
        '<nav aria-label="Demonstrations"><a href="/demo">Doctor demo</a> · '
        '<a href="/demo/patient">Patient demo</a> · '
        '<a href="/demo/admin">Admin demo</a></nav></div>'
    )


def shell(
    title: str, content: str, language: str = "ar", *, interactive: bool = False, demo: bool = False
) -> str:
    # Content is markup rendered exclusively by the escaping functions in this module.
    locale = effective(language)
    return (ADMIN_SHELL if interactive else SHELL).substitute(
        title=escape(title, quote=True),
        content=content,
        logo=inline_logo("entry"),
        service=PAGE_STRINGS["service"][locale],
        language=locale,
        direction="rtl" if locale == "ar" else "ltr",
        demo=' data-demo="true"' if demo else "",
    )


def continue_page(action: str, csrf: str, language: str = "ar") -> str:
    title = PAGE_STRINGS["continue"][effective(language)]
    return shell(
        title,
        render(
            CONTINUE,
            title=title,
            message=PAGE_STRINGS["continue_text"][effective(language)],
            action=action,
            csrf=csrf,
            button=title,
        ),
        language,
    )


LOGIN_REFUSALS = {
    "signed_out": "You signed out. Send /login in Telegram for a new link.",
    "patient_sign_in": "Please sign in again from Telegram.",
    "patient_access_changed": "Your access changed elsewhere. Please sign in again from Telegram.",
    "session_busy": "Please try again shortly.",
    "expired": "This link has expired; send /login again.",
    "unknown_link": "This link has expired; send /login again.",
    "already_used": "This link was already used; send /login again.",
    "wrong_account": "This link is not for this account.",
}


def refused_page(reason: str | None = None, language: str = "ar") -> str:
    language = "en" if reason is not None else effective(language)
    message = (
        "Please try again in a minute."
        if reason == "unavailable"
        else LOGIN_REFUSALS.get(reason, "Please open the link from the message again.")
        if reason is not None
        else PAGE_STRINGS["refused"][effective(language)]
    )
    return shell(
        PAGE_STRINGS["service"][effective(language)],
        '<h1 class="refusal">' + escape(message) + "</h1>",
        "en" if reason else language,
    )


def invitation_page(doctor: str, link: str, language: str = "ar") -> str:
    title = PAGE_STRINGS["invitation"][effective(language)]
    return shell(
        title,
        render(
            LANDING,
            title=title,
            doctor_label=PAGE_STRINGS["doctor_label"][effective(language)],
            doctor=doctor,
            instruction=PAGE_STRINGS["telegram_instruction"][effective(language)],
            link=link,
            button=PAGE_STRINGS["telegram_link"][effective(language)],
        ),
        language,
    )


def doctor_home(id: str, name: str, language: str = "ar") -> str:
    title = PAGE_STRINGS["doctor_home"][effective(language)]
    return shell(
        title,
        render(
            DOCTOR_HOME,
            title=title,
            id_label=PAGE_STRINGS["id_label"][effective(language)],
            id=id,
            name_label=PAGE_STRINGS["name_label"][effective(language)],
            name=name,
            status_label=PAGE_STRINGS["status_label"][effective(language)],
            status=PAGE_STRINGS["approved"][effective(language)],
        ),
        language,
    )


def patient_home(name: str, consent: int, language: str = "ar") -> str:
    title = PAGE_STRINGS["patient_home"][effective(language)]
    return shell(
        title,
        render(
            PATIENT_HOME,
            title=title,
            name_label=PAGE_STRINGS["name_label"][effective(language)],
            name=name,
            consent_label=PAGE_STRINGS["consent_label"][effective(language)],
            consent=str(consent),
        ),
        language,
    )


def admin_home(*, demo: bool = False) -> str:
    return shell(
        "Administrator",
        (demo_banner() if demo else "")
        + "<h1>Administrator</h1><p>Account administration</p>"
        + ("" if demo else '<button id="admin-logout">Sign out everywhere</button>')
        + '<p id="admin-result" role="status"></p><div id="admin-applications"></div>'
        + '<script src="/assets/browser.js" defer></script>',
        "en",
        interactive=True,
        demo=demo,
    )


def admin_denied_page() -> str:
    return shell(
        "Sanad",
        '<p class="refusal">Not available from an administrator session. '
        "Your session was closed for safety; sign in again from Telegram.</p>"
        '<a href="/admin">Administrator sign-in</a>',
        "en",
        interactive=True,
    )


def admin_entry_page() -> str:
    return shell(
        "Sanad",
        "<h1>Administrator sign-in</h1><p>Open Telegram and send /login admin to sign in.</p>",
        "en",
        interactive=True,
    )
