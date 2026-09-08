"""Additive Steward scheduling adapter and read-only delivery renderer."""

from collections.abc import Callable
from functools import wraps
from typing import cast

from sanad.agents.tools import AgentScope
from sanad.contact.ladder import ContactPlan
from sanad.coordinator import agent, policy, templates
from sanad.coordinator.permitted import compute
from sanad.domain import FollowUpTask, Mission, Provenance
from sanad.domain.entities import CoordinatorChoice
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as SAFETY
from sanad.steward.apply import CommitBuilder, EffectsRejected
from sanad.store.keys import digest
from sanad.store.protocol import Store
from sanad.store.records import (
    CommitRequest,
    CommitResult,
    OutboundIntent,
    Patient,
    PatientProfile,
    from_record,
    to_record,
)


def record_refusal(
    builder: CommitBuilder,
    source: Mission | FollowUpTask,
    slot: str,
    reason: agent.RefusalReason,
) -> None:
    id = digest(f"coordinator:{source.entity_type}:{source.id}:{source.version}:{slot}")
    if builder.store.get(builder.scope, "audit_event", id) is None:
        builder.audit("COORDINATOR_REFUSED:" + reason, id, (to_record(source, builder.scope).ref,))


def _commit_audit(builder: CommitBuilder) -> CommitResult:
    # CommitBuilder.finish also runs ordinary stale-order cleanup. An attempt
    # checkpoint must not perform that work or mutate any clinical/outbox row.
    events = tuple(builder.events.values())
    return builder.store.commit(
        CommitRequest(
            command=builder.command,
            expected=tuple(row.ref for row in events),
            events=events,
        )
    )


def _consume_attempt(builder: CommitBuilder, source: Mission, slot: str) -> agent.Refusal | None:
    """Steward checkpoint only. No model has a store, lease or command interface."""
    id = digest(f"coordinator-attempt:{source.id}:{source.deadline_generation}:{slot}")
    if builder.store.get(builder.scope, "audit_event", id):
        return agent.Refusal("attempt_already_started")
    command = builder.command.model_copy(update={"command_id": id})
    checkpoint = CommitBuilder(builder.scope, command, builder.now, builder.policy, builder.store)
    checkpoint.audit("COORDINATOR_ATTEMPT_STARTED", id, (to_record(source, builder.scope).ref,))
    result = _commit_audit(checkpoint)
    if result.status != "accepted":
        return agent.Refusal("stale_source")
    return None


def _apply(builder: CommitBuilder, source: Mission | FollowUpTask, intent: OutboundIntent) -> None:
    if not intent.template_id or not intent.slot_id:
        return
    updated_row = builder.puts.get((source.entity_type, source.id))
    patient_row = builder.store.get(builder.scope, "patient", builder.scope.patient_id)
    if updated_row is None or patient_row is None:
        return
    current = from_record(updated_row, type(source))
    patient = from_record(patient_row, Patient)
    at = current.next_contact_at if isinstance(current, Mission) else current.prompt_at
    plan = ContactPlan(at, intent.slot_id, intent.expires_at) if at else None
    bundle = compute(builder.store, current, patient, plan, intent.template_id)
    if not bundle.moves:
        record_refusal(builder, current, intent.slot_id, cast(agent.RefusalReason, bundle.reason))
        del builder.intents[intent.id]
        return
    if len(bundle.moves) < policy.min_choice_size:
        return
    assert isinstance(current, Mission) and isinstance(source, Mission) and plan is not None
    source_ref = to_record(source, builder.scope).ref
    refs = (*builder.command.expected_versions, source_ref)

    def fresh() -> bool:
        return all(
            (r := builder.store.get(builder.scope, ref.entity_type, ref.id)) is not None
            and r.ref == ref
            for ref in refs
        )

    def binding(in_time: Callable[[], bool]) -> AgentScope:
        return AgentScope(
            builder.command.principal,
            builder.scope,
            Provenance(
                source_observation_id=source.source_proposal_id or source.id,
                actor_kind="system",
                actor_id=builder.command.principal.subject,
                source_kind="doctor_statement",
                received_at=builder.now,
            ),
            SAFETY,
            lambda: in_time() and fresh(),
            worker=builder.command.worker,
        )

    outcome: agent.ContactProposal | agent.Refusal
    if (plan.window_end - builder.now).total_seconds() <= policy.call_timeout_s:
        outcome = agent.Refusal("contact_window_short")
    else:
        outcome = _consume_attempt(builder, source, intent.slot_id) or agent.bounded_call(
            bundle, binding
        )
    if not fresh():
        outcome = agent.Refusal("stale_source")
    if isinstance(outcome, agent.ContactProposal):
        # Steward repeats exact validation, even for a typed provider result.
        outcome = agent.validate(outcome.model_dump(), bundle)
    if isinstance(outcome, agent.ContactProposal):
        try:
            templates.render(bundle, outcome.fact_ids, patient)
        except ValueError as error:
            outcome = agent.Refusal(cast(agent.RefusalReason, str(error)))
    if isinstance(outcome, agent.Refusal):
        if outcome.reason == "stale_source":
            # Audit-only Steward command; never relax the clinical commit's
            # original source/authority checks to save a refusal.
            from sanad.steward.service import system_command

            command = system_command(
                builder.scope,
                digest(f"coordinator-stale:{intent.id}"),
                {
                    "type": "_ScheduleContact",
                    "mission_id": source.id,
                    "coordinator_refusal": "stale_source",
                },
                builder.now,
            ).model_copy(update={"fence": builder.command.fence})
            audit = CommitBuilder(
                builder.scope, command, builder.now, builder.policy, builder.store
            )
            record_refusal(audit, source, intent.slot_id, outcome.reason)
            if _commit_audit(audit).status != "accepted":
                raise EffectsRejected("coordinator_refusal_persistence")
        else:
            record_refusal(builder, current, intent.slot_id, outcome.reason)
        return
    choice = CoordinatorChoice(
        source_version=current.version,
        slot_id=plan.slot_id,
        contact_at=plan.at,
        window_end=plan.window_end,
        template_id=intent.template_id,
        move=outcome.move,
        fact_ids=outcome.fact_ids,
        bundle_digest=bundle.digest,
    )
    builder.puts[(source.entity_type, source.id)] = to_record(
        current.model_copy(update={"coordinator_choice": choice}),
        builder.scope,
    )
    builder.audit("COORDINATOR_SELECTED", digest(f"coordinator:{intent.id}"), (updated_row.ref,))


def coordinate(
    prepare: Callable[[CommitBuilder, Mission | FollowUpTask, PatientProfile], None],
) -> Callable[[CommitBuilder, Mission | FollowUpTask, PatientProfile], None]:
    @wraps(prepare)
    def run(
        builder: CommitBuilder, source: Mission | FollowUpTask, profile: PatientProfile
    ) -> None:
        prepare(builder, source, profile)
        # Only the accepted scheduler creates contacts. No intent means no model.
        if not any(
            r.body.get("notification_purpose") == "routine_prompt" for r in builder.intents.values()
        ):
            record_refusal(builder, source, "no-slot", "no_contact_plan")
        for row in tuple(builder.intents.values()):
            intent = from_record(row, OutboundIntent)
            if intent.notification_purpose == "routine_prompt":
                _apply(builder, source, intent)

    return run


def selected_text(
    store: Store,
    source: Mission | FollowUpTask,
    patient: Patient,
    template: str,
) -> str | None:
    """Read-only delivery rendering in current patient language; failure falls back."""
    if not isinstance(source, Mission) or not source.coordinator_choice:
        return None
    choice = source.coordinator_choice
    if choice.source_version != source.version or choice.template_id != template:
        return None
    bundle = compute(
        store,
        source,
        patient,
        ContactPlan(choice.contact_at, choice.slot_id, choice.window_end),
        template,
    )
    if bundle.digest != choice.bundle_digest:
        return None
    outcome = agent.validate({"move": choice.move, "fact_ids": choice.fact_ids}, bundle)
    if isinstance(outcome, agent.Refusal):
        return None
    try:
        return templates.render(bundle, outcome.fact_ids, patient)
    except ValueError:
        return None
