"""Browser shell and static assets. Clinical data comes only from accepted APIs."""

from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from sanad.auth.claim import ClaimService
from sanad.auth.login import LoginService
from sanad.domain.language import Audience, effective
from sanad.store.records import WebSession
from sanad.web.patient_controls import controls
from sanad.web.routes import require_session

ASSETS = Path(__file__).with_name("static")


def patient_content(data: dict[str, object], locale: str) -> str:
    """Escaped first-response plan from the same accepted patient projection."""
    en = locale == "en"

    def text(value: object) -> str:
        return "<bdi>" + escape(str(value or "")) + "</bdi>"

    def rows(key: str) -> list[dict[str, object]]:
        value = data.get(key)
        return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []

    def section(title: str, content: str) -> str:
        return '<section class="section"><h2>' + title + "</h2>" + content + "</section>"

    orders = "".join(
        '<article class="record-item"><p>'
        + " · ".join(
            text(o.get(k))
            for k in ("drug", "dose", "frequency", "timing", "route", "duration")
            if o.get(k)
        )
        + "</p></article>"
        for o in rows("orders")
    )
    requests = "".join(
        '<article class="record-item"><p>'
        + text(m.get("title"))
        + "</p><p>"
        + ("Due: " if en else "الموعد: ")
        + text(m.get("due_at"))
        + "</p></article>"
        for m in rows("next_missions")
    )
    reports = "".join(
        '<article class="record-item"><p>'
        + text(r.get("text"))
        + "</p><small>"
        + ("Self-reported." if en else "حسب البلاغ.")
        + "</small></article>"
        for r in rows("medication_reports")
    )
    reading = data.get("last_reading")
    prefs = data.get("preferences")
    prefs = prefs if isinstance(prefs, dict) else {}
    quiet = prefs.get("quiet_hours")
    quiet = quiet if isinstance(quiet, list) else []
    reminder = (
        ("Enabled" if en else "مفعلة")
        if prefs.get("routine_contact_enabled") and prefs.get("contact_status") == "active"
        else ("Paused or stopped" if en else "مؤجلة أو متوقفة")
    )
    missing = '<p class="empty">' + ("Not recorded." if en else "غير مسجل.") + "</p>"
    return (
        "<p>"
        + ("Your doctor: " if en else "دكتورك: ")
        + text(data.get("doctor_name"))
        + "</p>"
        + section("Your medication plan" if en else "خطة أدويتك", orders or missing)
        + section("What your doctor asked for" if en else "المطلوب منك", requests or missing)
        + section("Medication reports" if en else "بلاغات الأدوية", reports or missing)
        + section(
            "Your last reported reading" if en else "آخر قراءة أبلغت بها",
            "<p>" + text(reading.get("text")) + "</p>" if isinstance(reading, dict) else missing,
        )
        + section(
            "Reminders" if en else "التذكيرات",
            "<p>"
            + reminder
            + "</p><p>"
            + ("Quiet hours: " if en else "ساعات الهدوء: ")
            + ", ".join(text(v) for v in quiet)
            + " · "
            + text(prefs.get("timezone"))
            + "</p>",
        )
        + section(
            "Your open questions" if en else "أسئلتك المعلّقة",
            "".join("<p>" + text(q.get("question")) + "</p>" for q in rows("open_questions"))
            or missing,
        )
    )


def surface(
    name: str,
    language: str,
    *,
    audience: Audience = "doctor",
    view: str = "patients",
    patient_id: str = "",
    demo: bool = False,
    patient_plan: dict[str, object] | None = None,
) -> str:
    locale = effective(language, audience=audience)
    direction = "rtl" if locale == "ar" else "ltr"
    title = (
        "خطتك"
        if audience == "patient" and locale == "ar"
        else ("Your care" if audience == "patient" else "Sanad")
    )
    home = "/demo" if demo else "/pp" if audience == "patient" else "/a"
    noscript = (
        "فعّل JavaScript لعرض البيانات. الدخول متاح من تيليجرام."
        if locale == "ar"
        else "Enable JavaScript to load this view. Sign in through Telegram."
    )
    # All data attributes are escaped, and no private record is embedded in scripts.
    attrs = {
        "view": view,
        "patient": patient_id,
        "audience": audience,
        "demo": "true" if demo else "false",
    }
    data = " ".join(f'data-{k}="{escape(v, quote=True)}"' for k, v in attrs.items())
    initial = (
        patient_content(patient_plan, locale)
        if patient_plan is not None
        else "<p>" + ("جار تحميل الصفحة…" if locale == "ar" else "Loading your page…") + "</p>"
    )
    return f'''<!doctype html>
<html lang="{locale}" dir="{direction}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{title} · Sanad</title>
<script src="/assets/theme.js">
</script>
<link rel="stylesheet" href="/assets/browser.css">
<link rel="preload" href="/assets/inter.ttf" as="font" type="font/ttf" crossorigin>
<link rel="preload" href="/assets/notosansarabic.ttf" as="font" type="font/ttf" crossorigin>
<script src="/assets/browser.js" defer>
</script>
</head>
<body {data}>
<svg class="icon-sprite" aria-hidden="true"><symbol id="status-icon" viewBox="0 0 16 16">
<circle cx="8" cy="8" r="6"/><path d="M8 4v5m0 2v1"/></symbol></svg>
<a class="skip" href="#workspace">{"انتقل للمحتوى" if locale == "ar" else "Skip to content"}</a>
<div class="app">
<aside class="rail">
<a class="identity" href="{home}">{"سند" if locale == "ar" else "Sanad"}
</a>
<p class="account">
<bdi>{escape(name)}</bdi>
</p>
<nav id="navigation" aria-label="{"التنقل" if locale == "ar" else "Navigation"}">
</nav>
<div class="rail-bottom">
<label for="theme">{"المظهر" if locale == "ar" else "Appearance"}</label>
<select id="theme">
<option value="system">{"النظام" if locale == "ar" else "System"}</option>
<option value="light">{"فاتح" if locale == "ar" else "Light"}</option>
<option value="dark">{"داكن" if locale == "ar" else "Dark"}</option>
</select>
</div>
</aside>
<main id="workspace" tabindex="-1">
<div id="banner">
</div>
<header class="page-heading">
<p class="eyebrow" id="eyebrow">
</p>
<h1 id="title">{title}</h1>
<p id="subtitle">
</p>
</header>
<div class="refresh-bar">
<span id="freshness" role="status">
</span>
<button id="refresh" type="button">{"تحديث" if locale == "ar" else "Refresh"}</button>
</div>
<div id="feedback" role="status" aria-live="polite">
</div>
<div id="content" aria-busy="true">
{initial}
</div>
{controls(locale) if audience == "patient" else ""}
<noscript>
<p>{noscript}</p>
</noscript>
</main>
</div>
</body>
</html>'''


def browser_router(login: LoginService, claims: ClaimService) -> APIRouter:
    router = APIRouter()

    @router.get("/assets/{name}", include_in_schema=False)
    def asset(name: str) -> FileResponse:
        # A flat allow-list prevents path traversal and source-file disclosure.
        allowed = {
            p.name
            for p in ASSETS.iterdir()
            if p.suffix in {".css", ".js", ".ttf"} or p.name == "demo.json"
        }
        if name not in allowed:
            raise HTTPException(404)
        mime = (
            "font/ttf"
            if name.endswith(".ttf")
            else (
                "application/json"
                if name == "demo.json"
                else "text/css"
                if name.endswith(".css")
                else "text/javascript"
            )
        )
        return FileResponse(ASSETS / name, media_type=mime)

    @router.get("/a/{view}")
    def doctor_view(
        view: str, session: Annotated[WebSession, Depends(require_session("doctor"))]
    ) -> HTMLResponse:
        if view not in {"inbox", "history", "preferences"}:
            raise HTTPException(404)
        doctor = login.accounts.doctor(session.doctor_id)
        if doctor is None:
            raise HTTPException(401)
        return HTMLResponse(surface(doctor.name, doctor.language, view=view))

    @router.get("/a/patients/{patient_id}")
    def detail(
        patient_id: str, session: Annotated[WebSession, Depends(require_session("doctor"))]
    ) -> HTMLResponse:
        # Reuse the same scoped service the record API uses, before returning a shell.
        patient = claims.patient(session.doctor_id, patient_id)
        if patient is None:
            from sanad.web.pages import shell

            return HTMLResponse(
                shell("Sanad", "<p>Patient not found.</p>", "en", interactive=True), status_code=404
            )
        doctor = login.accounts.doctor(session.doctor_id)
        if doctor is None:
            raise HTTPException(401)
        return HTMLResponse(
            surface(doctor.name, doctor.language, view="detail", patient_id=patient_id)
        )

    @router.get("/demo")
    def demo() -> HTMLResponse:
        # No session lookup, no store, no authenticated link. Data is a static fixture.
        return HTMLResponse(surface("Synthetic clinic", "en", demo=True))

    return router
