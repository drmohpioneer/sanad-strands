"""Existing safety adapters screen doctor corrections before the ordinary lease."""

from typing import TYPE_CHECKING

from sanad.concierge.records import Reading, ReportFactPayload
from sanad.domain import ObservationRef, PatientScope
from sanad.evidence.screen import screen_values
from sanad.safety import screen_text, to_incident_facts
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY
from sanad.scribe.extract import LabRowCandidate
from sanad.scribe.records import ClinicalFact, LabFactPayload
from sanad.steward.apply import CommitBuilder
from sanad.steward.corrections import prepare
from sanad.steward.urgent import UrgentService
from sanad.store import keys
from sanad.store.records import CommandEnvelope, Evidence, from_record

if TYPE_CHECKING:
    from sanad.steward.service import Steward


def screen(steward: "Steward", command: CommandEnvelope) -> None:
    assert isinstance(command.scope, PatientScope)
    # Validate scope/current predecessor before attributing
    # the doctor's corrected observation. Transaction limits must not suppress danger.
    # No source mutation is committed here; the commit guard still enforces the bound.
    request = prepare(
        CommitBuilder(
            command.scope,
            command,
            steward.clock(),
            steward.policy_provider(command.scope),
            steward.store,
        ),
        enforce_limit=False,
    )
    values: list[LabRowCandidate | Reading] = []
    texts = [str(command.payload.get("reason") or "")]
    changes = command.payload.get("changes")
    if isinstance(changes, dict):
        texts.extend(str(value) for value in changes.values() if value is not None)
    if command.payload.get("operation") != "detach":
        for row in request.puts:
            if row.entity_type == "evidence":
                evidence = from_record(row, Evidence)
                values.extend(
                    LabRowCandidate(
                        analyte=r.analyte if isinstance(r, LabRowCandidate) else (r.name or ""),
                        value=r.value,
                        unit=r.unit,
                    )
                    for r in evidence.extracted_values
                    if isinstance(r, LabRowCandidate) or r.name
                )
            elif row.entity_type == "clinical_fact":
                fact = from_record(row, ClinicalFact)
                texts.append(fact.payload.text)
                if isinstance(fact.payload, LabFactPayload):
                    values.append(
                        LabRowCandidate.model_validate(fact.payload.model_dump(exclude={"text"}))
                    )
                elif isinstance(fact.payload, ReportFactPayload):
                    values.extend(fact.payload.readings)
    source_id = "correction:" + keys.digest(command.command_id)
    urgent = UrgentService(steward, patient_template_id="patient_emergency")
    screen_values(urgent, command.scope, values, source_id, command.requested_at, POLICY)
    verdict = screen_text("\n".join(texts), policy=POLICY)
    if verdict.level == "danger":
        facts, severity = to_incident_facts(
            verdict, source=ObservationRef(observation_id=source_id), policy=POLICY
        )
        urgent.raise_incident(
            command.scope, facts.unique_source_key, facts.as_payload(), severity, steward.clock()
        )
