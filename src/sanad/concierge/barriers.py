"""General mission routing around the accepted deterministic barrier recorder."""

from sanad.concierge import reports
from sanad.concierge.plan import Snapshot
from sanad.concierge.records import PatientAction, ReportFactPayload
from sanad.concierge.text import contains
from sanad.domain import Mission, Provenance
from sanad.domain.entities import TERMINAL_STATES, TestDetails
from sanad.steward.patient import PatientTurnCommit
from sanad.steward.types import records
from sanad.store.records import from_record, to_record

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
    obstacle = reports.recognize_barrier(text)
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


def route(tx: PatientTurnCommit, text: str, source: Provenance) -> reports.MedicationReply:
    choices = targets(
        tx.snapshot,
        text,
        tuple(from_record(r, Mission) for r in records(tx.store, tx.snapshot.scope, "mission")),
    )
    if len(choices) == 1:
        mark(tx, choices[0], text, source)
        return "patient_barrier_recorded", {"drug": choices[0].title}
    if choices:
        for mission in choices:
            tx.button(
                "start",
                mission.title,
                target=to_record(mission, tx.snapshot.scope).ref,
                slot="resolver_barrier",
            )
            for key, row in tuple(tx.builder.puts.items()):
                if row.entity_type == "patient_action" and row.version == 1:
                    token = from_record(row, PatientAction)
                    if token.target_ref == to_record(mission, tx.snapshot.scope).ref:
                        tx.builder.puts[key] = to_record(
                            token.model_copy(update={"medication_report_text": text}),
                            tx.snapshot.scope,
                        )
        return (
            "patient_barrier_choose"
            if all(m.kind == "MEDICATION" for m in choices)
            else "patient_barrier_mission_choose"
        ), {}
    return "patient_barrier_missing", {}


def callback(
    tx: PatientTurnCommit, token: PatientAction, source: Provenance
) -> reports.MedicationReply | None:
    if token.slot_id != "resolver_barrier":
        return None
    mission = next(
        (
            m
            for m in eligible(tx.snapshot)
            if to_record(m, tx.snapshot.scope).ref == token.target_ref
        ),
        None,
    )
    if (
        not mission
        or not token.medication_report_text
        or not reports.recognize_barrier(token.medication_report_text)
    ):
        return "patient_callback_stale", {}
    mark(tx, mission, token.medication_report_text, source)
    return "patient_barrier_recorded", {"drug": mission.title}
