"""Plain Egyptian Arabic cards rendered solely from checked structured fields."""

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from sanad.channels.telegram import wording
from sanad.media.numbers import numbers_in
from sanad.scribe.extract import (
    REQUEST_MISSING_QUESTION,
    OrderCandidate,
    PatientCandidate,
    placeholder_ambiguity,
)
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.scribe.proposal import Proposal

REASONS = {
    "request_missing": REQUEST_MISSING_QUESTION,
    "extraction_conflict": "قراءتين مختلفتين؛ وضّح البند.",
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
    "finding": "Finding",
    "complaint": "Complaint",
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
            + " "
            + " ".join(instruction_line(a.old) for a in proposal.amendments if a.old)
        )
    )
    return re.sub(
        r"\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?",
        lambda m: (
            m[0]
            if set(numbers_in(m[0])) <= present
            else "[number not heard]"
            if proposal.language == "en"
            else "[رقم غير مسموع]"
        ),
        text,
    )


PHOTO_LABELS = {
    "who": ("مين المريض؟", "Who is the patient?"),
    "new": ("مريض جديد: ", "New patient: "),
    "patient": ("المريض: ", "Patient: "),
    "heard": ("سمعت:", "I heard:"),
    "read": ("قريت في الصورة:", "I read in the image:"),
    "printed_name": ("الاسم المطبوع (غير مؤكد): ", "Printed name (unverified): "),
    "row": ("صف ", "Row "),
    "document": ("بيانات الصورة", "Image details"),
    "disagreement": (" — ⚠️ قراءتين مختلفتين: ", " — ⚠️ Two different readings: "),
    "printed_note": ("ملاحظة مطبوعة (مش تعليمات): ", "Printed note (not an instruction): "),
    "edit_row": (
        "للتعديل: صف 1: الاسم=...؛ القيمة=...؛ الوحدة=... (أو الجرعة=... للدوا)",
        "To edit: row 1: name=...; value=...; unit=... (or dose=... for medication)",
    ),
    "effective": ("التغيير يبدأ: ", "Change starts: "),
    "checkin": ("المتابعة المطلوبة: ", "Requested check-in: "),
    "or": (" أو ", " or "),
    "question_confirmation": ("؟ — محتاج تأكيد", "? — needs confirmation"),
    "numbers": ("الأرقام: ", "Numbers: "),
    "comma": ("، ", ", "),
    "age": ("العمر: ", "Age: "),
    "sex": ("النوع: ", "Sex: "),
    "male": ("ذكر", "male"),
    "female": ("أنثى", "female"),
    "identifiers": ("أرقام التعريف: ", "Identifiers: "),
    "last_activity": ("آخر نشاط: ", "Last activity: "),
    "confirmation": (" — محتاج تأكيد", " — needs confirmation"),
    "explicit": ("صريح", "explicit"),
    "explicit_expression": ("صريح: ", "explicit: "),
    "default": ("افتراضي ", "default "),
    "days_reason": (" يوم؛ الموعد الافتراضي لنوع المهمة", " days; default for this task type"),
    "due": ("  الموعد: {due} ({reason})", "  Due: {due} ({reason})"),
    "escalation": ("  لو متعملش هبلّغك: {time}", "  If not done I will notify you: {time}"),
    "start_deadline": (
        "تأكيد بداية {drug}: {time} (افتراضي 3 أيام)؛ لو متأكدش هبلّغك في نفس الموعد.",
        "Start confirmation for {drug}: {time} (default 3 days); "
        "if unconfirmed I will notify you at the same time.",
    ),
    "day_three": (
        "متابعة اليوم الثالث من تاريخ البداية اللي المريض يبلّغنا بيه؛ التأكيد هنا مش دليل إنه بدأ.",
        "The day-three check-in follows the start date the patient reports; "
        "confirmation here does not prove the patient started.",
    ),
    "alert": ("بلّغني لو: ", "Notify me if: "),
    "clarify": ("محتاج توضيح: ", "Needs clarification: "),
    "unassigned": (
        "سمعت الرقم {number} ومش عارف يتحط فين",
        "I heard {number}; which item is it for?",
    ),
    "not_recorded": ("مش هيتسجل:", "Not recorded:"),
    "superseded": (
        "الكارت ده بدّل الكارت اللي قبله؛ الأزرار القديمة مش شغالة.",
        "This card replaces the previous card; the old buttons no longer work.",
    ),
    "valid": ("صالح لمدة 30 دقيقة", "Valid for 30 minutes"),
}
PHOTO_FACTS = {
    "patient_report": ("نتيجة في الصورة", "Result in the image"),
    "condition": ("التاريخ المرضي", "History"),
    "allergy": ("حساسية", "Allergy"),
    "history": ("التاريخ المرضي", "History"),
    "medication_history": ("أدوية قديمة", "Previous medication"),
    "finding": ("Finding", "Finding"),
    "complaint": ("Complaint", "Complaint"),
}
PHOTO_ACTIONS = {action: (text, action) for action, text in _ACTIONS.items()}
PHOTO_REASONS = {
    "reader_disagreement": (
        REASONS["reader_disagreement"],
        "The readings differ; choose a reading or edit the item.",
    ),
    "shifted_rows": (
        REASONS["shifted_rows"],
        "Values may be shifted; edit each row or resend the image.",
    ),
    "document_unclear": (
        REASONS["document_unclear"],
        "Please clarify the image; send each document separately.",
    ),
    "unsupported_number": (
        REASONS["unsupported_number"],
        "A proposed number is not in your input; edit the item.",
    ),
}


def _photo_date(instant: datetime, proposal: Proposal, timezone: str | None = None) -> str:
    if proposal.language == "en":
        from sanad.scribe.english import date

        return date(
            instant, proposal.model_copy(update={"timezone": timezone or proposal.timezone})
        )
    return arabic_datetime(instant, timezone or proposal.timezone, reference=proposal.created_at)


def render_card(proposal: Proposal, language: str | None = None) -> tuple[str, ...]:
    # A presentation copy selects today's language without rewriting the saved proposal.
    if language is not None and language != proposal.language:
        proposal = proposal.model_copy(update={"language": language})
    if not proposal.photo:
        return render_dictation(proposal)
    language = proposal.language

    def label(key: str) -> str:
        return PHOTO_LABELS[key][language == "en"]

    candidate = proposal.candidate
    name = proposal.selected_display_name or candidate.patient.name_as_spoken or label("who")
    line = label("new") if proposal.creating_patient else label("patient")
    lines = [
        line + (plain(name) if proposal.selected_display_name else supported_text(name, proposal)),
        label("heard"),
    ]
    if proposal.photo:
        from sanad.scribe.crosscheck import shift_warning

        lines[1] = label("read")
        for printed_name in dict.fromkeys(
            r.printed_identity_hint.text
            for r in (proposal.photo.reads.first, proposal.photo.reads.second)
        ):
            if printed_name:
                lines.append(label("printed_name") + plain(printed_name))
        if proposal.photo.shift_detected:
            lines.append(shift_warning(language))
        if proposal.photo.danger_raised:
            lines.append(wording.label("alerted", language))
        for d in proposal.photo.reads.disagreements:
            if d.field not in proposal.photo.resolved_fields:
                row_label = (
                    (label("row") + str(int(d.field.split(".")[1]) + 1))
                    if d.field.startswith("items.")
                    else label("document")
                )
                lines.append(
                    row_label
                    + label("disagreement")
                    + plain(d.first or wording.label("unreadable", language))
                    + " / "
                    + plain(d.second or wording.label("unreadable", language))
                )
        for note in dict.fromkeys(
            (*proposal.photo.reads.first.notes, *proposal.photo.reads.second.notes)
        ):
            lines.append(label("printed_note") + plain(note))
        if candidate.orders or candidate.facts:
            lines.append(label("edit_row"))
    from sanad.scribe.amend import diff_lines

    lines.extend(diff_lines(proposal.amendments, language))
    for t in proposal.timings:
        if t.item.startswith(("effective:", "checkin:")):
            timing_label = (
                label("effective") if t.item.startswith("effective:") else label("checkin")
            )
            lines.append(timing_label + _photo_date(t.resolved.due_at, proposal))
    if proposal.disputed_numbers:
        lines.append(
            "⚠️ " + label("or").join(proposal.disputed_numbers) + label("question_confirmation")
        )
    heard = numbers_in(proposal.source_text)
    if heard:
        lines.append(label("numbers") + label("comma").join(heard))
    if candidate.patient.age:
        lines.append(label("age") + supported_text(candidate.patient.age, proposal))
    if candidate.patient.sex:
        lines.append(label("sex") + label("male" if candidate.patient.sex == "male" else "female"))
    if candidate.patient.identifiers:
        lines.append(
            label("identifiers")
            + label("comma").join(
                supported_text(v, proposal) for v in candidate.patient.identifiers
            )
        )
    if proposal.choices and not proposal.selected_patient_id and not proposal.creating_patient:
        lines.append(label("who"))
        for choice in proposal.choices:
            details = [plain(choice.display_name)]
            if choice.age:
                details.append(plain(choice.age))
            if choice.sex:
                details.append(label("male" if choice.sex == "male" else "female"))
            details.append(label("last_activity") + _photo_date(choice.updated_at, proposal))
            lines.append("• " + label("comma").join(details))
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
            suffix = label("comma") + label("comma").join(fields[2:]) if len(fields) > 2 else ""
            action = PHOTO_ACTIONS[order.action][language == "en"]
            line = "• " + first + suffix + " (" + action + ")"
            if proposal.blocked(f"order:{i}"):
                line += label("confirmation")
            lines.append(line)
        for i, mission in enumerate(candidate.missions):
            lines.append(
                "• "
                + supported_text(mission.text, proposal)
                + (label("confirmation") if proposal.blocked(f"mission:{i}") else "")
            )
            timing = next((t.resolved for t in proposal.timings if t.item == f"mission:{i}"), None)
            if timing and not any(
                issue.item == f"mission:{i}"
                and issue.code in {"unsupported_number", "disputed_number"}
                for issue in proposal.issues
            ):
                due = _photo_date(timing.due_at, proposal, timing.timezone)
                escalation = _photo_date(timing.escalation_at, proposal, timing.timezone)
                expression = plain(timing.original_time_expression or "")
                reason = (
                    (
                        label("explicit")
                        if re.search(r"\d{4}-\d{2}-\d{2}", expression)
                        else label("explicit_expression") + expression
                    )
                    if timing.due_source == "doctor"
                    else label("default")
                    + str(
                        int((timing.due_at - timing.timing_anchor.instant).total_seconds() / 86400)
                    )
                    + label("days_reason")
                )
                lines.append(label("due").format(due=due, reason=reason))
                lines.append(label("escalation").format(time=escalation))
        for i, order in enumerate(candidate.orders):
            timing = next((t.resolved for t in proposal.timings if t.item == f"order:{i}"), None)
            if order.action == "start" and timing and not proposal.blocked(f"order:{i}"):
                local = _photo_date(timing.due_at, proposal, timing.timezone)
                lines.append(label("start_deadline").format(drug=plain(order.drug), time=local))
                lines.append(label("day_three"))
        for i, fact in enumerate(candidate.facts):
            if not any(
                issue.item == f"fact:{i}" and issue.code == "unsafe_text"
                for issue in proposal.issues
            ):
                from sanad.scribe.crosscheck import lab_text

                fact_text = (
                    lab_text(fact.lab, language) if fact.lab and language == "en" else fact.text
                )
                lines.append(
                    PHOTO_FACTS[fact.category][language == "en"]
                    + ": "
                    + supported_text(fact_text, proposal)
                )
        for i, alert in enumerate(candidate.alerts):
            if not any(
                issue.item == f"alert:{i}" and issue.code == "unsafe_text"
                for issue in proposal.issues
            ):
                lines.append(label("alert") + supported_text(alert, proposal))
    for i, ambiguity in enumerate(candidate.ambiguities):
        if not any(
            issue.item == f"ambiguity:{i}" and issue.code == "unsafe_text"
            for issue in proposal.issues
        ):
            lines.append(label("clarify") + supported_text(ambiguity, proposal))
    for issue in proposal.issues:
        if issue.code == "unassigned_number":
            lines.extend(label("unassigned").format(number=n) for n in issue.numbers)
    blocked = [i for i in proposal.issues if i.blocked]
    if blocked:
        from sanad.scribe.english import REASONS as ENGLISH_REASONS

        lines.append(label("not_recorded"))
        lines.extend(
            "• " + PHOTO_REASONS.get(code, (REASONS[code], ENGLISH_REASONS[code]))[language == "en"]
            for code in dict.fromkeys(i.code for i in blocked)
        )
    if proposal.supersedes_id:
        lines.append(label("superseded"))
    lines.append(label("valid"))
    text = wording.render("scribe_card", language, body="\n".join(lines))
    cap = DRAFT_SCRIBE_POLICY.card_max_chars
    if len(text) <= cap:
        return (text,)
    if len(text) > 2 * cap:
        if any(i.code == "batch_too_large" for i in proposal.issues):
            return (
                label("patient")
                + label("who")
                + "\n"
                + label("not_recorded")
                + "\n• "
                + (ENGLISH_REASONS if language == "en" else REASONS)["batch_too_large"]
                + "\n"
                + label("valid"),
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
    dose = supported_text(order.dose or "", proposal)
    dose = re.sub(r"\b(?:مجم|مج|مليجرام)\b", "mg", dose)
    from sanad.scribe.names import normalize

    frequency = supported_text(order.frequency or "", proposal)
    if normalize(frequency) not in normalize(proposal.source_text):
        frequency = ""
    fields = [frequency] if frequency else []
    fields.extend(
        clean
        for v in (order.route, order.timing, order.duration)
        if v and (clean := supported_text(v, proposal))
    )
    line = name + (" " + dose if dose else "")
    if fields:
        line += ", " + ", ".join(plain(v) for v in fields)
    if proposal.language == "en" and not proposal.photo:
        change = next(
            (a for a in proposal.amendments if a.item == f"order:{index}" and a.old and not a.noop),
            None,
        )
        if change and change.old:
            previous = " ".join(v for v in (change.old.drug, change.old.dose) if v)
            line = supported_text(previous, proposal) + " → " + line
        if order.action != "continue":
            line += " (" + order.action + ")"
    elif order.action != "continue":
        line += " (" + _ACTIONS[order.action] + ")"
    return line


def consumed_question_numbers(proposal: Proposal) -> set[str]:
    """Rendered request counts and quoted test words need no extra global question."""
    from sanad.scribe.monitoring import request_counts, spoken_counts

    consumed = set().union(
        *(
            request_counts(clinical_line(proposal, f"mission:{i}", mission.text))
            for i, mission in enumerate(proposal.candidate.missions)
        )
    ) & request_counts(proposal.source_text)
    for issue in proposal.issues:
        if issue.code == "clinical_unclear" and issue.field == "analyte":
            for fragment in re.findall(r'"([^"\n]+)"', issue.question or ""):
                consumed.update(spoken_counts(re.sub(r"[,،;؛]", " ", fragment)))
    return consumed


def dictation_questions(proposal: Proposal) -> tuple[str, ...]:
    if proposal.language == "en" and not proposal.photo:
        from sanad.scribe.english import questions as english_questions

        return english_questions(proposal)
    questions: list[str] = []
    consumed = consumed_question_numbers(proposal)
    numbers_asked = {
        n for i in proposal.issues if i.code == "extraction_conflict" for n in i.numbers
    }
    for issue in proposal.issues:
        before = len(questions)
        if (
            issue.code == "unsupported_number"
            and issue.numbers
            and set(issue.numbers) <= numbers_asked
        ):
            # The merge question quotes both readings; numeric blocking remains.
            continue
        if issue.code in {"drug_unclear", "clinical_unclear"}:
            from sanad.scribe.clinical import TERM_QUESTION

            questions.append(
                issue.question if issue.field == "analyte" and issue.question else TERM_QUESTION
            )
            continue
        if any(
            q.item == issue.item and q.code == "extraction_conflict" and q.field == "dose"
            for q in proposal.issues
        ) and issue.code in {"dose_missing", "dose_unclear"}:
            continue
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
                if n not in numbers_asked and n not in consumed:
                    numbers_asked.add(n)
                    questions.append(f'سمعت "{n}"، ده يخص إيه؟')
        elif issue.question:
            question = plain(issue.question)
            questions.append(question)
        elif issue.code == "dose_missing" and issue.item.startswith("order:"):
            order = proposal.candidate.orders[int(issue.item.split(":")[1])]
            if not any(i.item == issue.item and i.code == "dose_unclear" for i in proposal.issues):
                question = f'جرعة "{plain(order.drug)}" إيه؟'
                questions.append(question)
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
        if issue.item in proposal.single_source:
            questions[before:] = [q + " سمعتها مرة واحدة" for q in questions[before:]]
    for number in proposal.disputed_numbers:
        if number not in numbers_asked and number not in consumed:
            questions.append(f'سمعت "{number}"، الرقم ده صح؟')
    questions.extend(
        f'سمعت "{supported_text(a, proposal)}"، توضح المقصود؟'
        for i, a in enumerate(proposal.candidate.ambiguities)
        if not placeholder_ambiguity(a)
        and not any(x.item == f"ambiguity:{i}" and x.code == "unsafe_text" for x in proposal.issues)
    )
    return tuple(dict.fromkeys(questions))


def clinical_line(proposal: Proposal, item: str, spoken: str) -> str:
    task_marker = ""
    if item.startswith("mission:"):
        mission = proposal.candidate.missions[int(item.split(":")[1])]
        if mission.kind == "TASK" and not proposal.photo and proposal.language != "en":
            from sanad.concierge.tasks import marker

            task_marker = marker(mission.text, proposal.language)
        if mission.kind == "TASK" and mission.clinical_en:
            return supported_text(mission.clinical_en, proposal) + task_marker
    # NameReadings are built by code, outside the provider schema. Legacy free
    # clinical_en fields never participate in a rendered line or question.
    kind = "finding" if item.startswith("fact:") else "test"
    fragments = [n for n in proposal.names if n.item == item and n.kind == kind]
    if fragments:
        values = [supported_text(n.latin, proposal) for n in fragments]
        value = ", ".join(dict.fromkeys(values) if kind == "test" else values)
    elif item.startswith("mission:") and mission.kind == "TEST" and not proposal.photo:
        # An unanchored analyte is only a question, never an instruction.
        value = ""
    else:
        value = supported_text(spoken, proposal)
    if item.startswith("fact:"):
        fact = proposal.candidate.facts[int(item.split(":")[1])]
        from sanad.scribe.terms import fact_kind

        prefix = fact_kind(fact.category, next((n.latin for n in fragments if n.verified), ""))
        # A standalone ECG/Echo label is carried by the fixed prefix.
        if len(fragments) > 1 and fragments[0].latin.casefold() == prefix.casefold():
            value = ", ".join(supported_text(n.latin, proposal) for n in fragments[1:])
        value = prefix + ": " + value
    return value + task_marker


def render_dictation(proposal: Proposal) -> tuple[str, ...]:
    if proposal.language == "en":
        from sanad.scribe.english import render

        return render(proposal)
    from sanad.domain import DRAFT_POLICY_2026_09, MissionKind
    from sanad.scribe.amend import diff_lines

    candidate = proposal.candidate
    name = proposal.selected_display_name or candidate.patient.name_as_spoken or "مين المريض؟"
    identity = [plain(name) if proposal.selected_display_name else supported_text(name, proposal)]
    if candidate.patient.age:
        age = PatientCandidate.age_without_repeated_year_unit(candidate.patient.age) or ""
        identity.append(supported_text(age, proposal) + " سنة")
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
    if candidate.orders:
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
                    "صريح" if re.search(r"\d{4}-\d{2}-\d{2}", expression) else "صريح: " + expression
                )
            elif timing.due_source == "default":
                days = DRAFT_POLICY_2026_09.default_deadlines[MissionKind(mission.kind)].offset.days
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
            started.append(f"تأكيد بداية {plain(order.drug)}: {at}؛ لو متأكدش هبلّغك في نفس الموعد.")
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
        if not any(x.item == f"fact:{i}" and x.code == "unsafe_text" for x in proposal.issues)
    ]
    if facts:
        limit = (
            DRAFT_SCRIBE_POLICY.history_lines_max
            if oversized and not proposal.editing
            else len(facts)
        )
        lines.extend(("التاريخ المرضي:", *facts[:limit]))
        if len(facts) > limit:
            lines.append(f"… ({len(facts) - limit} بنود إضافية، اضغط تعديل لعرضها)")
    alerts = [
        supported_text(a, proposal)
        for i, a in enumerate(candidate.alerts)
        if not any(x.item == f"alert:{i}" and x.code == "unsafe_text" for x in proposal.issues)
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
    text = "\n".join(lines)
    if (
        not oversized
        and not proposal.editing
        and len(text) > DRAFT_SCRIBE_POLICY.card_max_chars
        and len(facts) > DRAFT_SCRIBE_POLICY.history_lines_max
    ):
        from sanad.scribe.extract import ProposalIssue

        return render_dictation(
            proposal.model_copy(
                update={
                    "issues": (
                        *proposal.issues,
                        ProposalIssue(item="all", code="batch_too_large", blocked=False),
                    )
                }
            )
        )
    return split_card(text)


def split_card(text: str) -> tuple[str, ...]:
    """Keep every visible instruction, including an oversized confirmation batch."""
    cap = DRAFT_SCRIBE_POLICY.card_max_chars
    parts = []
    while len(text) > cap:
        cut = text.rfind("\n", 0, cap + 1)
        cut = cut if cut > 0 else cap
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return (*parts, text) if text else tuple(parts)
