"""Narrow evidence write guard shared by memory and DynamoDB."""

from datetime import datetime
from typing import TYPE_CHECKING

from sanad.concierge.records import PatientAction
from sanad.domain import PatientScope
from sanad.evidence.associate import identity_required
from sanad.store.records import (
    CommitRequest,
    Evidence,
    EvidenceAction,
    EvidenceHead,
    from_record,
)

if TYPE_CHECKING:
    from sanad.store._base import Check, StoreBase

COMMANDS = {"_EvidenceTurn", "RecordObjectiveFulfilled", "AssociateEvidence", "RejectEvidence"}
ALLOWED = {
    "evidence",
    "evidence_head",
    "evidence_hash",
    "evidence_action",
    "patient_action",
    "patient_media",
    "clinical_fact",
    "mission",
    "review",
    "evidence_annotation",
}


def guards(store: "StoreBase", request: CommitRequest, now: datetime) -> list["Check"] | None:
    from sanad.store._base import Check

    command, scope = request.command, request.command.scope
    actor = command.principal
    kind, action = command.payload.get("type"), command.payload.get("action")
    if kind not in COMMANDS or not isinstance(scope, PatientScope) or not command.fence:
        return None
    profile = store.get_patient_profile(scope)
    doctor = store.get(scope, "doctor_authority", scope.doctor_id)
    if not profile or not doctor or not doctor.body.get("approved"):
        return None
    if actor.actor_kind == "system":
        if (
            not command.worker
            or "media" not in command.worker.permitted_lanes
            or action not in {"record", "evaluate", "duplicate"}
        ):
            return None
    elif actor.actor_kind == "doctor":
        if (
            actor.subject != doctor.body.get("subject")
            or actor.auth_epoch != doctor.body.get("auth_epoch")
            or action not in {"associate", "accept", "reject", "doctor_card", "confirm_identity"}
        ):
            return None
    elif actor.actor_kind == "patient":
        if (
            kind != "_EvidenceTurn"
            or not command.work_claim
            or not request.receipt_completion
            or action
            not in {"patient_choose", "patient_yes", "patient_no", "patient_other", "patient_stale"}
            or actor.subject != profile.recipient_subject
            or not profile.binding_active
            or not profile.consent_active
        ):
            return None
    else:
        return None
    allowed = ALLOWED | ({"media_work"} if command.payload.get("document_claim") else set())
    if any(
        r.entity_type not in allowed
        or r.doctor_id != scope.doctor_id
        or r.patient_id != scope.patient_id
        for r in request.puts
    ):
        return None
    checks = [Check(doctor.key, doctor.version)]
    for pdf_row in request.puts:
        if pdf_row.entity_type == "media_work":
            if (
                pdf_row.body.get("mime") != "application/pdf"
                or pdf_row.body.get("state") != "completed"
                or not command.work_claim
                or command.work_claim.record_key.key != pdf_row.key
                or action not in {"evaluate", "duplicate"}
            ):
                return None
    for pdf_row in request.puts:
        if pdf_row.entity_type == "media_work":
            from sanad.media.documents import final_page_checks
            from sanad.store.records import MediaWork

            if action == "duplicate":
                original = store.get(
                    scope, "evidence", str(command.payload.get("duplicate_evidence_id")) + ":1"
                )
                if not original or original.body.get("content_hash") != pdf_row.body.get(
                    "byte_hash"
                ):
                    return None
                checks.append(Check(original.key, original.version))
            page_checks = final_page_checks(store, from_record(pdf_row, MediaWork))
            if page_checks is None:
                return None
            checks.extend(page_checks)
    versions = [from_record(r, Evidence) for r in request.puts if r.entity_type == "evidence"]
    heads = [from_record(r, EvidenceHead) for r in request.puts if r.entity_type == "evidence_head"]
    if len(versions) > 1 or len(heads) != len(versions):
        return None
    for evidence in versions:
        if evidence.document_pages:
            from sanad.media.documents import check_item_size
            from sanad.store.records import to_record

            try:
                check_item_size(to_record(evidence, scope))
            except ValueError:
                return None
        head = heads[0]
        if (
            head.evidence_id != evidence.evidence_id
            or head.current_version != evidence.version
            or head.mission_id != evidence.mission_id
            or head.status
            != (
                evidence.association_state
                if evidence.association_state
                in {"accepted", "accepted_pending_identity", "rejected"}
                else "candidate"
            )
        ):
            return None
        receipt = store.get(scope, "inbound_receipt", evidence.observation_id)
        from sanad.store import keys

        work = store.get(scope, "media_work", keys.digest(evidence.observation_id))
        if (
            not receipt
            or not work
            or evidence.content_hash != work.body.get("byte_hash")
            or evidence.media_id != work.id
            or evidence.provenance.received_at.isoformat()
            != str(receipt.body.get("received_at")).replace("Z", "+00:00")
        ):
            return None
        if any(
            r.provenance.source_observation_id != evidence.observation_id for r in evidence.readers
        ):
            return None
        checks.extend((Check(receipt.key, receipt.version), Check(work.key, work.version)))
        old_head = store.get(scope, "evidence_head", head.id)
        if evidence.version == 1:
            if (
                old_head
                or evidence.association_state != "candidate"
                or evidence.accepted_by
                or action != "record"
                or {"doctor_accepted", "identity_confirmed"}.intersection(evidence.flags)
                or evidence.patient_match_provenance in {"doctor_choice", "patient_choice"}
            ):
                return None
        else:
            if (
                not old_head
                or head.version != old_head.version + 1
                or evidence.version != from_record(old_head, EvidenceHead).current_version + 1
            ):
                return None
            prior = store.get(scope, "evidence", f"{head.id}:{evidence.version - 1}")
            if not prior:
                return None
            old = from_record(prior, Evidence)
            # Only the separately authenticated correction workflow restores a
            # detached accepted source. Ordinary association cannot bypass it.
            if old.association_state == "detached" or evidence.association_state == "detached":
                return None
            if action in {"associate", "accept"} and identity_required(old):
                return None
            new_flags = (
                (*old.flags, "doctor_accepted")
                if actor.actor_kind == "doctor" and action == "accept"
                else (*old.flags, "identity_confirmed")
                if actor.actor_kind == "doctor" and action == "confirm_identity"
                else old.flags
            )
            if evidence.flags != new_flags:
                return None
            if action == "confirm_identity" and (
                old.association_state not in {"candidate", "unmatched", "accepted_pending_identity"}
                or not identity_required(old)
                or evidence.mission_id != old.mission_id
            ):
                return None
            if evidence.association_state in {"accepted", "accepted_pending_identity"} and (
                evidence.accepted_by != actor.subject
                or evidence.accepted_at != evidence.updated_at
                or not old.updated_at <= evidence.updated_at <= command.requested_at <= now
            ):
                return None
            mutable = {
                "id",
                "version",
                "updated_at",
                "association_state",
                "patient_match_provenance",
                "required_predicate_results",
                "accepted_by",
                "accepted_at",
                "mission_id",
                "flags",
                "candidate_mission_ids",
                "rejection_reason",
            }
            if old.model_dump(exclude=mutable) != evidence.model_dump(exclude=mutable):
                from sanad.evidence.commit import detailed_pdf_disposition

                # Only the canonical PDF overflow reduction may replace inline reads.
                # Its page records remain immutable and its parent finishes atomically.
                if (
                    not old.document_pages
                    or action != "evaluate"
                    or not command.payload.get("document_claim")
                    or evidence != detailed_pdf_disposition(evidence)
                    or detailed_pdf_disposition(old).model_dump(exclude=mutable)
                    != evidence.model_dump(exclude=mutable)
                    or any(
                        r.entity_type not in {"evidence", "evidence_head", "media_work", "review"}
                        for r in request.puts
                    )
                    or not any(
                        r.entity_type == "media_work"
                        and r.id == evidence.media_id
                        and r.body.get("last_error") == "document_too_detailed"
                        for r in request.puts
                    )
                ):
                    return None
            checks.extend((Check(old_head.key, old_head.version), Check(prior.key, prior.version)))
        if evidence.patient_match_provenance == "doctor_choice" and actor.actor_kind != "doctor":
            return None
    if actor.actor_kind == "patient":
        from sanad.concierge.plan import authorized

        auth = store.authorize(actor.bot_id or "", actor.subject)
        snap = authorized(store, actor, auth.binding, now) if auth.binding else None
        if not snap or not command.work_claim:
            return None
        incoming = store.get(scope, "inbound_receipt", command.work_claim.record_key.pk)
        if (
            not incoming
            or incoming.body.get("source_subject") != actor.subject
            or incoming.body.get("source_chat") != snap.binding.private_chat_id
        ):
            return None
        for value in (snap.patient, snap.consent, snap.binding):
            row = store.get(scope, value.entity_type, value.id)
            if row is None:
                return None
            checks.append(Check(row.key, row.version))
    for row in request.puts:
        if row.entity_type not in {"evidence_action", "patient_action"}:
            continue
        token = (
            from_record(row, EvidenceAction)
            if row.entity_type == "evidence_action"
            else from_record(row, PatientAction)
        )
        if (
            isinstance(token, EvidenceAction)
            and not token.consumed_at
            and (
                token.actor_subject != doctor.body.get("subject")
                or token.auth_epoch != doctor.body.get("auth_epoch")
                or token.expires_at <= now
            )
        ):
            return None
        if token.consumed_at:
            prior = store.get(scope, row.entity_type, token.id)
            if (
                not prior
                or prior.body.get("consumed_at")
                or command.payload.get("token_hash") != token.id
                or token.actor_subject != actor.subject
                or token.expires_at <= now
            ):
                return None
            fields = {"version", "updated_at", "consumed_at"}
            if {k: v for k, v in prior.body.items() if k not in fields} != {
                k: v for k, v in row.body.items() if k not in fields
            }:
                return None
            if isinstance(token, PatientAction) and (
                token.delivery_epoch != profile.delivery_epoch
                or token.binding_epoch != profile.binding_epoch
                or token.consent_version != profile.consent_version
            ):
                return None
            if isinstance(token, EvidenceAction) and token.auth_epoch != actor.auth_epoch:
                return None
            checks.append(Check(prior.key, prior.version))
    return checks
