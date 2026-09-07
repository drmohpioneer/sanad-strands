"""One neutral RTL patient view using the accepted page shell and escaping."""

from html import escape

from sanad.web.pages import shell


def patient_page(name: str, data: dict[str, object]) -> str:
    blocks = [
        "<h1>خطتك</h1>",
        "<p>" + escape(name) + "</p>",
        "<p>دكتورك: " + escape(str(data.get("doctor_name", ""))) + "</p>",
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
            + " — "
            + escape(str(m.get("due_at", "")))
            + "</p>"
            for m in missions
            if isinstance(m, dict)
        )
    reading = data.get("last_reading")
    if isinstance(reading, dict):
        blocks.append(
            "<h2>آخر قراءة بلّغت بيها</h2><p>" + escape(str(reading.get("text", ""))) + "</p>"
        )
    prefs = data.get("preferences")
    if isinstance(prefs, dict):
        status = {
            "active": "التذكيرات مفعّلة",
            "paused": "التذكيرات مؤجلة",
            "opted_out": "التذكيرات متوقفة",
        }.get(str(prefs.get("contact_status")), "تفضيلات التواصل")
        blocks.append("<h2>التذكيرات</h2><p>" + status + "</p>")
        if prefs.get("resume_at"):
            blocks.append("<p>التأجيل لحد " + escape(str(prefs["resume_at"])) + "</p>")
        blocks.append(
            "<p>ساعات الهدوء: "
            + escape(
                " — ".join(
                    str(v)
                    for v in prefs.get("quiet_hours", [])
                    if isinstance(prefs.get("quiet_hours"), list)
                )
            )
            + "</p>"
        )
    questions = data.get("open_questions")
    if isinstance(questions, list) and questions:
        blocks.append("<h2>أسئلتك</h2>")
        blocks.extend(
            "<p>" + escape(str(q.get("question", ""))) + " — في انتظار الدكتور</p>"
            for q in questions
            if isinstance(q, dict)
        )
    return shell("خطتك", "\n".join(blocks))
