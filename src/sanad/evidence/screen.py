"""All readable values reach the urgent gateway before ordinary evidence work."""

from collections.abc import Sequence
from datetime import datetime

from pydantic import JsonValue

from sanad.concierge.records import Reading, ReportFactPayload
from sanad.domain import ObservationRef, PatientScope
from sanad.evidence import templates
from sanad.evidence.classify import analyte
from sanad.evidence.grading import grade as grade_row
from sanad.media.vision import DocumentRead
from sanad.safety import grade_bp, to_incident_facts
from sanad.safety.alerts import AlertHit, AlertInstruction, AlertOrder, apply_alerts, row_digest
from sanad.safety.models import IncidentFacts, LabVerdict, VitalVerdict
from sanad.safety.policy import SafetyPolicy
from sanad.scribe.extract import LabRowCandidate, OrderCandidate
from sanad.scribe.records import (
    CareOrderHead,
    CareOrderVersion,
    ClinicalFact,
    FactPayload,
    ValueAlert,
)
from sanad.steward.types import records
from sanad.steward.urgent import UrgentService
from sanad.store import keys
from sanad.store.protocol import Store
from sanad.store.records import Evidence, EvidenceHead, from_record


def alerts(store: Store, scope: PatientScope) -> tuple[AlertOrder, ...]:
    result = []
    for row in records(store, scope, "care_order_head"):
        head = from_record(row, CareOrderHead)
        version = store.get(scope, "care_order_version", head.current_version_id)
        if head.type != "value_alert" or head.status != "active" or not version:
            continue
        order = from_record(version, CareOrderVersion)
        if (
            order.order_id == head.id
            and order.order_version == head.current_order_version
            and isinstance(order.structured_instruction, ValueAlert)
        ):
            result.append(
                AlertOrder(
                    head_id=head.id,
                    active=True,
                    instruction=AlertInstruction.model_validate(
                        order.structured_instruction.model_dump()
                    ),
                )
            )
    return tuple(result)


def context(
    store: Store, scope: PatientScope, metric: str, received_at: datetime
) -> dict[str, JsonValue]:
    medications: list[JsonValue] = []
    conditions: list[JsonValue] = []
    previous: list[tuple[datetime, dict[str, JsonValue]]] = []
    for row in records(store, scope, "care_order_head"):
        head = from_record(row, CareOrderHead)
        version = store.get(scope, "care_order_version", head.current_version_id)
        if head.status == "active" and head.type == "medication" and version:
            order = from_record(version, CareOrderVersion)
            instruction = order.structured_instruction
            if isinstance(instruction, OrderCandidate):
                medications.append(
                    {
                        "drug": instruction.drug,
                        "dose": instruction.dose,
                        "frequency": instruction.frequency,
                    }
                )
    from sanad.steward.corrections import current_facts

    for row in current_facts(store, scope):
        fact = from_record(row, ClinicalFact)
        if fact.category == "condition" and isinstance(fact.payload, FactPayload):
            conditions.append(fact.payload.text)
        if (
            isinstance(fact.payload, ReportFactPayload)
            and fact.provenance.received_at < received_at
        ):
            for reading in fact.payload.readings:
                if analyte(reading.analyte) == analyte(metric):
                    previous.append(
                        (
                            fact.provenance.received_at,
                            {
                                "analyte": reading.analyte,
                                "value": reading.raw_value,
                                "unit": reading.raw_unit,
                                "date": fact.provenance.received_at.isoformat(),
                                "source": "patient_report",
                            },
                        )
                    )
    for row in records(store, scope, "evidence_head"):
        evidence_head = from_record(row, EvidenceHead)
        version = store.get(
            scope, "evidence", f"{evidence_head.id}:{evidence_head.current_version}"
        )
        if evidence_head.status != "accepted" or not version:
            continue
        evidence = from_record(version, Evidence)
        if evidence.provenance.received_at >= received_at:
            continue
        for value in evidence.extracted_values:
            if isinstance(value, LabRowCandidate) and analyte(value.analyte) == analyte(metric):
                previous.append(
                    (
                        evidence.provenance.received_at,
                        {
                            "analyte": value.analyte,
                            "value": value.value,
                            "unit": value.unit,
                            "date": str(
                                evidence.printed_date or evidence.provenance.received_at.date()
                            ),
                            "source": "accepted_evidence",
                        },
                    )
                )
    return {
        "medications": medications,
        "conditions": conditions,
        "previous": max(previous, key=lambda v: v[0])[1] if previous else None,
    }


def render_context(block: dict[str, JsonValue], language: str) -> str:
    def clean(value: JsonValue) -> str:
        return " ".join(str(value or "Not recorded").replace("{", "(").replace("}", ")").split())

    lines = [templates.render("context_heading", language)]
    medications = block.get("medications", [])
    if isinstance(medications, list):
        for item in medications:
            if isinstance(item, dict):
                lines.append(" · ".join(clean(item.get(k)) for k in ("drug", "dose", "frequency")))
    conditions = block.get("conditions", [])
    if isinstance(conditions, list):
        lines.extend(clean(value) for value in conditions)
    previous = block.get("previous")
    if isinstance(previous, dict):
        lines.append(
            templates.render("previous_prefix", language)
            + " · ".join(
                clean(previous.get(k)) for k in ("analyte", "value", "unit", "date", "source")
            )
        )
    return "\n".join(lines)


def screen_values(
    urgent: UrgentService,
    scope: PatientScope,
    values: Sequence[LabRowCandidate | Reading],
    source_id: str,
    received_at: datetime,
    policy: SafetyPolicy,
    *,
    prior: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], tuple[AlertHit, ...]]:
    store = urgent.store
    active = alerts(store, scope)
    ids: list[str] = []
    all_hits: list[AlertHit] = []
    seen: set[str] = set()
    for raw in values:
        row = grade_row(raw, policy) if isinstance(raw, LabRowCandidate) else raw
        value = row.raw_value if isinstance(row, Reading) else row.value
        unit = row.raw_unit if isinstance(row, Reading) else row.unit
        digest = row_digest(row.analyte, value, unit)
        if digest in seen:
            continue
        seen.add(digest)
        graded = grade_row(LabRowCandidate(analyte=row.analyte, value=value, unit=unit), policy)
        verdict: LabVerdict | VitalVerdict | None = graded.verdict
        if row.analyte.casefold() in {"bp", "blood pressure"} and value:
            pieces = value.split("/")
            if len(pieces) == 2 and all(p.isdigit() for p in pieces):
                verdict = grade_bp(int(pieces[0]), int(pieces[1]), policy=policy)
        hits = apply_alerts((row,), active, policy)
        all_hits.extend(hits)
        matched = tuple(h for h in hits if h.status == "hit")
        critical = verdict is not None and verdict.level in {"critical", "crisis", "low"}
        if not critical and not matched:
            continue
        severity: str
        if critical:
            assert verdict is not None
            facts, kernel_severity = to_incident_facts(
                verdict, source=ObservationRef(observation_id=source_id), policy=policy
            )
            severity = kernel_severity
            key = facts.unique_source_key + ":" + digest
        else:
            assert verdict is not None
            rule_id = "patient_alert:" + matched[0].head_id
            facts = IncidentFacts(
                source=ObservationRef(observation_id=source_id),
                unique_source_key=f"{source_id}#{verdict.rule_family}#{rule_id}",
                rule_family=verdict.rule_family,
                rule_id=rule_id,
                policy_version=policy.policy_version,
                verdict=verdict,
            )
            severity = "patient_alert"
            key = (
                "alert:"
                + matched[0].head_id
                + ":"
                + keys.digest(source_id + ":" + matched[0].row_digest)
            )
        payload = facts.as_payload()
        payload["context"] = context(store, scope, row.analyte, received_at)
        payload["patient_alerts"] = [h.model_dump(mode="json") for h in matched]
        payload["observed_row"] = {"name": row.analyte, "value": value, "unit": unit}
        incident = urgent.raise_incident(
            scope, key, payload, severity, urgent.steward.clock(), prior_delivery_refs=prior
        )
        ids.append(incident.id)
    return tuple(dict.fromkeys(ids)), tuple(dict.fromkeys(all_hits))


def read_values(read: DocumentRead) -> tuple[LabRowCandidate, ...]:
    return tuple(
        LabRowCandidate(analyte=r.item.name, value=r.item.value, unit=r.item.unit, flag=r.item.flag)
        for reader in (read.first, read.second)
        for r in reader.items
        if r.item.name and r.item.name.strip()
    )
