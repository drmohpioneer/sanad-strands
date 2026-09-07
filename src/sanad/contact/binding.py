"""Apply existing PatientBound events in the guarded intended-person transaction."""

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from sanad.contact.scheduler import prime
from sanad.domain import DRAFT_POLICY_2026_09, Mission
from sanad.domain import events as ev
from sanad.domain.transitions import transition_mission
from sanad.steward.apply import CommitBuilder
from sanad.steward.types import StewardPolicy, records
from sanad.store.records import (
    MODELS,
    AuditEvent,
    OutboundIntent,
    Patient,
    StoredRecord,
    from_record,
)

if TYPE_CHECKING:
    from sanad.auth.claim import ClaimService
    from sanad.auth.commands import ConfirmPatientClaim


def binding_result(mission: Mission, event_id: str, now: datetime) -> ev.TransitionResult:
    effects: list[ev.Effect] = []
    if (
        mission.escalation_at <= now
        and mission.handled_deadline_generation < mission.deadline_generation
    ):
        deadline = transition_mission(
            mission, ev.DeadlineReached(event_id=event_id + ":deadline"), now, DRAFT_POLICY_2026_09
        )
        assert isinstance(deadline, ev.TransitionResult) and isinstance(deadline.aggregate, Mission)
        mission = deadline.aggregate
        effects.extend(deadline.effects)
    bound = transition_mission(
        mission, ev.PatientBound(event_id=event_id, consent_active=True), now, DRAFT_POLICY_2026_09
    )
    assert isinstance(bound, ev.TransitionResult) and isinstance(bound.aggregate, Mission)
    effects.extend(bound.effects)
    if bound.noop:
        effects.append(
            ev.RecordAudit(
                event_type="PATIENT_BOUND",
                event_id=event_id,
                before_version=mission.version - 1,
                after_version=mission.version,
            )
        )
    return ev.TransitionResult(
        aggregate=prime(bound.aggregate, now)
        if bound.aggregate.state == "open"
        else bound.aggregate,
        effects=tuple(effects),
    )


def valid_binding_mission(old: StoredRecord, new: StoredRecord, now: datetime) -> bool:
    mission = from_record(old, Mission)
    if mission.state != "awaiting_link":
        return False
    changed = from_record(new, Mission)
    expected = binding_result(mission, "check", now).aggregate
    ignore = {"latest_deadline_notice_event_id"}
    return expected.model_dump(exclude=ignore) == changed.model_dump(exclude=ignore)


def prepare_binding(
    service: "ClaimService",
    command: "ConfirmPatientClaim",
    patient: Patient,
) -> tuple[tuple[BaseModel, ...], tuple[OutboundIntent, ...], tuple[AuditEvent, ...]]:
    now = service.clock()
    envelope = service.envelope(command).model_copy(update={"scope": patient.scope})
    builder = CommitBuilder(
        patient.scope, envelope, now, StewardPolicy(DRAFT_POLICY_2026_09), service.store
    )
    for row in records(service.store, patient.scope, "mission"):
        mission = from_record(row, Mission)
        if mission.state == "awaiting_link":
            builder.add(binding_result(mission, command.command_id + ":bound:" + mission.id, now))
    models = tuple(from_record(r, MODELS[r.entity_type]) for r in builder.puts.values())
    intents = tuple(from_record(r, OutboundIntent) for r in builder.intents.values())
    events = tuple(
        from_record(r, AuditEvent).model_copy(update={"scope": service.scope})
        for r in builder.events.values()
    )
    return models, intents, events
