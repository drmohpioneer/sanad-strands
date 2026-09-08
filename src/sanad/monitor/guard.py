"""The sole added patient-store rule: reproduce the exact MONITOR revision."""

from datetime import datetime

from sanad.concierge.plan import Snapshot
from sanad.concierge.records import PatientAction, ReportFactPayload
from sanad.domain import DRAFT_POLICY_2026_09, PatientScope
from sanad.domain.entities import MonitorDetails
from sanad.monitor.executor import ACTIVE, project, reading_time, text_details
from sanad.scribe.records import ClinicalFact
from sanad.steward.types import records
from sanad.store.protocol import Store
from sanad.store.records import CommitRequest, InboundReceipt, StoredRecord, from_record


def source_danger(store: Store, scope: PatientScope, source_id: str) -> bool:
    for row in records(store, scope, "incident"):
        facts = row.body.get("facts")
        source = facts.get("source") if isinstance(facts, dict) else None
        if isinstance(source, dict) and source.get("observation_id") == source_id:
            return True
    return False


def permits(
    store: Store, snap: Snapshot, request: CommitRequest, row: StoredRecord, now: datetime
) -> bool:
    from sanad.monitor.executor import parse_reading
    from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT

    command = request.command
    original = next((m for m in snap.missions if m.id == row.id), None)
    if (
        command.payload.get("type") != "RecordPatientReply"
        or original is None
        or original.state not in ACTIVE
        or not isinstance(original.details, MonitorDetails)
        or row.version != original.version + 1
        or row.doctor_id != snap.scope.doctor_id
        or row.patient_id != snap.scope.patient_id
    ):
        return False
    incoming = store.get(snap.scope, "inbound_receipt", str(command.payload.get("receipt_id")))
    if incoming is None:
        return False
    receipt = from_record(incoming, InboundReceipt)
    observed = None
    received = receipt.received_at
    source_id = receipt.id
    text = str((receipt.payload or {}).get("text", ""))
    if receipt.kind == "callback":
        token_row = store.get(
            snap.scope,
            "patient_action",
            str((receipt.payload or {}).get("callback_token_hash", "")),
        )
        if token_row is None:
            return False
        token = from_record(token_row, PatientAction)
        target = row.ref.model_copy(update={"version": original.version})
        own_deadline = (
            original.latest_deadline_notice_event_id
            == command.command_id + ":monitor:" + original.id + ":deadline"
        )
        if (
            token.slot_id != "monitor_reading"
            or token.target_ref != target
            and not (
                own_deadline
                and token.target_ref == target.model_copy(update={"version": original.version - 1})
            )
            or not token.monitor_reading_text
            or token.monitor_observed_at is None
            or token.monitor_received_at is None
        ):
            return False
        observed, received, source_id = (
            token.monitor_observed_at,
            token.monitor_received_at,
            token.source_receipt_id,
        )
        text = token.monitor_reading_text
    for value in request.puts:
        if value.entity_type != "clinical_fact":
            continue
        fact = from_record(value, ClinicalFact)
        payload = fact.payload
        if (
            fact.scope != snap.scope
            or fact.version != 1
            or not isinstance(payload, ReportFactPayload)
            or payload.report_kind != "reading"
            or fact.provenance.source_observation_id != receipt.id
            or fact.provenance.actor_id != command.principal.subject
        ):
            continue
        # Voice has a persisted extraction checkpoint; its accepted report still
        # must reproduce the deterministic parser, not a supplied slot verdict.
        if receipt.kind == "voice":
            from sanad.store import keys

            work = store.get(snap.scope, "media_work", keys.digest(receipt.id))
            if not work or not work.body.get("transcript_ref"):
                return False
            text = payload.text
        if (
            payload.text != text
            or payload.readings != parse_reading(text, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT).values
        ):
            continue
        at = observed or reading_time(text, received, snap.patient.timezone)
        if at is None or at > received or received > now:
            return False
        details = text_details(store, snap.scope, original, fact, at, received)
        if details == original.details:
            return False
        expected = project(
            original,
            details,
            command.command_id,
            now,
            DRAFT_POLICY_2026_09,
            danger=source_danger(store, snap.scope, source_id),
        )
        return expected.aggregate.model_dump(mode="json") == row.body
    return False
