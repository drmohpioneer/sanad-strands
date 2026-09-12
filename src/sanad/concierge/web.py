"""One neutral RTL patient view using the accepted page shell and escaping."""

from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import JsonValue

    from sanad.channels.transport import SendOutcome
    from sanad.store.records import OutboundIntent

from sanad.domain.language import effective
from sanad.web.pages import shell


def patient_page(name: str, data: dict[str, object], language: str = "ar") -> str:
    locale = effective(language, audience="patient")

    def text(ar: str, en: str) -> str:
        return en if locale == "en" else ar

    blocks = [
        text("<h1>خطتك</h1>", "<h1>Your care</h1>"),
        "<p>" + escape(name) + "</p>",
        text("<p>دكتورك: ", "<p>Your doctor: ") + escape(str(data.get("doctor_name", ""))) + "</p>",
    ]
    orders = data.get("orders")
    if isinstance(orders, list):
        blocks.extend(
            "<p>" + escape(str(o["line"])) + "</p>" for o in orders if isinstance(o, dict)
        )
    missions = data.get("next_missions")
    if isinstance(missions, list):
        blocks.extend(
            "<p>"
            + escape(str(m.get("title", "")))
            + ": "
            + escape(str(m.get("due_at", "")))
            + "</p>"
            for m in missions
            if isinstance(m, dict)
        )
    reading = data.get("last_reading")
    if isinstance(reading, dict):
        blocks.append(
            text("<h2>آخر قراءة بلّغت بيها</h2><p>", "<h2>Your last reported reading</h2><p>")
            + escape(str(reading.get("text", "")))
            + "</p>"
        )
    prefs = data.get("preferences")
    if isinstance(prefs, dict):
        status = {
            "active": text("التذكيرات مفعّلة", "Reminders enabled"),
            "paused": text("التذكيرات مؤجلة", "Reminders paused"),
            "opted_out": text("التذكيرات متوقفة", "Reminders stopped"),
        }.get(str(prefs.get("contact_status")), text("تفضيلات التواصل", "Contact preferences"))
        blocks.append(text("<h2>التذكيرات</h2><p>", "<h2>Reminders</h2><p>") + status + "</p>")
        if prefs.get("resume_at"):
            blocks.append(
                text("<p>التأجيل لحد ", "<p>Paused until ")
                + escape(str(prefs["resume_at"]))
                + "</p>"
            )
        blocks.append(
            text("<p>ساعات الهدوء: ", "<p>Quiet hours: ")
            + escape(
                ": ".join(
                    str(v)
                    for v in prefs.get("quiet_hours", [])
                    if isinstance(prefs.get("quiet_hours"), list)
                )
            )
            + "</p>"
        )
    questions = data.get("open_questions")
    if isinstance(questions, list) and questions:
        blocks.append(text("<h2>أسئلتك</h2>", "<h2>Your questions</h2>"))
        blocks.extend(
            "<p>"
            + escape(str(q.get("question", "")))
            + text(": في انتظار الدكتور</p>", ": Waiting for your doctor</p>")
            for q in questions
            if isinstance(q, dict)
        )
    return shell(text("خطتك", "Your care"), "\n".join(blocks), locale)


def stored_reply(intent: "OutboundIntent", payload: "dict[str, JsonValue] | None") -> "SendOutcome":
    """Completion makes the exact text visible; no external transport is involved."""
    from sanad.channels.transport import SendOutcome

    if payload is None or not isinstance(payload.get("text"), str):
        return SendOutcome(status="failed", retryable=False, code="missing_text")
    return SendOutcome(status="accepted", provider_message_id="web:" + intent.id)
