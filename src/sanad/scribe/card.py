"""Plain Egyptian Arabic cards rendered solely from checked structured fields."""

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from sanad.channels.telegram import wording
from sanad.media.numbers import numbers_in
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.scribe.proposal import Proposal

REASONS = {
    "reader_disagreement": "قراءتين مختلفتين؛ اختار القراءة أو عدّل البند.",
    "shifted_rows": "الأرقام ممكن تكون متزحزحة؛ عدّل كل صف أو ابعت الصورة تاني.",
    "order_missing": "مفيش أمر مسجل بالدوا ده",
    "document_unclear": "الصورة محتاجة توضيح؛ ابعت كل مستند لوحده.",
    "unsupported_number": "فيه رقم مش موجود في كلامك؛ عدّل البند.",
    "dose_missing": "الجرعة ناقصة؛ قول الرقم.",
    "drug_unclear": "اسم الدوا مش واضح؛ قول الاسم كامل.",
    "disputed_number": "الرقم محتاج تأكيد؛ ابعت تعديل بالرقم المقصود.",
    "amendment_pending_09b": "تعديل أمر دوا موجود لسه مش متاح.",
    "timing_unclear": "الموعد مش واضح؛ حدده في التعديل.",
    "patient_missing": "مين المريض؟",
    "multiple_patients": "الكلام محتاج توضيح؛ لو فيه مريضين ابعت كل واحد لوحده.",
    "unsafe_text": "البند محتاج مراجعة بسبب إشارة خطر.",
    "empty_item": "البند فاضي؛ وضّح المطلوب.",
    "batch_too_large": "الكلام كتير على كارت واحد؛ ابعته على دفعات أصغر.",
    "clarification": "مش واضح المطلوب؛ وضّح المريض والتعليمات.",
    "duplicate_order": "فيه أكتر من أمر لنفس الدوا؛ وضّح أمر واحد في التعديل.",
    "unassigned_number": "فيه رقم سمعته ومالوش بند واضح؛ راجع الكارت وابعت تعديل.",
}
_ACTIONS = {"start": "بداية", "stop": "إيقاف", "change": "تغيير", "continue": "استمرار"}
_FACTS = {
    "patient_report": "نتيجة في الصورة",
    "condition": "التاريخ المرضي",
    "allergy": "حساسية",
    "history": "التاريخ المرضي",
    "medication_history": "أدوية قديمة",
}
_WEEKDAYS = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد")
_MONTHS = (
    "يناير",
    "فبراير",
    "مارس",
    "أبريل",
    "مايو",
    "يونيو",
    "يوليو",
    "أغسطس",
    "سبتمبر",
    "أكتوبر",
    "نوفمبر",
    "ديسمبر",
)


def arabic_datetime(instant: datetime, timezone: str, *, reference: datetime) -> str:
    local = instant.astimezone(ZoneInfo(timezone))
    year = f" {local.year}" if local.year != reference.astimezone(ZoneInfo(timezone)).year else ""
    period = (
        "بعد منتصف الليل"
        if local.hour < 5
        else "الصبح"
        if local.hour < 12
        else "الظهر"
        if local.hour < 15
        else "العصر"
        if local.hour < 18
        else "بالليل"
    )
    clock = str(local.hour % 12 or 12)
    if local.minute or local.second or local.microsecond:
        clock += f":{local.minute:02}"
    if local.second or local.microsecond:
        clock += f":{local.second:02}"
    if local.microsecond:
        clock += f".{local.microsecond:06}".rstrip("0")
    return (
        f"{_WEEKDAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]}{year}، "
        f"{clock} {period}"
    )


def plain(text: str) -> str:
    return " ".join("".join(c for c in text if not unicodedata.category(c).startswith("C")).split())


def supported_text(text: str, proposal: Proposal) -> str:
    """Do not read a hallucinated number back as if it came from the doctor."""
    text = plain(text).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    from sanad.scribe.amend import instruction_line

    present = set(
        numbers_in(
            proposal.source_text
            + " ".join(instruction_line(a.old) for a in proposal.amendments if a.old)
        )
    )
    return re.sub(
        r"\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?",
        lambda m: m[0] if set(numbers_in(m[0])) <= present else "[رقم غير مسموع]",
        text,
    )


def render_card(proposal: Proposal) -> tuple[str, ...]:
    candidate = proposal.candidate
    name = proposal.selected_display_name or candidate.patient.name_as_spoken or "مين المريض؟"
    line = "مريض جديد: " if proposal.creating_patient else "المريض: "
    lines = [
        line + (plain(name) if proposal.selected_display_name else supported_text(name, proposal)),
        "سمعت:",
    ]
    if proposal.photo:
        from sanad.scribe.crosscheck import SHIFT_WARNING

        lines[1] = "قريت في الصورة:"
        for printed_name in dict.fromkeys(
            r.printed_identity_hint.text
            for r in (proposal.photo.reads.first, proposal.photo.reads.second)
        ):
            if printed_name:
                lines.append("الاسم المطبوع (غير مؤكد): " + plain(printed_name))
        if proposal.photo.shift_detected:
            lines.append(SHIFT_WARNING)
        if proposal.photo.danger_raised:
            lines.append("⚠️ تم تنبيهك")
        for d in proposal.photo.reads.disagreements:
            if d.field not in proposal.photo.resolved_fields:
                label = (
                    ("صف " + str(int(d.field.split(".")[1]) + 1))
                    if d.field.startswith("items.")
                    else "بيانات الصورة"
                )
                lines.append(
                    label
                    + " — ⚠️ قراءتين مختلفتين: "
                    + plain(d.first or "غير مقروء")
                    + " / "
                    + plain(d.second or "غير مقروء")
                )
        for note in dict.fromkeys(
            (*proposal.photo.reads.first.notes, *proposal.photo.reads.second.notes)
        ):
            lines.append("ملاحظة مطبوعة (مش تعليمات): " + plain(note))
        lines.append("للتعديل: صف 1: الاسم=...؛ القيمة=...؛ الوحدة=... (أو الجرعة=... للدوا)")
    from sanad.scribe.amend import diff_lines

    lines.extend(diff_lines(proposal.amendments))
    for t in proposal.timings:
        if t.item.startswith(("effective:", "checkin:")):
            label = "التغيير يبدأ: " if t.item.startswith("effective:") else "المتابعة المطلوبة: "
            lines.append(
                label
                + arabic_datetime(
                    t.resolved.due_at, proposal.timezone, reference=proposal.created_at
                )
            )
    if proposal.disputed_numbers:
        lines.append("⚠️ " + " أو ".join(proposal.disputed_numbers) + "؟ — محتاج تأكيد")
    heard = numbers_in(proposal.source_text)
    if heard:
        lines.append("الأرقام: " + "، ".join(heard))
    if candidate.patient.age:
        lines.append("العمر: " + supported_text(candidate.patient.age, proposal))
    if candidate.patient.sex:
        lines.append("النوع: " + ("ذكر" if candidate.patient.sex == "male" else "أنثى"))
    if candidate.patient.identifiers:
        lines.append(
            "أرقام التعريف: "
            + "، ".join(supported_text(v, proposal) for v in candidate.patient.identifiers)
        )
    if proposal.choices and not proposal.selected_patient_id and not proposal.creating_patient:
        lines.append("مين المريض؟")
        for choice in proposal.choices:
            details = [plain(choice.display_name)]
            if choice.age:
                details.append(plain(choice.age))
            if choice.sex:
                details.append("ذكر" if choice.sex == "male" else "أنثى")
            details.append(
                "آخر نشاط: "
                + arabic_datetime(
                    choice.updated_at, proposal.timezone, reference=proposal.created_at
                )
            )
            lines.append("• " + "، ".join(details))
    clarification = proposal.intent == "unclear" or any(
        i.code == "batch_too_large" for i in proposal.issues
    )
    if not clarification:
        for i, order in enumerate(candidate.orders):
            fields = [
                supported_text(v, proposal)
                for v in (
                    order.drug,
                    order.dose,
                    order.frequency,
                    order.route,
                    order.timing,
                    order.duration,
                )
                if v
            ]
            first = " ".join(fields[:2])
            suffix = "، " + "، ".join(fields[2:]) if len(fields) > 2 else ""
            line = "• " + first + suffix + " (" + _ACTIONS[order.action] + ")"
            if proposal.blocked(f"order:{i}"):
                line += " — محتاج تأكيد"
            lines.append(line)
        for i, mission in enumerate(candidate.missions):
            lines.append(
                "• "
                + supported_text(mission.text, proposal)
                + (" — محتاج تأكيد" if proposal.blocked(f"mission:{i}") else "")
            )
            timing = next((t.resolved for t in proposal.timings if t.item == f"mission:{i}"), None)
            if timing and not any(
                issue.item == f"mission:{i}"
                and issue.code in {"unsupported_number", "disputed_number"}
                for issue in proposal.issues
            ):
                due = arabic_datetime(timing.due_at, timing.timezone, reference=proposal.created_at)
                escalation = arabic_datetime(
                    timing.escalation_at, timing.timezone, reference=proposal.created_at
                )
                expression = plain(timing.original_time_expression or "")
                reason = (
                    (
                        "صريح"
                        if re.search(r"\d{4}-\d{2}-\d{2}", expression)
                        else "صريح: " + expression
                    )
                    if timing.due_source == "doctor"
                    else "افتراضي "
                    + str(
                        int((timing.due_at - timing.timing_anchor.instant).total_seconds() / 86400)
                    )
                    + " يوم؛ الموعد الافتراضي لنوع المهمة"
                )
                lines.append(f"  الموعد: {due} ({reason})")
                lines.append(f"  لو متعملش هبلّغك: {escalation}")
        for i, order in enumerate(candidate.orders):
            timing = next((t.resolved for t in proposal.timings if t.item == f"order:{i}"), None)
            if order.action == "start" and timing and not proposal.blocked(f"order:{i}"):
                local = arabic_datetime(
                    timing.due_at, timing.timezone, reference=proposal.created_at
                )
                lines.append(
                    f"تأكيد بداية {plain(order.drug)}: {local}"
                    " (افتراضي 3 أيام)؛ لو متأكدش هبلّغك في نفس الموعد."
                )
                lines.append(
                    "متابعة اليوم الثالث من تاريخ البداية اللي المريض يبلّغنا بيه؛ "
                    "التأكيد هنا مش دليل إنه بدأ."
                )
        for i, fact in enumerate(candidate.facts):
            if not any(
                issue.item == f"fact:{i}" and issue.code == "unsafe_text"
                for issue in proposal.issues
            ):
                lines.append(_FACTS[fact.category] + ": " + supported_text(fact.text, proposal))
        for i, alert in enumerate(candidate.alerts):
            if not any(
                issue.item == f"alert:{i}" and issue.code == "unsafe_text"
                for issue in proposal.issues
            ):
                lines.append("بلّغني لو: " + supported_text(alert, proposal))
    for i, ambiguity in enumerate(candidate.ambiguities):
        if not any(
            issue.item == f"ambiguity:{i}" and issue.code == "unsafe_text"
            for issue in proposal.issues
        ):
            lines.append("محتاج توضيح: " + supported_text(ambiguity, proposal))
    for issue in proposal.issues:
        if issue.code == "unassigned_number":
            lines.extend(f"سمعت الرقم {n} ومش عارف يتحط فين" for n in issue.numbers)
    blocked = [i for i in proposal.issues if i.blocked]
    if blocked:
        lines.append("مش هيتسجل:")
        lines.extend("• " + REASONS[code] for code in dict.fromkeys(i.code for i in blocked))
    if proposal.supersedes_id:
        lines.append("الكارت ده بدّل الكارت اللي قبله؛ الأزرار القديمة مش شغالة.")
    lines.append("صالح لمدة 30 دقيقة")
    text = wording.render("scribe_card", body="\n".join(lines))
    cap = DRAFT_SCRIBE_POLICY.card_max_chars
    if len(text) <= cap:
        return (text,)
    if len(text) > 2 * cap:
        if any(i.code == "batch_too_large" for i in proposal.issues):
            return (
                "المريض: مين المريض؟\nمش هيتسجل:\n• "
                + REASONS["batch_too_large"]
                + "\nصالح لمدة 30 دقيقة",
            )
        # Caller records batch_too_large and re-renders before offering confirmation.
        return (text,)
    cut = text.rfind("\n", len(text) - cap, cap + 1)
    if cut < 0:
        cut = cap
    return text[:cut], text[cut:].lstrip("\n")
