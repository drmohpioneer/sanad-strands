"""English doctor presentation over the same checked, durable proposal."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.scribe.proposal import Proposal, card_footer

REASONS = {
    "request_missing": (
        "I heard a request for tests or an examination; which items did you request?"
    ),
    "extraction_conflict": "The two readings differ; please clarify the item.",
    "clinical_unclear": "Please clarify this item.",
    "drug_unclear": "Please confirm the drug name as heard.",
    "fact_medication": "Should I record the current medication as continue?",
    "correction_unclear": "Which item does your correction refer to?",
    "reader_disagreement": "The readings differ; please edit the item.",
    "shifted_rows": "The values may be shifted between rows; please edit them.",
    "order_missing": "There is no current order for this drug; please clarify the change.",
    "document_unclear": "Please clarify the document.",
    "unsupported_number": "A proposed number was not in your dictation; please edit the item.",
    "dose_unclear": "Please confirm the compound dose.",
    "dose_missing": "What dose did you intend?",
    "disputed_number": "Please confirm the number heard.",
    "amendment_pending_09b": "This medication amendment needs review.",
    "monitor_start_past": "The start date has passed. When should it start?",
    "timing_unclear": "Please specify the intended deadline.",
    "patient_missing": "Who is the patient?",
    "multiple_patients": "Please send each patient's instructions separately.",
    "unsafe_text": "This item needs review because of a danger signal.",
    "empty_item": "Please clarify the empty item.",
    "batch_too_large": "This batch exceeds the confirmation limit; edit it into smaller batches.",
    "clarification": "Please clarify the patient and instructions.",
    "duplicate_order": "There is more than one order for this drug; please clarify one order.",
    "unassigned_number": "Which item does the number heard belong to?",
}


class IdentityLine(BaseModel):
    """Already sanitized identity components, in their original display order."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    prefix: str
    parts: tuple[str, ...]


class ChoiceLine(BaseModel):
    """Already sanitized choice components, including its formatted activity date."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    parts: tuple[str, ...]


class CardView(BaseModel):
    """English card sections; None preserves an absent alert header."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    heading: IdentityLine
    choices: tuple[ChoiceLine, ...]
    medications: tuple[str, ...]
    requested: tuple[str, ...]
    history: tuple[str, ...]
    history_hidden: int
    alerts: tuple[str, ...] | None
    questions: tuple[str, ...]
    notices: tuple[str, ...]
    buttons: str
    validity: str


def date(instant: datetime, proposal: Proposal) -> str:
    local = instant.astimezone(ZoneInfo(proposal.timezone))
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    year = (
        f" {local.year}"
        if local.year != proposal.created_at.astimezone(ZoneInfo(proposal.timezone)).year
        else ""
    )
    return f"{weekdays[local.weekday()]} {local.day} {months[local.month - 1]}{year}, {local:%H:%M}"


def _bounded_quotes(question: str) -> str:
    def clip(match: re.Match[str]) -> str:
        text = match[1]
        if len(text) <= 120:
            return match[0]
        prefix = text[:119]
        boundaries = list(re.finditer(r"\s+", prefix))
        if boundaries and not text[119].isspace():
            prefix = prefix[: boundaries[-1].start()]
        return '"' + prefix.rstrip() + '…"'

    return re.sub(r'"([^"\n]*)"', clip, question)


def questions(proposal: Proposal) -> tuple[str, ...]:
    from sanad.scribe.card import consumed_question_numbers, plain, unsupported_order_fields
    from sanad.scribe.extract import anchored_ambiguity, placeholder_ambiguity

    result: list[str] = []
    consumed = consumed_question_numbers(proposal)
    asked = {n for i in proposal.issues if i.code == "extraction_conflict" for n in i.numbers}
    for issue in proposal.issues:
        if issue.code == "monitor_start_past":
            match = re.search(r"\d{4}-\d{2}-\d{2}", issue.question or "")
            day = match[0] if match else ""
            result.append(
                f"The start date {day} has passed. When should it start?"
                if day
                else REASONS[issue.code]
            )
            continue
        before = len(result)
        if issue.field == "verification" and issue.question:
            result.append(plain(issue.question))
            continue
        code, item = issue.code, issue.item
        if code == "request_missing" and issue.field == "task":
            result.append(
                "I heard a monitoring request missing from the card; what should I record?"
            )
            continue
        if code == "clinical_unclear":
            if issue.question and (issue.field == "analyte" or issue.blocked):
                result.append(plain(issue.question))
            elif issue.blocked:
                result.append(REASONS[code])
            continue
        order = (
            proposal.candidate.orders[int(item.split(":")[1])]
            if item.startswith("order:")
            else None
        )
        if code == "unsupported_number" and issue.numbers and set(issue.numbers) <= asked:
            continue
        if code in {"dose_missing", "dose_unclear"} and any(
            i.item == item and i.code == "extraction_conflict" and i.field == "dose"
            for i in proposal.issues
        ):
            continue
        if code in {"unassigned_number", "disputed_number"}:
            for n in issue.numbers:
                if n not in asked and not (code == "unassigned_number" and n in consumed):
                    asked.add(n)
                    result.append(
                        f'I heard "{n}"; which item does it belong to?'
                        if code == "unassigned_number"
                        else f'I heard "{n}"; is that correct?'
                    )
        elif code == "extraction_conflict":
            if item == "patient" and issue.alternatives:
                readings = " and as ".join(f'"{plain(v)}"' for v in issue.alternatives)
                result.append(f"I heard the patient as {readings}; which is right?")
                continue
            alternatives = tuple(
                dict.fromkeys(
                    v
                    for v in re.findall(r'"([^"\n]+)"', issue.question or "")
                    if v.casefold() not in {"null", "none"}
                )
            )
            quoted = " / ".join(
                f'"{plain(v) if v != "غير مذكور" else "not supplied"}"' for v in alternatives
            )
            subject = f" for {plain(order.drug)}" if order else ""
            result.append(
                f"I heard {quoted}{subject}; which {issue.field or 'reading'} did you mean?"
                if quoted
                else f"Please confirm the {issue.field or 'reading'}{subject}."
            )
        elif code == "dose_missing" and order:
            if not any(i.item == item and i.code == "dose_unclear" for i in proposal.issues):
                result.append(f"What dose of {plain(order.drug)} did you intend?")
        elif code == "dose_unclear" and order:
            alternatives = tuple(re.findall(r'"([^"\n]+)"', issue.question or ""))
            heard = alternatives[0] if alternatives else order.dose or order.drug
            result.append(
                f'I heard "{plain(heard)}" for {plain(order.drug)}; which dose did you mean?'
            )
        elif code == "unsupported_number" and order:
            values = unsupported_order_fields(proposal, order)
            result.extend(
                f'I heard "{v}" in the proposed {field}, '
                "but that number is absent from your dictation."
                for field, v in values.items()
            )
            if not values:
                result.append(REASONS[code])
        elif code == "patient_missing" and (
            proposal.choices or not proposal.candidate.patient.name_as_spoken
        ):
            continue
        else:
            result.append(REASONS[code])
        if item in proposal.single_source:
            result[before:] = [q + " Heard once." for q in result[before:]]
    result.extend(
        f'I heard "{n}"; is that correct?'
        for n in proposal.disputed_numbers
        if n not in asked and n not in consumed
    )
    result.extend(
        (
            f'I heard "{plain(a)}"; please clarify.'
            if anchored_ambiguity(a, proposal.source_text)
            else REASONS["clarification"]
        )
        for i, a in enumerate(proposal.candidate.ambiguities)
        if not placeholder_ambiguity(a)
        and not any(x.item == f"ambiguity:{i}" and x.code == "unsafe_text" for x in proposal.issues)
    )
    from sanad.scribe.grounding import unique_questions

    # Deduplicate against full stored issues before clipping display-only quotes.
    return tuple(_bounded_quotes(q) for q in unique_questions(result, proposal))


def build_view(proposal: Proposal) -> CardView:
    from sanad.domain import DRAFT_POLICY_2026_09, MissionKind
    from sanad.scribe.card import (
        clinical_line,
        history_lines,
        medication_line,
        plain,
        supported_text,
    )
    from sanad.scribe.grounding import permits

    c = proposal.candidate
    name = proposal.selected_display_name or c.patient.name_as_spoken or "Who is the patient?"
    identity = [plain(name) if proposal.selected_display_name else supported_text(name, proposal)]
    identity.extend(
        supported_text(v, proposal)
        for v in (c.patient.age, c.patient.sex, *c.patient.identifiers)
        if v
    )
    heading = IdentityLine(
        prefix="New patient: " if proposal.creating_patient else "Patient: ",
        parts=tuple(identity),
    )
    choices: tuple[ChoiceLine, ...] = ()
    if proposal.choices and not proposal.selected_patient_id and not proposal.creating_patient:
        choices = tuple(
            ChoiceLine(
                parts=tuple(
                    v
                    for v in (
                        (
                            "Did you mean " + plain(p.display_name) + "?"
                            if proposal.invitation_requested
                            and len(proposal.choices) == 1
                            and p.score < 3
                            else plain(p.display_name)
                        ),
                        p.age,
                        p.sex,
                        p.headline,
                        "last activity: " + date(p.updated_at, proposal),
                    )
                    if v
                )
            )
            for p in proposal.choices
        )
    medications = tuple(
        medication_line(proposal, i)
        for i in range(len(c.orders))
        if not proposal.blocked(f"order:{i}")
    )
    blocked_lines = [
        medication_line(proposal, i) for i in range(len(c.orders)) if proposal.blocked(f"order:{i}")
    ]
    requested: list[str] = []
    for i, mission in enumerate(c.missions):
        if proposal.evidence_fingerprint and not permits(proposal, f"mission:{i}", "text"):
            continue
        line = mission.kind + ": " + clinical_line(proposal, f"mission:{i}", mission.text)
        if mission.kind == "TASK" and not proposal.photo:
            from sanad.concierge.tasks import marker

            line += marker(mission.text, "en")
        if mission.kind == "MONITOR":
            if not permits(proposal, f"mission:{i}", "schedule:en"):
                continue
            from sanad.scribe.monitoring import card_line

            line = card_line(mission.text, proposal.created_at, proposal.timezone, "en")
        timing = next((t.resolved for t in proposal.timings if t.item == f"mission:{i}"), None)
        if timing and not any(
            x.item == f"mission:{i}" and x.code in {"unsupported_number", "disputed_number"}
            for x in proposal.issues
        ):
            default_days = DRAFT_POLICY_2026_09.default_deadlines[
                MissionKind(mission.kind)
            ].offset.days
            reason = (
                "explicit: " + plain(timing.original_time_expression or "")
                if timing.due_source == "doctor"
                else f"default {default_days} days"
                if timing.due_source == "default"
                else "suggested deadline"
            )
            line += (
                f": due {date(timing.due_at, proposal)} ({reason}); "
                f"if not done I will notify you: {date(timing.escalation_at, proposal)}"
            )
        if proposal.blocked(f"mission:{i}"):
            blocked_lines.append(line)
        else:
            requested.append(line)
    for t in proposal.timings:
        if not permits(proposal, t.item, "deadline"):
            continue
        family, index = t.item.split(":")
        if family in {"effective", "checkin"}:
            if proposal.blocked("order:" + index):
                continue
            from sanad.scribe.card import unsupported_order_fields

            if family + "_expression" not in unsupported_order_fields(
                proposal, c.orders[int(index)]
            ):
                requested.append(
                    ("Change starts: " if family == "effective" else "Requested follow-up: ")
                    + date(t.resolved.due_at, proposal)
                )
        elif (
            family == "order"
            and c.orders[int(index)].action == "start"
            and not proposal.blocked(t.item)
        ):
            requested.append(
                f"Confirm start of {plain(c.orders[int(index)].drug)}: "
                f"{date(t.resolved.due_at, proposal)} (default 3 days); "
                "if unconfirmed I will notify you at the same time."
            )
    if any(
        t.item.startswith("order:")
        and c.orders[int(t.item.split(":")[1])].action == "start"
        and not proposal.blocked(t.item)
        for t in proposal.timings
    ):
        requested.append(
            "Day-three follow-up runs from the start date reported by the patient; "
            "confirmation here does not establish that treatment started."
        )
    blocked_lines.extend(
        line
        for i, f in enumerate(c.facts)
        if proposal.blocked(f"fact:{i}")
        and not any(x.item == f"fact:{i}" and x.code == "unsafe_text" for x in proposal.issues)
        for line in history_lines(proposal, f"fact:{i}", f.text)
    )
    facts = [
        line
        for i, f in enumerate(c.facts)
        if not proposal.blocked(f"fact:{i}")
        and not any(x.item == f"fact:{i}" and x.code == "unsafe_text" for x in proposal.issues)
        for line in history_lines(proposal, f"fact:{i}", f.text)
    ]
    alerts = (
        tuple(
            supported_text(a, proposal)
            for i, a in enumerate(c.alerts)
            if not proposal.blocked(f"alert:{i}")
            and not any(x.item == f"alert:{i}" and x.code == "unsafe_text" for x in proposal.issues)
        )
        if c.alerts
        else None
    )
    blocked_lines.extend(
        "Notify me if: " + supported_text(a, proposal)
        for i, a in enumerate(c.alerts)
        if proposal.blocked(f"alert:{i}")
        and not any(x.item == f"alert:{i}" and x.code == "unsafe_text" for x in proposal.issues)
    )
    notices: tuple[str, ...] = ()
    if proposal.corrected:
        notices = ("Card updated from your reply",)
    elif proposal.supersedes_id:
        notices = ("This card replaces the previous card; its old buttons no longer work.",)
    view = CardView(
        heading=heading,
        choices=choices,
        medications=medications,
        requested=tuple(requested),
        history=tuple(facts),
        history_hidden=0,
        alerts=alerts,
        questions=tuple(filter(None, blocked_lines)) + questions(proposal),
        notices=notices,
        buttons=card_footer(proposal, "en"),
        validity="valid 30 minutes",
    )
    from sanad.scribe.english import _lines

    # The legacy size decision excludes the History heading and overflow notice.
    oversized = (
        any(i.code == "batch_too_large" for i in proposal.issues)
        or len("\n".join(_lines(view, history_heading=False))) > DRAFT_SCRIBE_POLICY.card_max_chars
    )
    keep = (
        DRAFT_SCRIBE_POLICY.history_lines_max if oversized and not proposal.editing else len(facts)
    )
    return view.model_copy(
        update={"history": tuple(facts[:keep]), "history_hidden": max(0, len(facts) - keep)}
    )
