"""Recover provider-accepted contact without a second transport request."""

from typing import TYPE_CHECKING

from sanad.auth.service import revise
from sanad.contact.policy import DRAFT_CONTACT_POLICY as POLICY
from sanad.contact.scheduler import prime
from sanad.domain import FollowUpTask, Mission, PatientScope
from sanad.domain import events as ev
from sanad.domain.entities import TERMINAL_STATES
from sanad.domain.transitions import transition_followup, transition_mission
from sanad.steward.apply import CommitBuilder, EffectsRejected
from sanad.steward.types import records
from sanad.store.records import OutboundIntent, Patient, PatientProfile, from_record, to_record

if TYPE_CHECKING:
    from sanad.steward.service import Steward


def apply(steward: "Steward", intent: OutboundIntent) -> OutboundIntent:
    from sanad.steward.service import system_command

    if (
        not isinstance(intent.scope, PatientScope)
        or intent.status != "provider_accepted"
        or intent.contact_feedback != "pending"
    ):
        return intent
    command = system_command(
        intent.scope,
        "contact:" + intent.id,
        {
            "type": "_ContactFeedback",
            "intent_id": intent.id,
        },
        steward.clock(),
        lane="delivery",
    )
    for _ in range(2):
        result = steward.handle(command)
        if result.status == "accepted":
            row = steward.store.mark_contact_feedback(intent.scope, intent.id)
            return from_record(row, OutboundIntent) if row else intent
        if result.status != "stale_version":
            break
    return intent


def prepare(builder: CommitBuilder, profile: PatientProfile) -> None:
    row = builder.store.get(
        builder.scope, "outbound_intent", str(builder.command.payload.get("intent_id", ""))
    )
    if row is None:
        raise EffectsRejected("contact_intent_missing")
    intent = from_record(row, OutboundIntent)
    if (
        intent.status != "provider_accepted"
        or not intent.accepted_at
        or intent.contact_feedback != "pending"
    ):
        raise EffectsRejected("contact_not_accepted")
    refs = [r for r in intent.source_versions if r.entity_type in {"mission", "followup"}]
    if len(refs) != 1:
        raise EffectsRejected("contact_source_missing")
    ref = refs[0]
    source_row = builder.store.get(builder.scope, ref.entity_type, ref.id)
    if source_row is None:
        raise EffectsRejected("contact_source_missing")
    builder.command = builder.command.model_copy(
        update={
            "expected_versions": tuple(
                dict.fromkeys((*builder.command.expected_versions, row.ref, source_row.ref))
            )
        }
    )
    now, timing = builder.now, builder.policy.timing
    if ref.entity_type == "mission":
        mission = from_record(source_row, Mission)
        # Delivery may be accepted before fulfillment but recovered afterward. Keep terminal truth.
        historical = mission.state in TERMINAL_STATES | {"blocked"}
        result = transition_mission(
            mission,
            ev.ContactAccepted(
                event_id=builder.command.command_id,
                accepted_at=intent.accepted_at,
                kind=intent.contact_kind or "chase",
            ),
            now,
            timing,
        )
        if not isinstance(result, ev.TransitionResult) or not isinstance(result.aggregate, Mission):
            raise EffectsRejected("contact_feedback_transition")
        changed = result.aggregate
        effects = list(result.effects)
        if (
            not historical
            and intent.contact_kind == "chase"
            and changed.unanswered_delivered_count >= POLICY.unreachable_after
            and changed.state != "overdue"
        ):
            exhausted = transition_mission(
                changed,
                ev.ContactExhausted(
                    event_id=builder.command.command_id + ":exhausted", exhausted=True
                ),
                now,
                timing,
            )
            assert isinstance(exhausted, ev.TransitionResult) and isinstance(
                exhausted.aggregate, Mission
            )
            changed = exhausted.aggregate.model_copy(update={"version": mission.version + 1})
            effects.extend(
                e.model_copy(
                    update={"before_version": mission.version, "after_version": changed.version}
                )
                if isinstance(e, ev.RecordAudit)
                else e
                for e in exhausted.effects
            )
        if changed.state in {"open", "waiting_patient"}:
            changed = prime(changed, now)
        builder.add(ev.TransitionResult(aggregate=changed, effects=tuple(effects)))
        if intent.contact_kind == "chase":
            last = max(
                t for t in (profile.last_chase_accepted_at, intent.accepted_at) if t is not None
            )
            builder.put(to_record(revise(profile, now, last_chase_accepted_at=last), builder.scope))
        patients = builder.store.get(builder.scope, "patient", builder.scope.patient_id)
        if patients and changed.state == "unreachable":
            all_open = [
                changed if r.id == changed.id else from_record(r, Mission)
                for r in records(builder.store, builder.scope, "mission")
                if r.body["state"] not in TERMINAL_STATES | {"proposed", "awaiting_link"}
            ]
            patient = from_record(patients, Patient)
            if (
                all_open
                and all(m.state == "unreachable" for m in all_open)
                and patient.contact_status == "active"
            ):
                builder.put(
                    to_record(revise(patient, now, contact_status="unreachable"), builder.scope)
                )
    else:
        task = from_record(source_row, FollowUpTask)
        if task.state == "scheduled":
            builder.add(
                transition_followup(
                    task, ev.PromptAccepted(event_id=builder.command.command_id), now, timing
                )
            )
        else:
            # A stop, answer or deadline wins the clinical state race; delivery is still recorded.
            builder.audit(
                "CONTACT_FEEDBACK_OBSOLETE", builder.command.command_id, (source_row.ref,)
            )
