"""Shared confirmed-order supersession and honest instruction compensation."""

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from sanad.corrections import Correction
from sanad.domain import (
    FollowUpTask,
    Mission,
    PatientScope,
    Principal,
    ReviewObligation,
    VersionRef,
)
from sanad.domain import events as ev
from sanad.domain.boundaries import NonblankStr
from sanad.domain.entities import TERMINAL_STATES, DoctorTimingPolicy, ReviewAction
from sanad.domain.transitions import transition_followup, transition_mission, transition_review
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.records import CareOrderHead, CareOrderVersion
from sanad.steward.apply import CommitBuilder, EffectsRejected
from sanad.steward.types import records
from sanad.store.keys import Scope
from sanad.store.protocol import Store
from sanad.store.records import OrderAuthority, from_record, to_record

if TYPE_CHECKING:
    from sanad.scribe.proposal import Proposal


class AmendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["AmendOrder"] = "AmendOrder"
    predecessor: VersionRef
    reason: NonblankStr = Field(max_length=1000)
    changes: dict[str, str | None] = Field(min_length=1, max_length=4)


def active_reference(store: Store, scope: Scope, ref: VersionRef) -> bool:
    row = store.get(scope, ref.entity_type, ref.id)
    if row is None or row.ref != ref:
        return False
    if ref.entity_type == "care_order":
        return row.body.get("status") == "active"
    if ref.entity_type == "care_order_version":
        head = store.get(scope, "care_order_head", str(row.body.get("order_id")))
        return bool(
            head
            and head.body.get("status") == "active"
            and head.body.get("current_version_id") == ref.id
        )
    return False


def superseded_models(
    store: Store,
    scope: PatientScope,
    previous_ref: VersionRef,
    successor_ref: VersionRef,
    actor: Principal,
    event_id: str,
    now: datetime,
    policy: DoctorTimingPolicy,
) -> tuple[BaseModel, ...]:
    """The existing slice-14 transition path, shared by both doctor authorities."""
    models: list[BaseModel] = []
    superseded_ids = set()
    for row in records(store, scope, "mission"):
        mission = from_record(row, Mission)
        if (
            mission.kind != "MEDICATION"
            or previous_ref not in mission.order_refs
            or mission.state in TERMINAL_STATES
        ):
            continue
        outcome = transition_mission(
            mission,
            ev.OrderSuperseded(
                event_id=f"{event_id}:{mission.id}", successor_order_ref=successor_ref
            ),
            now,
            policy,
        )
        if isinstance(outcome, ev.TransitionRejected):
            raise EffectsRejected(outcome.reason_code)
        models.append(outcome.aggregate)
        superseded_ids.add(mission.id)
    for row in records(store, scope, "followup"):
        task = from_record(row, FollowUpTask)
        if (
            task.kind != "MEDICATION_DAY3"
            or previous_ref not in task.order_refs
            or task.state in {"fulfilled", "cancelled"}
        ):
            continue
        outcome2 = transition_followup(
            task,
            ev.CancelFollowUp(event_id=f"{event_id}:{task.id}", reason="order_superseded"),
            now,
            policy,
        )
        if isinstance(outcome2, ev.TransitionRejected):
            raise EffectsRejected(outcome2.reason_code)
        models.append(outcome2.aggregate)
    for row in records(store, scope, "review"):
        review = from_record(row, ReviewObligation)
        if (
            review.source_type != "mission"
            or review.source_id not in superseded_ids
            or review.review_kind != "unmet_objective"
            or review.state == "resolved"
        ):
            continue
        outcome3 = transition_review(
            review,
            ev.ResolveReview(
                event_id=f"{event_id}:{review.id}",
                action=ReviewAction.extend,
                expected_source_version=review.source_version,
                actor_id=actor.subject,
                reason="superseded",
            ),
            now,
            policy,
        )
        if isinstance(outcome3, ev.TransitionRejected):
            raise EffectsRejected(outcome3.reason_code)
        models.append(outcome3.aggregate)
    return tuple(models)


def clinical_meaning(order: OrderCandidate) -> tuple[str, ...]:
    # Only whitespace/case-equivalent spelling changes qualify as a typo.
    return tuple(
        " ".join(str(getattr(order, f) or "").casefold().split())
        for f in ("drug", "dose", "frequency", "route", "timing", "duration")
    )


def amend(builder: CommitBuilder) -> None:
    from sanad.steward.corrections import exposure, notice, read, review
    from sanad.store import keys

    body = AmendBody.model_validate(builder.command.payload)
    if body.predecessor.entity_type != "care_order_version" or set(body.changes) - {
        "dose",
        "frequency",
        "duration",
        "timing",
    }:
        raise EffectsRejected("amendable_order_field_required")
    old = from_record(read(builder, body.predecessor), CareOrderVersion)
    if not active_reference(builder.store, builder.scope, body.predecessor) or not isinstance(
        old.structured_instruction, OrderCandidate
    ):
        raise EffectsRejected("current_active_medication_required")
    head_row = builder.store.get(builder.scope, "care_order_head", old.order_id)
    authority_row = builder.store.get(builder.scope, "care_order", old.order_id)
    if not head_row or not authority_row:
        raise EffectsRejected("order_authority_missing")
    head = from_record(read(builder, head_row.ref), CareOrderHead)
    authority = from_record(read(builder, authority_row.ref), OrderAuthority)
    instruction = OrderCandidate.model_validate(
        old.structured_instruction.model_dump() | body.changes
    )
    if not instruction.dose or not instruction.dose.strip():
        raise EffectsRejected("explicit_dose_required")
    if instruction == old.structured_instruction:
        raise EffectsRejected("order_unchanged")
    cid = keys.digest(builder.command.command_id)
    version = old.order_version + 1
    new = CareOrderVersion.model_validate(
        old.model_dump()
        | {
            "id": f"{old.order_id}:{version}",
            "order_version": version,
            "structured_instruction": instruction,
            "created_at": builder.now,
            "updated_at": builder.now,
            "confirmed_at": builder.now,
            "confirmed_by": builder.command.principal.subject,
            "supersedes_version": old.order_version,
            "effective_from": builder.now,
            "provenance": old.provenance.model_copy(
                update={
                    "source_observation_id": cid,
                    "actor_kind": "doctor",
                    "actor_id": builder.command.principal.subject,
                    "source_kind": "clinician_confirmed",
                    "received_at": builder.now,
                    "confirmed_at": builder.now,
                    "confirmed_by": builder.command.principal.subject,
                    "source_span": None,
                    "source_region": None,
                }
            ),
        }
    )
    successor_ref = to_record(new, builder.scope).ref
    builder.put(to_record(new, builder.scope))
    builder.put(
        to_record(
            head.model_copy(
                update={
                    "version": head.version + 1,
                    "updated_at": builder.now,
                    "current_order_version": version,
                    "current_version_id": new.id,
                    "delivery_epoch": head.delivery_epoch + 1,
                    "changed_by": builder.command.principal.subject,
                    "changed_at": builder.now,
                }
            ),
            builder.scope,
        )
    )
    builder.put(
        to_record(
            authority.model_copy(
                update={"version": authority.version + 1, "updated_at": builder.now}
            ),
            builder.scope,
        )
    )
    changed = clinical_meaning(instruction) != clinical_meaning(old.structured_instruction)
    dependencies = superseded_models(
        builder.store,
        builder.scope,
        authority_row.ref,
        successor_ref,
        builder.command.principal,
        "supersede:" + builder.command.command_id,
        builder.now,
        builder.policy.timing,
    )
    if changed:
        for model in dependencies:
            builder.put(to_record(model, builder.scope))
    else:
        # Spelling-only revisions retain execution state and every clinical clock.
        from sanad.domain.entities import MedicationDetails

        next_authority = authority_row.ref.model_copy(update={"version": authority.version + 1})
        for kind in ("mission", "followup"):
            for row in records(builder.store, builder.scope, kind):
                dependent = (
                    from_record(row, Mission)
                    if kind == "mission"
                    else from_record(row, FollowUpTask)
                )
                if authority_row.ref not in dependent.order_refs:
                    continue
                updated = dependent.model_copy(
                    update={
                        "version": dependent.version + 1,
                        "updated_at": builder.now,
                        "order_refs": tuple(
                            next_authority if r == authority_row.ref else r
                            for r in dependent.order_refs
                        ),
                    }
                )
                if isinstance(updated, Mission) and isinstance(updated.details, MedicationDetails):
                    updated = updated.model_copy(
                        update={
                            "details": updated.details.model_copy(
                                update={"order_ref": successor_ref}
                            )
                        }
                    )
                builder.put(to_record(updated, builder.scope))
    mission_ids = {
        r.id
        for r in records(builder.store, builder.scope, "mission")
        if authority_row.ref in from_record(r, Mission).order_refs
    }
    exposed, prior = exposure(builder, {authority_row.ref, body.predecessor}, mission_ids)
    correction = Correction(
        id=cid,
        scope=builder.scope,
        created_at=builder.now,
        updated_at=builder.now,
        predecessor=body.predecessor,
        successor=successor_ref,
        operation="amend",
        actor=builder.command.principal,
        reason=body.reason,
        before=old.structured_instruction.model_dump(mode="json"),
        after=instruction.model_dump(mode="json"),
        exposures=exposed,
        prior_report_ids=prior,
        affected_mission_ids=tuple(sorted(mission_ids)),
    )
    builder.put(to_record(correction, builder.scope))
    if changed or exposed:
        obligation = review(builder, correction)
        notice(builder, correction, obligation)
    builder.audit(
        "ACTIVE_ORDER_AMENDED", builder.command.command_id, (successor_ref,), (body.predecessor,)
    )


def compensation_models(
    store: Store,
    models: tuple[BaseModel, ...],
    actor: Principal,
    proposal: "Proposal",
    now: datetime,
    policy: DoctorTimingPolicy,
) -> tuple[BaseModel, ...]:
    """Append compensation to the same Scribe confirmation transaction."""
    from sanad.steward.corrections import exposure, notice, review
    from sanad.steward.types import StewardPolicy
    from sanad.store import keys
    from sanad.store.records import MODELS, CommandEnvelope

    extras: dict[tuple[str, str], BaseModel] = {}
    for new in models:
        if (
            not isinstance(new, CareOrderVersion)
            or not new.supersedes_version
            or new.type != "medication"
        ):
            continue
        old_row = store.get(
            new.scope, "care_order_version", f"{new.order_id}:{new.supersedes_version}"
        )
        authority = store.get(new.scope, "care_order", new.order_id)
        if not old_row or not authority:
            raise EffectsRejected("amendment_predecessor_missing")
        old = from_record(old_row, CareOrderVersion)
        if not isinstance(old.structured_instruction, OrderCandidate) or not isinstance(
            new.structured_instruction, OrderCandidate
        ):
            continue
        now = new.confirmed_at
        command_id = "scribe-correction:" + proposal.id + ":" + new.id
        command = CommandEnvelope(
            command_id=command_id,
            scope=new.scope,
            principal=actor,
            requested_at=now,
            payload={"type": "AmendOrder"},
        )
        builder = CommitBuilder(new.scope, command, now, StewardPolicy(policy), store)
        mission_ids = {
            r.id
            for r in records(store, new.scope, "mission")
            if authority.ref in from_record(r, Mission).order_refs
        }
        exposed, prior = exposure(builder, {authority.ref, old_row.ref}, mission_ids)
        correction = Correction(
            id=keys.digest(command_id),
            scope=new.scope,
            created_at=now,
            updated_at=now,
            predecessor=old_row.ref,
            successor=to_record(new, new.scope).ref,
            operation="amend",
            actor=actor,
            reason=proposal.source_text,
            before=old.structured_instruction.model_dump(mode="json"),
            after=new.structured_instruction.model_dump(mode="json"),
            exposures=exposed,
            prior_report_ids=prior,
            affected_mission_ids=tuple(sorted(mission_ids)),
        )
        builder.put(to_record(correction, new.scope))
        if (
            clinical_meaning(old.structured_instruction)
            != clinical_meaning(new.structured_instruction)
            or exposed
        ):
            obligation = review(builder, correction)
            notice(builder, correction, obligation)
        for record in (*builder.puts.values(), *builder.intents.values()):
            extras[record.entity_type, record.id] = from_record(record, MODELS[record.entity_type])
    return tuple(extras.values())
