"""Explicit preview/confirm reopening and separately recorded doctor responses."""

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sanad.corrections import Correction, CorrectionOffer
from sanad.domain import Mission, ReviewObligation, VersionRef
from sanad.domain import events as ev
from sanad.domain.boundaries import NonblankStr, UtcInstant
from sanad.domain.deadlines import ExplicitTiming
from sanad.domain.entities import MedicationDetails, ReviewAction
from sanad.domain.transitions import transition_mission, transition_review
from sanad.steward.apply import CommitBuilder, EffectsRejected
from sanad.store import keys
from sanad.store.records import from_record, to_record


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["PreviewReopen"] = "PreviewReopen"
    mission_ref: VersionRef
    due_at: UtcInstant
    reason: NonblankStr = Field(max_length=1000)


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["ConfirmReopen"] = "ConfirmReopen"
    offer_ref: VersionRef


class ResponseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["CorrectionResponse"] = "CorrectionResponse"
    correction_id: NonblankStr
    review_ref: VersionRef
    reason: NonblankStr = Field(max_length=1000)


def order_refs(builder: CommitBuilder, mission: Mission) -> tuple[VersionRef, ...]:
    from sanad.steward.corrections import read

    refs = list(mission.order_refs)
    if isinstance(mission.details, MedicationDetails):
        ref = mission.details.order_ref
        version = read(builder, ref)
        head_id = str(version.body["order_id"])
        head = builder.store.get(builder.scope, "care_order_head", head_id)
        if (
            not head
            or head.body.get("status") != "active"
            or head.body.get("current_version_id") != ref.id
        ):
            raise EffectsRejected("superseded_order")
        read(builder, head.ref)
        if ref not in refs:
            refs.append(ref)
    for ref in refs:
        row = read(builder, ref)
        if ref.entity_type == "care_order" and row.body.get("status") != "active":
            raise EffectsRejected("superseded_order")
    return tuple(refs)


def prepare_action(builder: CommitBuilder) -> None:
    from sanad.steward.corrections import correction_current, read

    kind = builder.command.payload.get("type")
    if kind == "AmendOrder":
        from sanad.scribe.order_changes import amend

        amend(builder)
        return
    if kind == "CorrectionResponse":
        response = ResponseBody.model_validate(builder.command.payload)
        correction = from_record(
            read(
                builder, VersionRef(entity_type="correction", id=response.correction_id, version=1)
            ),
            Correction,
        )
        review = from_record(read(builder, response.review_ref), ReviewObligation)
        if (
            not correction_current(builder.store, correction)
            or review.source_type != "correction"
            or review.source_id != correction.id
        ):
            raise EffectsRejected("current_correction_review_required")
        builder.add(
            transition_review(
                review,
                ev.ResolveReview(
                    event_id=builder.command.command_id,
                    action=ReviewAction.review,
                    expected_source_version=review.source_version,
                    actor_id=builder.command.principal.subject,
                    reason=response.reason,
                ),
                builder.now,
                builder.policy.timing,
            )
        )
        builder.audit(
            "DOCTOR_CORRECTION_RESPONSE_RECORDED",
            builder.command.command_id + ":response",
            (to_record(correction, builder.scope).ref,),
        )
        return
    profile = builder.store.get_patient_profile(builder.scope)
    assert profile
    if kind == "PreviewReopen":
        body = PreviewBody.model_validate(builder.command.payload)
        mission = from_record(read(builder, body.mission_ref), Mission)
        if (
            mission.state not in {"fulfilled", "cancelled", "closed_unfulfilled"}
            or body.due_at <= builder.now
        ):
            raise EffectsRejected("future_reopen_deadline_required")
        order_refs(builder, mission)
        instruction = mission.title
        if isinstance(mission.details, MedicationDetails):
            order_row = read(builder, mission.details.order_ref)
            value = order_row.body.get("structured_instruction")
            if isinstance(value, dict):
                instruction = " ".join(
                    str(value[k])
                    for k in ("drug", "dose", "frequency", "route", "timing", "duration")
                    if value.get(k)
                )
        active = (
            profile.binding_active and profile.consent_active and profile.routine_contact_enabled
        )
        preview = (
            f"Reopen {mission.title}. Confirmed instruction: {instruction}. "
            "The deadline clock restarts on confirmation; "
            f"deadline and escalation: {body.due_at.isoformat()}. "
            + (
                "The patient may receive reminders for the existing confirmed instruction "
                "under the existing schedule and contact policy."
                if active
                else "Patient messages remain stopped or unavailable. "
                "Reopening does not restore consent."
            )
            + " No medication course or follow-up anchor is restarted automatically."
        )
        offer = CorrectionOffer(
            id=keys.digest(builder.command.command_id),
            scope=builder.scope,
            actor=builder.command.principal,
            mission_ref=body.mission_ref,
            due_at=body.due_at,
            reason=body.reason,
            binding_epoch=profile.binding_epoch,
            consent_version=profile.consent_version,
            delivery_epoch=profile.delivery_epoch,
            expires_at=builder.now + timedelta(minutes=15),
            preview=preview,
            created_at=builder.now,
            updated_at=builder.now,
        )
        builder.put(to_record(offer, builder.scope))
        builder.audit(
            "REOPEN_PREVIEWED", builder.command.command_id, (to_record(offer, builder.scope).ref,)
        )
        return
    body2 = ConfirmBody.model_validate(builder.command.payload)
    offer = from_record(read(builder, body2.offer_ref), CorrectionOffer)
    if (
        offer.actor != builder.command.principal
        or offer.consumed_at
        or offer.expires_at <= builder.now
        or offer.due_at <= builder.now
        or (offer.binding_epoch, offer.consent_version, offer.delivery_epoch)
        != (profile.binding_epoch, profile.consent_version, profile.delivery_epoch)
    ):
        raise EffectsRejected("reopen_preview_stale")
    mission = from_record(read(builder, offer.mission_ref), Mission)
    refs = order_refs(builder, mission)
    outcome = transition_mission(
        mission,
        ev.DoctorReopen(
            event_id=builder.command.command_id,
            actor_id=builder.command.principal.subject,
            timing=ExplicitTiming(
                instant=offer.due_at,
                original_expression=offer.due_at.isoformat(),
                timezone=mission.timezone,
            ),
            grace_seconds=0,
            new_objective_predicate=mission.objective_predicate,
            new_order_refs=refs,
            current_active_order_refs=refs,
            reason=offer.reason,
            consent_active=profile.binding_active and profile.consent_active,
        ),
        builder.now,
        builder.policy.timing,
    )
    builder.add(outcome)
    builder.put(
        to_record(
            offer.model_copy(
                update={
                    "version": offer.version + 1,
                    "updated_at": builder.now,
                    "consumed_at": builder.now,
                }
            ),
            builder.scope,
        )
    )
