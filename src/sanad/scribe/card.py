"""Plain Egyptian Arabic cards rendered solely from checked structured fields."""

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from sanad.channels.telegram import wording
from sanad.media.numbers import numbers_in
from sanad.scribe.extract import REQUEST_MISSING_QUESTION, OrderCandidate
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.scribe.proposal import Proposal

REASONS = {
    "request_missing": REQUEST_MISSING_QUESTION,
    "clinical_unclear": "الصياغة الإنجليزية محتاجة مراجعة.",
    "fact_medication": "دوا حالي محتاج تأكيد كأمر استمرار.",
    "correction_unclear": "التعديل محتاج توضيح للبند المقصود.",
    "reader_disagreement": "قراءتين مختلفتين؛ اختار القراءة أو عدّل البند.",
    "shifted_rows": "الأرقام ممكن تكون متزحزحة؛ عدّل كل صف أو ابعت الصورة تاني.",
    "order_missing": "مفيش أمر مسجل بالدوا ده",
    "document_unclear": "الصورة محتاجة توضيح؛ ابعت كل مستند لوحده.",
    "unsupported_number": "فيه رقم مش موجود في كلامك؛ عدّل البند.",
    "dose_unclear": "الجرعة المركبة محتاجة تأكيد.",
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
    if local.minute:
        clock += f":{local.minute:02}"
    return (
        f"{_WEEKDAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]}{year}، "
        f"{clock} {period}"
    )


def plain(text: str) -> str:
    text = re.sub(r"\b(?:null|none)\b", "", text, flags=re.I)
    return " ".join("".join(c for c in text if not unicodedata.category(c).startswith("C")).split())


def supported_text(text: str, proposal: Proposal) -> str:
    """Do not read a hallucinated number back as if it came from the doctor."""
    from sanad.scribe.clinical import normalize_units

    text = normalize_units(plain(text)).translate(
        str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    )
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
    if not proposal.photo:
        return render_dictation(proposal)
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
        if candidate.orders or candidate.facts:
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


_BUTTONS = "✅ تمام | ✏️ تعديل | ❌ إلغاء"


def unsupported_order_fields(proposal: Proposal, order: OrderCandidate) -> dict[str, str]:
    """Keep the stored candidate and numeric blocks; omit unsupported display fields."""
    from sanad.scribe.amend import FIELDS, instruction_line

    present = set(
        numbers_in(
            proposal.source_text
            + "\n"
            + "\n".join(instruction_line(a.old) for a in proposal.amendments if a.old)
        )
    )
    return {
        field: plain(value)
        for field in (*FIELDS, "effective_expression", "checkin_expression")
        if (value := getattr(order, field)) and not set(numbers_in(value)) <= present
    }


def display_order(proposal: Proposal, order: OrderCandidate) -> OrderCandidate:
    return order.model_copy(update=dict.fromkeys(unsupported_order_fields(proposal, order)))


def medication_line(proposal: Proposal, index: int) -> str:
    order = display_order(proposal, proposal.candidate.orders[index])
    name = supported_text(order.drug, proposal)
    if any(i.item == f"order:{index}" and i.code == "drug_unclear" for i in proposal.issues):
        name += " (؟)" if proposal.names else " (الاسم؟)"
    dose = supported_text(order.dose or "", proposal)
    dose = re.sub(r"\b(?:مجم|مج|مليجرام)\b", "mg", dose)
    frequency = supported_text(order.frequency or "", proposal)
    fields = [frequency] if frequency else []
    fields.extend(
        clean
        for v in (order.route, order.timing, order.duration)
        if v and (clean := supported_text(v, proposal))
    )
    line = name + (" " + dose if dose else "")
    if fields:
        line += ", " + ", ".join(plain(v) for v in fields)
    if order.action != "continue":
        line += " (" + _ACTIONS[order.action] + ")"
    return line


def dictation_questions(proposal: Proposal) -> tuple[str, ...]:
    questions: list[str] = []
    numbers_asked: set[str] = set()
    for issue in proposal.issues:
        if issue.code == "unsupported_number" and issue.item.startswith("order:"):
            order = proposal.candidate.orders[int(issue.item.split(":")[1])]
            fields = unsupported_order_fields(proposal, order)
            questions.extend(
                f'سمعت "{value}" بس مش لاقي الرقم ده في كلامك' for value in fields.values()
            )
            if not fields:
                questions.append(REASONS[issue.code])
        elif issue.code == "disputed_number":
            for n in issue.numbers:
                if n in numbers_asked:
                    continue
                numbers_asked.add(n)
                if issue.item.startswith("fact:"):
                    fact = proposal.candidate.facts[int(issue.item.split(":")[1])]
                    if re.search(
                        r"\bEF\b",
                        fact.text
                        + " "
                        + " ".join(
                            n.latin for n in proposal.names if n.item == issue.item and n.verified
                        ),
                        re.I,
                    ):
                        questions.append(f"سمعت {n}، ده الـ EF؟")
                        continue
                questions.append(f'سمعت "{n}"، الرقم ده صح؟')
        elif issue.code == "unassigned_number":
            for n in issue.numbers:
                if n not in numbers_asked:
                    numbers_asked.add(n)
                    questions.append(f'سمعت "{n}"، ده يخص إيه؟')
        elif issue.question:
            questions.append(plain(issue.question))
        elif issue.code == "dose_missing" and issue.item.startswith("order:"):
            order = proposal.candidate.orders[int(issue.item.split(":")[1])]
            if not any(i.item == issue.item and i.code == "dose_unclear" for i in proposal.issues):
                questions.append(f'جرعة "{plain(order.drug)}" إيه؟')
        elif issue.code == "drug_unclear" and any(
            i.item == issue.item and i.question for i in proposal.issues
        ):
            continue
        elif (
            issue.code in {"multiple_patients", "clarification"} and proposal.candidate.ambiguities
        ):
            continue
        elif issue.code == "patient_missing" and (
            proposal.choices or not proposal.candidate.patient.name_as_spoken
        ):
            # The patient line/selection already asks this question.
            continue
        else:
            questions.append(REASONS[issue.code])
    for number in proposal.disputed_numbers:
        if number not in numbers_asked:
            questions.append(f'سمعت "{number}"، الرقم ده صح؟')
    questions.extend(
        f'سمعت "{supported_text(a, proposal)}"، توضح المقصود؟'
        for i, a in enumerate(proposal.candidate.ambiguities)
        if not any(x.item == f"ambiguity:{i}" and x.code == "unsafe_text" for x in proposal.issues)
    )
    return tuple(dict.fromkeys(questions))


def clinical_line(proposal: Proposal, item: str, spoken: str) -> str:
    # NameReadings are built by code, outside the provider schema. Legacy free
    # clinical_en fields never participate in a rendered line or question.
    kind = "finding" if item.startswith("fact:") else "test"
    fragments = [n for n in proposal.names if n.item == item and n.kind == kind]
    if fragments:
        values = [
            supported_text(n.latin, proposal)
            + (" (؟)" if not n.verified and kind == "finding" else "")
            for n in fragments
        ]
        value = ", ".join(dict.fromkeys(values) if kind == "test" else values)
    else:
        value = supported_text(spoken, proposal)
        if any(i.item == item and i.code == "clinical_unclear" for i in proposal.issues):
            value += " (؟)"
    if item.startswith("fact:") and fragments:
        fact = proposal.candidate.facts[int(item.split(":")[1])]
        value = fact.clinical_kind + ": " + value
    return value


def render_dictation(proposal: Proposal) -> tuple[str, ...]:
    from sanad.domain import DRAFT_POLICY_2026_09, MissionKind
    from sanad.scribe.amend import diff_lines

    candidate = proposal.candidate
    name = proposal.selected_display_name or candidate.patient.name_as_spoken or "مين المريض؟"
    identity = [plain(name) if proposal.selected_display_name else supported_text(name, proposal)]
    if candidate.patient.age:
        identity.append(supported_text(candidate.patient.age, proposal) + " سنة")
    if candidate.patient.sex:
        identity.append("ذكر" if candidate.patient.sex == "male" else "أنثى")
    identity.extend(supported_text(v, proposal) for v in candidate.patient.identifiers)
    lines = [("مريض جديد: " if proposal.creating_patient else "المريض: ") + "، ".join(identity)]
    if proposal.choices and not proposal.selected_patient_id and not proposal.creating_patient:
        for choice in proposal.choices:
            details = [plain(choice.display_name)]
            if choice.age:
                details.append(plain(choice.age))
            if choice.sex:
                details.append("ذكر" if choice.sex == "male" else "أنثى")
            details.append(
                "آخر نشاط: "
                + arabic_datetime(
                    choice.updated_at,
                    proposal.timezone,
                    reference=proposal.created_at,
                )
            )
            lines.append("• " + "، ".join(details))
    oversized = any(i.code == "batch_too_large" for i in proposal.issues)
    if candidate.orders and not oversized:
        lines.append("الأدوية:")
        lines.extend(medication_line(proposal, i) for i in range(len(candidate.orders)))
        lines.extend(
            diff_lines(
                tuple(
                    change.model_copy(update={"new": display_order(proposal, change.new)})
                    for change in proposal.amendments
                )
            )
        )
        for auxiliary in proposal.timings:
            if auxiliary.item.startswith(("effective:", "checkin:")):
                field, index = auxiliary.item.split(":")
                if field + "_expression" in unsupported_order_fields(
                    proposal, candidate.orders[int(index)]
                ):
                    continue
                label = (
                    "التغيير يبدأ: "
                    if auxiliary.item.startswith("effective:")
                    else "المتابعة المطلوبة: "
                )
                lines.append(
                    label
                    + arabic_datetime(
                        auxiliary.resolved.due_at,
                        proposal.timezone,
                        reference=proposal.created_at,
                    )
                )
    required = []
    if not oversized:
        for i, mission in enumerate(candidate.missions):
            line = mission.kind + ": " + clinical_line(proposal, f"mission:{i}", mission.text)
            timing = next((t.resolved for t in proposal.timings if t.item == f"mission:{i}"), None)
            if timing and not any(
                x.item == f"mission:{i}" and x.code in {"unsupported_number", "disputed_number"}
                for x in proposal.issues
            ):
                due = arabic_datetime(timing.due_at, timing.timezone, reference=proposal.created_at)
                escalation = arabic_datetime(
                    timing.escalation_at, timing.timezone, reference=proposal.created_at
                )
                expression = plain(timing.original_time_expression or "")
                if timing.due_source == "doctor":
                    reason = (
                        "صريح"
                        if re.search(r"\d{4}-\d{2}-\d{2}", expression)
                        else "صريح: " + expression
                    )
                elif timing.due_source == "default":
                    days = DRAFT_POLICY_2026_09.default_deadlines[
                        MissionKind(mission.kind)
                    ].offset.days
                    reason = f"افتراضي {days} يوم"
                else:
                    reason = "مقترح: " + plain(timing.due_reason)
                line += f" — الموعد: {due} ({reason})"
                line += (
                    "؛ " if timing.due_at == timing.escalation_at else "\n  "
                ) + f"لو متعملش هبلّغك: {escalation}"
            required.append(line)
        started = []
        for i, order in enumerate(candidate.orders):
            timing = next((t.resolved for t in proposal.timings if t.item == f"order:{i}"), None)
            if order.action == "start" and timing and not proposal.blocked(f"order:{i}"):
                at = arabic_datetime(timing.due_at, timing.timezone, reference=proposal.created_at)
                started.append(
                    f"تأكيد بداية {plain(order.drug)}: {at}؛ لو متأكدش هبلّغك في نفس الموعد."
                )
        required.extend(started)
        if started:
            required.append(
                "متابعة اليوم الثالث من تاريخ البداية اللي المريض يبلّغنا بيه؛ "
                "التأكيد هنا مش دليل إنه بدأ."
            )
    if required:
        lines.extend(("المطلوب:", *required))
    facts = [
        (
            (
                "حساسية: "
                if f.category == "allergy"
                and not any(n.item == f"fact:{i}" for n in proposal.names)
                else "أدوية قديمة: "
                if f.category == "medication_history"
                and not any(n.item == f"fact:{i}" for n in proposal.names)
                else ""
            )
            + clinical_line(proposal, f"fact:{i}", f.text)
        )
        for i, f in enumerate(candidate.facts)
        if not oversized
        and not any(x.item == f"fact:{i}" and x.code == "unsafe_text" for x in proposal.issues)
    ]
    if facts:
        lines.extend(("التاريخ المرضي:", *facts))
    alerts = [
        supported_text(a, proposal)
        for i, a in enumerate(candidate.alerts)
        if not oversized
        and not any(x.item == f"alert:{i}" and x.code == "unsafe_text" for x in proposal.issues)
    ]
    if alerts:
        lines.extend(("بلّغني لو:", *alerts))
    questions = dictation_questions(proposal)
    if questions:
        lines.extend(("محتاج تأكيد:", *questions))
    if proposal.corrected:
        lines.append("عدّلت الكارت حسب كلامك")
    elif proposal.supersedes_id:
        lines.append("الكارت ده بدّل الكارت اللي قبله؛ الأزرار القديمة مش شغالة.")
    lines.extend((_BUTTONS, "صالح 30 دقيقة"))
    text, cap = "\n".join(lines), DRAFT_SCRIBE_POLICY.card_max_chars
    if len(text) <= cap:
        return (text,)
    if len(text) > 2 * cap:
        return (
            (
                "\n".join(
                    (
                        lines[0],
                        "محتاج تأكيد:",
                        REASONS["batch_too_large"],
                        _BUTTONS,
                        "صالح 30 دقيقة",
                    )
                ),
            )
            if oversized
            else (text,)
        )
    cut = text.rfind("\n", len(text) - cap, cap + 1)
    cut = cut if cut >= 0 else cap
    return text[:cut], text[cut:].lstrip("\n")
