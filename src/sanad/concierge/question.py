"""Patient-created QUESTION with a durable observation and default policy clock."""

from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY as POLICY
from sanad.concierge.records import ReportFactPayload
from sanad.concierge.text import normalized
from sanad.domain import (
    CreateSupportTicket,
    Mission,
    ObservationRef,
    Provenance,
    TransitionResult,
    create_support_ticket,
)
from sanad.domain.entities import QuestionDetails
from sanad.scribe.records import ClinicalFact
from sanad.steward.patient import PatientTurnCommit
from sanad.store import keys
from sanad.store.records import to_record


def open_ticket(tx: PatientTurnCommit, text: str) -> Mission:
    tx.kind("CreateSupportTicket")
    for mission in tx.snapshot.missions:
        if (
            isinstance(mission.details, QuestionDetails)
            and tx.now - mission.created_at < POLICY.question_dedupe
            and normalized(mission.details.question_text) == normalized(text)
        ):
            tx.put(
                ClinicalFact(
                    id=keys.digest(tx.id + ":question-attachment"),
                    scope=tx.snapshot.scope,
                    created_at=tx.now,
                    updated_at=tx.now,
                    category="patient_report",
                    visibility="patient_released",
                    payload=ReportFactPayload(
                        report_kind="question_attachment",
                        text=text,
                        target_ref=to_record(mission, tx.snapshot.scope).ref,
                    ),
                    provenance=Provenance(
                        source_observation_id=tx.receipt.id,
                        actor_kind="patient",
                        actor_id=tx.principal.subject,
                        source_kind="patient_report",
                        received_at=tx.receipt.received_at,
                    ),
                )
            )
            return mission
    event = CreateSupportTicket(
        event_id=tx.id,
        mission_id=keys.digest(tx.id + ":question"),
        doctor_id=tx.snapshot.scope.doctor_id,
        patient_id=tx.snapshot.scope.patient_id,
        title="سؤال للمراجعة",
        question_text=text,
        source_observation_ref=ObservationRef(observation_id=tx.receipt.id),
    )
    result = create_support_ticket(event, tx.now, tx.builder.policy.timing)
    tx.builder.add(result)
    assert isinstance(result, TransitionResult)
    assert isinstance(result.aggregate, Mission)
    # The 48-hour default offset now selects a local date at the policy clock (11b).
    assert result.aggregate.grace_seconds == 0
    return result.aggregate
