"""Atomic authority and lifecycle guards for private photo intake."""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sanad.domain import PatientScope
from sanad.store.records import (
    Claim,
    CommitRequest,
    IntakeCallback,
    IntakeConcern,
    IntakeDraft,
    from_record,
)

if TYPE_CHECKING:
    from sanad.store._base import Check, StoreBase


def intake_guards(
    store: "StoreBase", request: CommitRequest, now: datetime
) -> tuple[list["Check"], datetime] | None:
    from sanad.store._base import Check

    scope, actor, kind = (
        request.command.scope,
        request.command.principal,
        request.command.payload["type"],
    )
    drafts = [from_record(r, IntakeDraft) for r in request.puts if r.entity_type == "intake_draft"]
    if len(drafts) != 1:
        return None
    draft = drafts[0]
    old_row = store.get(scope, "intake_draft", draft.id)
    checks, expiry = [], now + timedelta(days=1)
    if kind == "IntakeCreate":
        if old_row or draft.version != 1 or draft.state != "pending" or draft.safety_epoch:
            return None
        for receipt_id in draft.source_receipt_ids:
            receipt = store.get(scope, "inbound_receipt", receipt_id)
            if receipt is None or receipt.body.get("source_subject") != actor.subject:
                return None
            checks.append(Check(receipt.key, receipt.version))
        if any(r.entity_type != "intake_draft" for r in request.puts) or request.intents:
            return None
    else:
        if old_row is None:
            return None
        old = from_record(old_row, IntakeDraft)
        if (
            (old.state != "pending" and kind != "IntakeDanger")
            or old.state not in {"pending", "associated"}
            or draft.state != old.state
            or draft.version != old.version + 1
        ):
            return None
        if (
            draft.source_receipt_ids != old.source_receipt_ids
            or draft.media_work_ids != old.media_work_ids
        ):
            return None
        checks.append(Check(old_row.key, old_row.version))
        if kind == "IntakeDanger":
            concerns = [
                from_record(r, IntakeConcern)
                for r in request.puts
                if r.entity_type == "intake_concern"
            ]
            reviews = [r for r in request.puts if r.entity_type == "review"]
            if (
                len(concerns) != 1
                or len(reviews) != 1
                or len(request.intents) != 1
                or concerns[0].intake_id != draft.id
                or draft.safety_epoch != old.safety_epoch + 1
                or reviews[0].id != concerns[0].review_obligation_id
                or reviews[0].body.get("review_kind") != "incident_response"
                or reviews[0].body.get("source_id") != draft.id
                or request.intents[0].body.get("template_id") != "liaison:DANGER"
            ):
                return None
        elif kind == "IntakeReview":
            reviews = [r for r in request.puts if r.entity_type == "review"]
            if (
                old.review_at > now
                or old.review_obligation_id
                or draft.state != "pending"
                or len(reviews) != 1
                or draft.review_obligation_id != reviews[0].id
                or reviews[0].body.get("review_kind") != "intake_clarification"
                or request.intents
            ):
                return None
        elif kind == "IntakeAction":
            nonce = request.command.payload.get("nonce_hash")
            if nonce:
                token_row = store.get(scope, "intake_callback", str(nonce))
                if token_row is None:
                    return None
                token = from_record(token_row, IntakeCallback)
                if (
                    token.actor_subject != actor.subject
                    or token.consumed_at
                    or token.expires_at <= now
                    or token.intake_id != old.id
                    or token.intake_version > old.version
                ):
                    return None
                if (
                    token.action == "select"
                    and store.get(
                        PatientScope(
                            doctor_id=old.owner_doctor_id, patient_id=token.patient_id or ""
                        ),
                        "patient",
                        token.patient_id or "",
                    )
                    is None
                ):
                    return None
                checks.append(Check(token_row.key, token_row.version))
                expiry = min(expiry, token.expires_at)
            if any(
                r.entity_type
                not in {"intake_draft", "intake_callback", "scribe_state", "scribe_proposal"}
                for r in request.puts
            ):
                return None
        else:
            return None
    return checks, expiry


def association_guards(
    store: "StoreBase", request: CommitRequest, now: datetime
) -> tuple[list["Check"], datetime] | None:
    """Bind a private draft and a one-use button to exactly the proposed patient."""
    from sanad.scribe.proposal import Proposal
    from sanad.store._base import Check

    drafts = [from_record(r, IntakeDraft) for r in request.puts if r.entity_type == "intake_draft"]
    if not drafts:
        return [], now + timedelta(days=1)
    if len(drafts) != 1:
        return None
    draft = drafts[0]
    old_row = store.get(draft.scope, "intake_draft", draft.id)
    proposals = [
        from_record(r, Proposal)
        for r in request.puts
        if r.entity_type == "scribe_proposal" and r.version == 1
    ]
    if old_row is None or len(proposals) != 1:
        return None
    old, proposal = from_record(old_row, IntakeDraft), proposals[0]
    if (
        old.state != "pending"
        or draft.state != "associated"
        or draft.version != old.version + 1
        or draft.source_receipt_ids != old.source_receipt_ids
        or draft.media_work_ids != old.media_work_ids
        or draft.proposal_id != proposal.id
        or draft.selected_patient_id != proposal.selected_patient_id
        or proposal.photo is None
        or proposal.photo.intake_id != draft.id
        or proposal.photo.media_work_ids != draft.media_work_ids
        or proposal.photo.reads != draft.reads
    ):
        return None
    checks, expiry = [Check(old_row.key, old_row.version)], proposal.expires_at
    if old.processing_claim:
        try:
            fence = Claim.model_validate(request.command.payload.get("intake_fence"))
        except ValueError:
            return None
        if (
            fence.owner != request.command.principal.subject
            or fence.version != old.version
            or fence.record_key.key != old_row.key
            or fence.expires_at <= now
            or fence.generation != old.processing_claim.generation
            or fence.expires_at != old.processing_claim.expires_at
            or draft.processing_claim is not None
        ):
            return None
        expiry = min(expiry, fence.expires_at)
    consumed = [
        from_record(r, IntakeCallback) for r in request.puts if r.entity_type == "intake_callback"
    ]
    if len(consumed) > 1:
        return None
    for token in consumed:
        row = store.get(draft.scope, "intake_callback", token.id)
        if row is None:
            return None
        original = from_record(row, IntakeCallback)
        if (
            original.consumed_at
            or not token.consumed_at
            or original.actor_subject != request.command.principal.subject
            or original.intake_id != old.id
            or original.intake_version > old.version
            or original.expires_at <= now
            or original.patient_id != draft.selected_patient_id
            or original.action == "later"
            or (original.action == "new") != proposal.creating_patient
        ):
            return None
        checks.append(Check(row.key, row.version))
        expiry = min(expiry, original.expires_at)
    for r in request.puts:
        if r.entity_type == "intake_concern":
            concern = from_record(r, IntakeConcern)
            previous = store.get(concern.scope, "intake_concern", concern.id)
            if (
                previous is None
                or concern.intake_id != draft.id
                or concern.state != "associated"
                or concern.associated_patient_id != proposal.selected_patient_id
            ):
                return None
            checks.append(Check(previous.key, previous.version))
    return checks, expiry


def photo_work_guards(
    store: "StoreBase", request: CommitRequest, now: datetime
) -> list["Check"] | None:
    from sanad.store._base import Check
    from sanad.store.records import PhotoAssociationWork

    tasks = [
        from_record(r, PhotoAssociationWork)
        for r in request.puts
        if r.entity_type == "photo_association_work"
    ]
    if len(tasks) != 1 or request.intents:
        return None
    task = tasks[0]
    old = store.get(task.scope, "photo_association_work", task.id)
    proposal = store.get(task.scope, "scribe_proposal", task.proposal_id)
    if (
        old is None
        or old.body.get("state") != "pending"
        or task.state != "completed"
        or proposal is None
        or proposal.body.get("status") != "confirmed"
    ):
        return None
    if any(
        task.model_dump()[k] != old.body[k]
        for k in ("scope", "patient_id", "intake_id", "proposal_id")
    ):
        return None
    checks = [Check(old.key, old.version), Check(proposal.key, proposal.version)]
    for record in request.puts:
        if record.entity_type in {"intake_draft", "intake_concern"}:
            model = (
                from_record(record, IntakeDraft)
                if record.entity_type == "intake_draft"
                else from_record(record, IntakeConcern)
            )
            current = store.get(model.scope, record.entity_type, record.id)
            if current is None:
                return None
            if isinstance(model, IntakeDraft):
                if model.id != task.intake_id or model.selected_patient_id != task.patient_id:
                    return None
            elif (
                model.intake_id != task.intake_id
                or model.associated_patient_id != task.patient_id
                or model.state != "associated"
            ):
                return None
            checks.append(Check(current.key, current.version))
    return checks
