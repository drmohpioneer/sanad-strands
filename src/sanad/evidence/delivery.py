"""Deterministic evidence context at the existing clinical doctor gateway."""

from sanad.domain import Mission, PatientScope
from sanad.evidence import templates
from sanad.evidence.screen import render_context
from sanad.safety.models import IncidentFacts
from sanad.steward.types import records
from sanad.store.protocol import Store
from sanad.store.records import Evidence, EvidenceHead, MediaWork, from_record


def pending_verification(store: Store, scope: PatientScope, mission: Mission) -> bool:
    for row in records(store, scope, "evidence_head"):
        head = from_record(row, EvidenceHead)
        version = store.get(scope, "evidence", f"{head.id}:{head.current_version}")
        if not version:
            continue
        evidence = from_record(version, Evidence)
        if (
            head.status not in {"rejected", "superseded"}
            and evidence.provenance.received_at <= mission.due_at
            and (evidence.mission_id == mission.id or mission.id in evidence.candidate_mission_ids)
            and (
                evidence.identity_pending
                or not evidence.required_predicate_results
                or all(
                    p.evaluated_at > mission.escalation_at
                    for p in evidence.required_predicate_results
                )
            )
        ):
            return True
    for row in records(store, scope, "media_work"):
        work = from_record(row, MediaWork)
        receipt = store.get(scope, "inbound_receipt", work.receipt_id)
        if (
            not receipt
            or mission.id not in work.pending_mission_ids
            or work.state in {"completed", "needs_attention"}
        ):
            continue
        from sanad.store.records import InboundReceipt

        if from_record(receipt, InboundReceipt).received_at <= mission.due_at:
            return True
    return False


def incident_context(
    store: Store, scope: PatientScope, incident_id: str, facts: IncidentFacts, language: str
) -> str:
    lines = [render_context(facts.context, language)] if facts.context else []
    if facts.patient_alerts:
        for hit in facts.patient_alerts:
            lines.append(
                templates.render("alert_prefix", language)
                + " · ".join(
                    str(hit.get(k) or "Not recorded")
                    for k in ("metric", "comparator", "threshold", "unit")
                )
            )
    for row in records(store, scope, "evidence_head"):
        head = from_record(row, EvidenceHead)
        version = store.get(scope, "evidence", f"{head.id}:{head.current_version}")
        if not version or head.status != "accepted" or not head.mission_id:
            continue
        evidence = from_record(version, Evidence)
        mission = store.get(scope, "mission", head.mission_id)
        if (
            incident_id in evidence.incident_ids
            and mission
            and mission.body.get("state") == "fulfilled"
        ):
            lines.append(templates.render("danger_fulfillment", language))
            break
    text = "\n".join(lines).replace("{", "(").replace("}", ")")
    return "\n" + text if text else ""
