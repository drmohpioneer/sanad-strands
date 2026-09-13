"""Deterministic patient reading attachment after the accepted danger screen."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sanad.concierge import reports
from sanad.concierge.records import PatientAction
from sanad.domain import Mission, Provenance
from sanad.domain.entities import MonitorDetails
from sanad.monitor.executor import ACTIVE, project, reading_time, text_details
from sanad.monitor.report import patient_reply, render
from sanad.monitor.slots import metric
from sanad.scribe.records import ClinicalFact
from sanad.steward.patient import PatientTurnCommit
from sanad.store import keys
from sanad.store.records import from_record, to_record


def candidates(tx: PatientTurnCommit, values: reports.ReadingResult) -> tuple[Mission, ...]:
    names = {metric(r.analyte) for r in values.values} - {None}
    return tuple(
        m
        for m in tx.snapshot.missions
        if m.state in ACTIVE
        and isinstance(m.details, MonitorDetails)
        and metric(m.details.metric) in names
    )


def before_reading(tx: PatientTurnCommit, missions: tuple[Mission, ...]) -> tuple[Mission, ...]:
    """Use the accepted pre-deadline commit before a later reading revision."""
    from dataclasses import replace

    from sanad.evidence.deadline import before_fulfillment
    from sanad.steward.apply import CommitBuilder

    fresh = []
    for mission in missions:
        command = tx.builder.command.model_copy(
            update={"command_id": tx.id + ":monitor:" + mission.id}
        )
        builder = CommitBuilder(tx.snapshot.scope, command, tx.now, tx.builder.policy, tx.store)
        fresh.append(before_fulfillment(builder, mission))
    updated = {m.id: m for m in fresh}
    tx.snapshot = replace(
        tx.snapshot, missions=tuple(updated.get(m.id, m) for m in tx.snapshot.missions)
    )
    tx.builder.command = tx.builder.command.model_copy(
        update={"expected_versions": (*tx.snapshot.expected, *tx.snapshot.order_refs)}
    )
    return tuple(fresh)


def attach_reading(
    tx: PatientTurnCommit,
    text: str,
    values: reports.ReadingResult,
    source: Provenance,
    *,
    danger: bool = False,
    selected: Mission | None = None,
    observed_at: datetime | None = None,
    received_at: datetime | None = None,
) -> tuple[str, str] | None:
    from sanad.concierge.text import contains, is_question

    if is_question(text) or contains(
        text,
        "my son",
        "my daughter",
        "ابني",
        "بنتي",
        "زوجتي",
        "I am a doctor",
        "انا دكتور",
        "انا الدكتور",
    ):
        return None
    missions = (selected,) if selected else candidates(tx, values)
    if not missions:
        return None
    missions = before_reading(tx, missions)
    reports.record_reading(tx, text, values, source=source)
    row = tx.builder.puts[("clinical_fact", keys.digest(tx.id + ":reading"))]
    fact = from_record(row, ClinicalFact)
    received = received_at or tx.receipt.received_at
    observed = observed_at or reading_time(text, received, tx.snapshot.patient.timezone)
    language = tx.snapshot.patient.language
    if observed is None:
        return "monitor_verify", render("verify", language)
    ambiguous = {
        metric(m.details.metric)
        for m in missions
        if isinstance(m.details, MonitorDetails)
        if sum(
            other.details.kind == "MONITOR"
            and metric(other.details.metric) == metric(m.details.metric)
            for other in missions
        )
        > 1
    }
    lines = []
    for mission in missions:
        assert isinstance(mission.details, MonitorDetails)
        if metric(mission.details.metric) in ambiguous:
            tx.button(
                "start",
                mission.title,
                target=to_record(mission, tx.snapshot.scope).ref,
                slot="monitor_reading",
            )
            for key, action_row in tuple(tx.builder.puts.items()):
                if action_row.entity_type == "patient_action" and action_row.version == 1:
                    action = from_record(action_row, PatientAction)
                    if action.target_ref == to_record(mission, tx.snapshot.scope).ref:
                        tx.builder.puts[key] = to_record(
                            action.model_copy(
                                update={
                                    "monitor_reading_text": text,
                                    "monitor_observed_at": observed,
                                    "monitor_received_at": received,
                                }
                            ),
                            tx.snapshot.scope,
                        )
            continue
        details = text_details(tx.store, tx.snapshot.scope, mission, fact, observed, received)
        if details == mission.details:
            lines.append(render("verify", language))
            continue
        result = project(mission, details, tx.id, tx.now, tx.builder.policy.timing, danger=danger)
        tx.builder.add(result)
        lines.append(
            patient_reply(
                details,
                row.ref,
                language,
                tx.snapshot.patient.timezone,
                today=tx.now.astimezone(
                    ZoneInfo(details.timezone or tx.snapshot.patient.timezone)
                ).date(),
            )
        )
    if ambiguous:
        lines.append(render("choose", language))
    return "monitor_recorded", "\n".join(lines)


def callback(
    tx: PatientTurnCommit, token: PatientAction, source: Provenance
) -> tuple[str, str] | None:
    if token.slot_id != "monitor_reading":
        return None
    from sanad.concierge.templates import render as patient_render

    mission = next(
        (
            m
            for m in tx.snapshot.missions
            if to_record(m, tx.snapshot.scope).ref == token.target_ref
        ),
        None,
    )
    if (
        mission is None
        or mission.state not in ACTIVE
        or not token.monitor_reading_text
        or token.monitor_observed_at is None
        or token.monitor_received_at is None
    ):
        return "patient_callback_stale", patient_render(
            "patient_callback_stale", tx.snapshot.patient.language
        )
    from sanad.monitor.executor import parse_reading
    from sanad.monitor.guard import source_danger
    from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT

    values = parse_reading(token.monitor_reading_text, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT)
    return attach_reading(
        tx,
        token.monitor_reading_text,
        values,
        source,
        selected=mission,
        observed_at=token.monitor_observed_at,
        received_at=token.monitor_received_at,
        danger=source_danger(tx.store, tx.snapshot.scope, token.source_receipt_id),
    )


def unsupported_review(
    tx: PatientTurnCommit, text: str, values: reports.ReadingResult, source: Provenance
) -> bool:
    from sanad.concierge.question import open_ticket
    from sanad.scribe.monitoring import task_request

    if (
        not any(
            m.state in ACTIVE and task_request(m.title) and m.details.kind == "TASK"
            for m in tx.snapshot.missions
        )
        or not values.values
        or any(metric(v.analyte) for v in values.values)
    ):
        return False
    reports.record_reading(tx, text, values, source=source)
    open_ticket(tx, text)
    return True
