"""General mission routing around the accepted deterministic barrier recorder."""

from datetime import timedelta
from typing import Literal

from sanad.concierge import reports, templates
from sanad.concierge.barrier_evidence import category
from sanad.concierge.barrier_reading import outcome_for, stage
from sanad.concierge.plan import Snapshot
from sanad.concierge.policy import BARRIER_CATEGORIES
from sanad.concierge.records import BarrierOutcome, PatientAction, ReportFactPayload
from sanad.concierge.text import contains
from sanad.domain import FollowUpTask, Mission, Provenance
from sanad.domain.entities import TERMINAL_STATES, TestDetails
from sanad.steward.patient import PatientTurnCommit
from sanad.steward.types import records
from sanad.store.records import InboundReceipt, from_record, to_record

KINDS = frozenset({"MEDICATION", "TEST", "VISIT", "TASK", "MONITOR", "SEND_RECORDS"})
KIND_WORDS = {
    "MEDICATION": ("medicine", "medication", "drug", "pharmacy", "دوا", "الدواء", "الصيدلية"),
    "TEST": ("lab", "test", "blood test", "تحاليل", "التحليل", "المعمل"),
    "VISIT": ("visit", "appointment", "زيارة", "الزيارة", "المعاد"),
    "TASK": ("task", "مهمة"),
    "MONITOR": ("monitor", "measurement", "قياس", "القياس"),
    "SEND_RECORDS": ("records", "upload", "papers", "الورق", "الملفات"),
}


def eligible(snapshot: Snapshot) -> tuple[Mission, ...]:
    return tuple(
        m
        for m in snapshot.missions
        if m.kind in KINDS
        and m.state not in TERMINAL_STATES | {"proposed", "awaiting_link"}
        and all(r in snapshot.acknowledgment_refs for r in m.order_refs)
    )


def targets(
    snapshot: Snapshot, text: str, all_missions: tuple[Mission, ...] = ()
) -> tuple[Mission, ...]:
    choices = eligible(snapshot)
    known = all_missions or snapshot.missions
    named = tuple(
        m
        for m in known
        if contains(text, m.title)
        or isinstance(m.details, TestDetails)
        and any(contains(text, a) for a in m.details.analytes)
    )
    # Keep the accepted medicine aliases without interpreting a barrier twice.
    from sanad.scribe.extract import OrderCandidate
    from sanad.scribe.names import entry_for

    named_order_ids = set()
    for order in (*snapshot.orders, *snapshot.stopped_orders):
        instruction = order.structured_instruction
        if isinstance(instruction, OrderCandidate):
            entry = entry_for(instruction.drug)
            names = (instruction.drug, *entry.arabic_spellings) if entry else (instruction.drug,)
            if any(contains(text, name) for name in names):
                named_order_ids.add(order.order_id)
    named += tuple(m for m in known if any(r.id in named_order_ids for r in m.order_refs))
    if named:
        return tuple(m for m in choices if m.id in {n.id for n in named})
    kinds = {kind for kind, words in KIND_WORDS.items() if contains(text, *words)}
    return tuple(m for m in choices if not kinds or m.kind in kinds)


def mark(tx: PatientTurnCommit, mission: Mission, text: str, source: Provenance) -> None:
    obstacle = category(outcome_for(tx))
    assert obstacle
    if any(tx.receipt.id in a.receipt_ids for a in mission.barrier_attempts):
        pass  # The recovered receipt already owns its saved report fact.
    elif mission.barrier_reason and mission.state in {"blocked", "overdue"}:
        # No second block event, no moved pause, no refreshed budget.
        reports.fact(
            tx,
            text,
            ReportFactPayload(
                report_kind="barrier",
                text=text,
                barrier_type=obstacle,
                target_ref=to_record(mission, tx.snapshot.scope).ref,
            ),
            source=source,
        )
        tx.kind("RecordPatientReply")
    else:
        reports.record_barrier(tx, mission, text, obstacle, source=source)
    tx.builder.command = tx.builder.command.model_copy(
        update={
            "payload": {
                **tx.builder.command.payload,
                "resolver_target": mission.id,
                "resolver_words": text,
            }
        }
    )


def choice_buttons(
    tx: PatientTurnCommit, target: Mission | FollowUpTask, text: str
) -> reports.MedicationReply:
    for number, kind in enumerate(BARRIER_CATEGORIES, 1):
        label = templates.render("patient_barrier_option_" + kind, tx.snapshot.patient.language)
        _button(tx, "barrier_category", label, target, text, number, kind)
    return "patient_barrier_uncertain", {}


def _button(
    tx: PatientTurnCommit,
    action: Literal["barrier_category", "barrier_target"],
    label: str,
    target: Mission | FollowUpTask,
    text: str,
    number: int,
    kind: str | None = None,
) -> None:
    tx.button(action, label, target=to_record(target, tx.snapshot.scope).ref, slot="barrier_28")
    for key, row in tuple(tx.builder.puts.items()):
        if row.entity_type == "patient_action" and row.version == 1:
            token = from_record(row, PatientAction)
            if token.choice_number is None and token.slot_id == "barrier_28":
                tx.builder.puts[key] = to_record(
                    PatientAction.model_validate(
                        token.model_dump()
                        | {
                            "expires_at": tx.now + timedelta(minutes=30),
                            "medication_report_text": text,
                            "choice_number": number,
                            "barrier_category": kind,
                        }
                    ),
                    tx.snapshot.scope,
                )


def route(tx: PatientTurnCommit, text: str, source: Provenance) -> reports.MedicationReply:
    choices = targets(
        tx.snapshot,
        text,
        tuple(from_record(r, Mission) for r in records(tx.store, tx.snapshot.scope, "mission")),
    )
    if len(choices) == 1:
        if category(outcome_for(tx)):
            mark(tx, choices[0], text, source)
            return "patient_barrier_recorded", {"drug": choices[0].title}
        return choice_buttons(tx, choices[0], text)
    if choices:
        for number, mission in enumerate(choices, 1):
            _button(tx, "barrier_target", mission.title, mission, text, number)
        key = (
            "patient_barrier_choose"
            if all(m.kind == "MEDICATION" for m in choices)
            else "patient_barrier_mission_choose"
        )
        if tx.receipt.channel == "web":
            return "patient_barrier_targets", {
                "options": "\n".join(f"{i}. {m.title}" for i, m in enumerate(choices, 1))
            }
        return key, {}
    return "patient_barrier_missing", {}


def pending_tokens(tx: PatientTurnCommit) -> tuple[PatientAction, ...]:
    return tuple(
        from_record(r, PatientAction)
        for r in records(tx.store, tx.snapshot.scope, "patient_action")
        if r.body.get("action") in {"barrier_category", "barrier_target"}
    )


def live_choice(tx: PatientTurnCommit, token: PatientAction) -> bool:
    return bool(
        token.actor_subject == tx.principal.subject
        and not token.consumed_at
        and token.expires_at > tx.now
        and token.delivery_epoch == tx.profile.delivery_epoch
        and token.binding_epoch == tx.profile.binding_epoch
        and token.consent_version == tx.snapshot.consent.version
        and not any(
            t.consumed_at
            and t.source_receipt_id == token.source_receipt_id
            and t.action == token.action
            for t in pending_tokens(tx)
        )
    )


def web_choice(
    tx: PatientTurnCommit, text: str, source: Provenance
) -> reports.MedicationReply | None:
    if tx.receipt.channel != "web":
        return None
    tokens = pending_tokens(tx)
    if not tokens:
        return None
    latest = max(
        tokens, key=lambda t: (t.created_at, t.action == "barrier_category", t.source_receipt_id)
    )
    tokens = tuple(
        t
        for t in tokens
        if t.source_receipt_id == latest.source_receipt_id and t.action == latest.action
    )
    for token in tokens:
        accepted = {str(token.choice_number)}
        if token.barrier_category:
            accepted.update(
                templates.render(
                    "patient_barrier_option_" + token.barrier_category, lang
                ).casefold()
                for lang in ("en", "ar")
            )
        if text.strip().casefold() in accepted:
            if not live_choice(tx, token):
                return "patient_callback_stale", {}
            tx.consume(token)
            return callback(tx, token, source)
    return None


def callback(
    tx: PatientTurnCommit, token: PatientAction, source: Provenance
) -> reports.MedicationReply | None:
    legacy = token.slot_id in {"resolver_barrier", "medication_barrier"}
    if not legacy and token.action not in {"barrier_category", "barrier_target"}:
        return None
    if not legacy and not live_choice(tx, token):
        return "patient_callback_stale", {}
    all_targets: tuple[Mission | FollowUpTask, ...] = (
        *eligible(tx.snapshot),
        *tx.snapshot.followups,
    )
    target = next(
        (m for m in all_targets if to_record(m, tx.snapshot.scope).ref == token.target_ref), None
    )
    row = tx.store.get(tx.snapshot.scope, "inbound_receipt", token.source_receipt_id)
    if not target or not row:
        return "patient_callback_stale", {}
    prior = from_record(row, InboundReceipt)
    if prior.source_subject != tx.principal.subject:
        return "patient_callback_stale", {}
    if isinstance(target, FollowUpTask) and (
        target.kind != "MEDICATION_DAY3" or target.state != "waiting_response"
    ):
        return "patient_callback_stale", {}
    text = token.medication_report_text
    if not text and token.slot_id == "medication_barrier":
        text = str((prior.payload or {}).get("text", ""))
    if not text:
        return "patient_callback_stale", {}
    if token.action == "barrier_category":
        stage(
            tx,
            BarrierOutcome(
                status="accepted",
                category=token.barrier_category,
                provenance="patient_choice",
                choice_id=token.id,
                source_receipt_id=prior.id,
            ),
        )
    elif prior.barrier_outcome:
        stage(tx, prior.barrier_outcome)
    else:
        # Old mission-choice buttons remain useful without resurrecting phrase matching.
        return choice_buttons(tx, target, text)
    tx.builder.command = tx.builder.command.model_copy(
        update={
            "payload": {
                **tx.builder.command.payload,
                "barrier_source_receipt": prior.id,
                "barrier_choice_id": token.id,
            }
        }
    )
    if not category(outcome_for(tx)):
        return choice_buttons(tx, target, text)
    if isinstance(target, FollowUpTask):
        reports.record_day3(tx, text, source=source)
        return "patient_day3_recorded", {}
    mark(tx, target, text, source)
    return "patient_barrier_recorded", {"drug": target.title}
