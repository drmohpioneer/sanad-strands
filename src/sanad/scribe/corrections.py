"""Bind replies to an open card without letting a model choose patient identity."""

import json
import re
from dataclasses import dataclass

from sanad.media.numbers import numbers_in
from sanad.scribe.card import dictation_questions
from sanad.scribe.extract import (
    DictationCandidate,
    FactCandidate,
    MissionCandidate,
    OrderCandidate,
    ProposalIssue,
)
from sanad.scribe.names import entry_for, normalize, split_drug_dose
from sanad.scribe.patients import lookup
from sanad.scribe.proposal import Proposal
from sanad.store.protocol import Store

NEW_PATIENT = re.compile(r"مريض\s+(?:جديد|تاني|تانى)|\bnew patient\b", re.I)


def reply_mode(text: str, previous: Proposal, store: Store) -> str:
    marker = NEW_PATIENT.search(text)
    if marker:
        return "new" if not text[: marker.start()].strip(" \n،,و") else "choose"
    from sanad.scribe.extract import PatientCandidate

    choices = lookup(store, previous.scope, PatientCandidate(name_as_spoken=text))
    current = previous.selected_patient_id
    for choice in choices:
        spoken = normalize(choice.display_name)
        # Lookup ranks candidates; an actual name mention, not a guessed model ID,
        # must establish the escape from the current card.
        if choice.patient_id != current and re.search(
            r"(?<!\w)" + re.escape(spoken) + r"(?!\w)", normalize(text)
        ):
            if spoken != normalize(previous.candidate.patient.name_as_spoken or ""):
                return "new"
    return "correction"


def correction_request(previous: Proposal, text: str) -> str:
    return json.dumps(
        {
            "previous_candidate": previous.candidate.model_dump(mode="json"),
            "previous_source_text": previous.source_text,
            "open_questions": "\n".join(
                f"q{i}: {q}" for i, q in enumerate(dictation_questions(previous), 1)
            ),
            "correction_text": text,
        },
        ensure_ascii=False,
    )


def _aliases(order: OrderCandidate) -> tuple[str, ...]:
    name, _ = split_drug_dose(order.drug)
    entry = entry_for(name) or (entry_for(order.name_latin) if order.name_latin else None)
    values = [name, order.name_latin or ""]
    if entry:
        values.extend((entry.latin, *entry.arabic_spellings, *entry.latin_spellings))
        # A shortened label answers that card's question, never changes ingredients.
        if " " in entry.latin:
            root = entry_for(entry.latin.split()[0])
            if root:
                values.extend((root.latin, *root.arabic_spellings, *root.latin_spellings))
    return tuple(dict.fromkeys(normalize(v) for v in values if v))


def _fact_material(fact: FactCandidate) -> str:
    return " ".join(
        (
            fact.text,
            fact.clinical_en or "",
            *(term.spoken + " " + (term.english or "") for term in fact.terms),
        )
    )


def answer_slots(previous: Proposal, text: str) -> dict[str, str]:
    aliases: dict[str, tuple[str, ...]] = {
        f"order:{i}": _aliases(o) for i, o in enumerate(previous.candidate.orders)
    }
    for i, fact in enumerate(previous.candidate.facts):
        whole = normalize(_fact_material(fact))
        fact_labels: list[str] = []
        for pattern, fact_names in (
            (r"\bef\b|كفاءه|فانكشن", ("EF", "كفاءة", "فانكشن")),
            (r"\becg\b|رسم قلب", ("ECG", "رسم قلب")),
            (r"angina|انجينا|ذبحه", ("angina", "انجينا", "ذبحة")),
            (r"hypertension|ضغط", ("hypertension", "ضغط")),
            (r"diabetes|سكر", ("diabetes", "سكر")),
        ):
            if re.search(pattern, whole, re.I):
                fact_labels.extend(normalize(n) for n in fact_names)
        aliases[f"fact:{i}"] = tuple(fact_labels)
    for i, mission in enumerate(previous.candidate.missions):
        aliases[f"mission:{i}"] = (normalize(mission.text), normalize(mission.clinical_en or ""))
    for i, alert in enumerate(previous.candidate.alerts):
        aliases[f"alert:{i}"] = tuple(
            w for w in normalize(alert).split() if len(w) > 3 and not numbers_in(w)
        )
    assigned: dict[str, list[str]] = {}
    for clause in re.split(r"[,،؛;\n]", normalize(text)):
        found: list[tuple[int, int, str]] = []
        for item, names in aliases.items():
            for name in names:
                if name:
                    for match in re.finditer(
                        r"(?<![a-z\u0621-\u064a])(?:وال|ال|و)?"
                        + re.escape(name)
                        + r"(?![a-z\u0621-\u064a])",
                        clause,
                        re.I,
                    ):
                        found.append((match.start(), match.end(), item))
        # Retain the longest non-overlapping label and refuse tied identities.
        labels: list[tuple[int, int, str]] = []
        for label in sorted(found, key=lambda m: (m[0], -(m[1] - m[0]))):
            if not any(label[0] < prior[1] and prior[0] < label[1] for prior in labels):
                labels.append(label)
        if len({item for _, _, item in labels}) == 1:
            assigned.setdefault(labels[0][2], []).append(clause)
        elif labels:
            for index, (start, _, item) in enumerate(labels):
                end = labels[index + 1][0] if index + 1 < len(labels) else len(clause)
                assigned.setdefault(item, []).append(clause[start:end])
    if not assigned:
        open_items = {
            i.item
            for i in previous.issues
            if i.code in {"dose_missing", "dose_unclear", "disputed_number"}
            and i.item.startswith(("order:", "fact:"))
        }
        if (
            not open_items
            and len(previous.candidate.orders) == 1
            and re.search(r"جرعة|جرعه|dose", text, re.I)
        ):
            open_items = {"order:0"}
        if len(open_items) == 1 and numbers_in(text):
            assigned[next(iter(open_items))] = [text]
    return {item: " ".join(parts) for item, parts in assigned.items()}


def same_patient(previous: Proposal, proposed: DictationCandidate, text: str) -> DictationCandidate:
    patient = previous.candidate.patient
    # A spelling correction before creation changes no stored patient's identity.
    if previous.creating_patient and (
        not patient.name_as_spoken or re.search(r"اسمه|اسمها|الاسم|name", text, re.I)
    ):
        name = proposed.patient.name_as_spoken
        if name and normalize(name) in normalize(text):
            patient = patient.model_copy(update={"name_as_spoken": name})
    return proposed.model_copy(update={"patient": patient})


def valid_patient_revision(previous: Proposal, changed: Proposal) -> bool:
    expected = same_patient(
        previous, changed.candidate, changed.source_text[len(previous.source_text) :]
    )
    return changed.candidate.patient == expected.patient


@dataclass(frozen=True)
class MergedCorrection:
    candidate: DictationCandidate
    issues: tuple[ProposalIssue, ...]
    resolved_numbers: tuple[str, ...]


def _additions[T: (OrderCandidate, FactCandidate, MissionCandidate, str)](
    kind: str,
    incoming: tuple[T, ...],
    old: tuple[T, ...],
    target: list[T],
    slots: dict[str, str],
    indices: dict[str, int],
    text: str,
) -> None:
    item = kind + ":new"
    for index, value in enumerate(incoming):
        if value in old or value in target:
            continue
        raw = (
            value
            if isinstance(value, str)
            else value.drug
            if isinstance(value, OrderCandidate)
            else value.text
        )
        explicit = item in slots and indices.get(item) == index
        quoted = bool(raw and normalize(raw) in normalize(text))
        if old and not explicit:
            continue
        if isinstance(value, OrderCandidate) and any(
            isinstance(o, OrderCandidate) and set(_aliases(value)) & set(_aliases(o)) for o in old
        ):
            continue
        if (explicit or quoted) and set(numbers_in(str(value))) <= set(numbers_in(text)):
            target.append(value)


def _settles_merge(
    issue: ProposalIssue, candidate: DictationCandidate, slots: dict[str, str]
) -> bool:
    """An answer to another field of the item cannot settle this conflict."""
    answer = slots.get(issue.item, "")
    if not answer or not issue.field or ":" not in issue.item:
        return False
    family, index = issue.item.split(":")
    values = getattr(
        candidate,
        {"order": "orders", "fact": "facts", "mission": "missions", "alert": "alerts"}[family],
    )
    if int(index) >= len(values):
        return False
    item = values[int(index)]
    value = item if isinstance(item, str) else getattr(item, issue.field, None)
    if not value:
        return False
    answer = normalize(answer)
    if issue.field == "action":
        patterns = {
            "continue": r"كمل|استمر|ماشي|بياخد|continue",
            "start": r"ابدأ|ابدا|ضفت|زودته|start",
            "stop": r"وقف|بطل|stop",
            "change": r"غير|زودت جرعه|قللت|change",
        }
        return bool(re.search(patterns.get(str(value), r"(?!)"), answer))
    if issue.field in {"dose", "text"} and (amounts := numbers_in(str(value))):
        other_field = re.search(r"مرات|مرتين|يومي|بالليل|الصبح|ساعه|ساعات", answer)
        explicit_dose = re.search(r"جرعه|dose", answer)
        return set(amounts) <= set(numbers_in(answer)) and (not other_field or bool(explicit_dose))
    return normalize(str(value)) in answer


def merge_correction(
    previous: Proposal, proposed: DictationCandidate, text: str
) -> MergedCorrection:
    old = previous.candidate
    slots = answer_slots(previous, text)
    indices: dict[str, int] = {}
    for edit in proposed.correction_edits:
        if normalize(edit.source_quote) not in normalize(text):
            continue
        occupied = answer_slots(previous, edit.source_quote)
        # A quoted dose answer cannot be reassigned to another drug/finding.
        if occupied and edit.item not in occupied:
            continue
        if edit.item.startswith("order:") and edit.item != "order:new" and edit.item not in slots:
            continue
        slots.setdefault(edit.item, edit.source_quote)
        indices[edit.item] = edit.proposal_index
    issues: list[ProposalIssue] = []
    consumed: set[str] = set(previous.resolved_numbers)
    orders = []
    for i, order in enumerate(old.orders):
        item = f"order:{i}"
        answer = slots.get(item)
        if not answer:
            orders.append(order)
            continue
        renamed = bool(re.search(r"مش|بدل|بدّل|not\b|instead", answer, re.I))
        matches = [o for o in proposed.orders if set(_aliases(o)) & set(_aliases(order))]
        incoming = (
            proposed.orders[indices[item]]
            if item in indices and indices[item] < len(proposed.orders)
            else matches[0]
            if len(matches) == 1
            else proposed.orders[i]
            if renamed and i < len(proposed.orders)
            else order
        )
        changes: dict[str, object] = {}
        allowed = set(numbers_in(" ".join(str(v) for v in order.model_dump().values()))) | set(
            numbers_in(answer)
        )
        for name in (
            "dose",
            "frequency",
            "route",
            "timing",
            "duration",
            "effective_expression",
            "checkin_expression",
            "action",
        ):
            value = getattr(incoming, name)
            if value is None or value == getattr(order, name):
                continue
            if not set(numbers_in(value)) <= allowed:
                issues.append(
                    ProposalIssue(
                        item=item,
                        code="correction_unclear",
                        blocked=False,
                        question=f'إجابة "{order.drug}" فيها رقم مش تابع له؛ وضّح التعديل.',
                    )
                )
                continue
            changes[name] = value
        if renamed and any(alias and alias in normalize(answer) for alias in _aliases(incoming)):
            changes.update(
                drug=incoming.drug, name_latin=incoming.name_latin, generic=incoming.generic
            )
        changed = order.model_copy(update=changes)
        if changed.dose != order.dose and any(
            q.item == item
            and q.code in {"dose_missing", "dose_unclear", "disputed_number", "unsupported_number"}
            for q in previous.issues
        ):
            consumed.update(numbers_in(order.dose or ""))
        # A model may already have proposed the full strength, blocked because
        # the source only held a compressed prefix. The explicit dose answer
        # settles that same question even if the candidate dose did not change.
        entry = entry_for(order.drug)
        if (
            entry
            and "5/160/12.5" in entry.fixed_combination_strengths
            and numbers_in(changed.dose or "") == ("5", "160", "12.5")
            and {"5", "160", "12.5"} <= set(numbers_in(answer))
            and any(
                q.item == item and q.code in {"dose_unclear", "unsupported_number"}
                for q in previous.issues
            )
        ):
            source_slot = answer_slots(previous, previous.source_text).get(item, "")
            for match in re.finditer(r"(?<!\d)(560|516)\s+12\.5(?!\d)", source_slot):
                consumed.add(match[1])
        orders.append(changed)
    facts = []
    for i, fact in enumerate(old.facts):
        item = f"fact:{i}"
        index = indices.get(item, i)
        incoming_fact = proposed.facts[index] if index < len(proposed.facts) else fact
        if "ef" in normalize(_fact_material(fact)):
            incoming_fact = next(
                (f for f in proposed.facts if re.search(r"\bEF\b", _fact_material(f), re.I)),
                incoming_fact,
            )
        answer = slots.get(item)
        allowed = set(numbers_in(_fact_material(fact))) | set(numbers_in(answer or ""))
        if answer and set(numbers_in(_fact_material(incoming_fact))) <= allowed:
            facts.append(incoming_fact)
            if incoming_fact != fact:
                consumed.update(numbers_in(fact.text))
        else:
            facts.append(fact)
    missions = list(old.missions)
    for i, mission in enumerate(old.missions):
        item = f"mission:{i}"
        index = indices.get(item, i)
        if item in slots and index < len(proposed.missions):
            incoming_mission = proposed.missions[index]
            allowed = set(numbers_in(str(mission.model_dump()) + slots[item]))
            if set(numbers_in(str(incoming_mission.model_dump()))) <= allowed:
                missions[i] = incoming_mission
    alerts = list(old.alerts)
    for i, alert in enumerate(old.alerts):
        item = f"alert:{i}"
        index = indices.get(item, i)
        if item in slots and index < len(proposed.alerts):
            incoming_alert = proposed.alerts[index]
            if set(numbers_in(incoming_alert)) <= set(numbers_in(alert + slots[item])):
                alerts[i] = incoming_alert
                if incoming_alert != alert:
                    consumed.update(numbers_in(alert))
    # Additions require words actually supplied in this reply, or an exact quote
    # attached to that new item. The normal source-number and name gates still run.
    _additions("order", proposed.orders, old.orders, orders, slots, indices, text)
    _additions("fact", proposed.facts, old.facts, facts, slots, indices, text)
    _additions("mission", proposed.missions, old.missions, missions, slots, indices, text)
    _additions("alert", proposed.alerts, old.alerts, alerts, slots, indices, text)
    # Never delete an unanswered ambiguity just because the model omitted it.
    ambiguities = tuple(
        a
        for a in old.ambiguities
        if not any(number in numbers_in(text) for number in numbers_in(a))
        and not any(
            (name := normalize(order.drug))
            and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", normalize(a))
            and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", normalize(text))
            for order in proposed.orders
        )
    )
    merged = old.model_copy(
        update={
            "orders": tuple(orders),
            "facts": tuple(facts),
            "missions": tuple(missions),
            "alerts": tuple(alerts),
            "ambiguities": ambiguities,
            "correction_edits": (),
        }
    )
    merged._dropped_numbers = tuple(
        dict.fromkeys((*old._dropped_numbers, *proposed._dropped_numbers))
    )
    # Stored proposals retain single-source metadata separately from model fields.
    # Unanswered items retain it; answered items use the new pair's evidence.
    proposed_targets = {f"{item.split(':')[0]}:{index}": item for item, index in indices.items()}
    merged._single_source = tuple(
        dict.fromkeys(
            (
                *(item for item in previous.single_source if item not in slots),
                *(proposed_targets.get(item, item) for item in proposed._single_source),
            )
        )
    )
    remaining_conflicts = []
    from sanad.scribe.extract import extracted_numbers

    for issue in previous.issues:
        if issue.code != "extraction_conflict":
            continue
        if _settles_merge(issue, merged, slots):
            consumed.update(n for n in issue.numbers if n not in extracted_numbers(merged))
        else:
            remaining_conflicts.append(issue)
    merged._merge_issues = ()
    return MergedCorrection(
        same_patient(previous, merged.model_copy(update={"patient": proposed.patient}), text),
        tuple(
            (
                *issues,
                *remaining_conflicts,
                *(
                    issue.model_copy(update={"item": proposed_targets.get(issue.item, issue.item)})
                    for issue in proposed._merge_issues
                ),
            )
        ),
        tuple(sorted(consumed)),
    )
