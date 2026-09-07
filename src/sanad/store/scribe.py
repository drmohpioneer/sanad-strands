"""Narrow conditional cross-partition transactions for doctor confirmation."""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sanad.domain import PatientScope, TenantScope
from sanad.scribe.proposal import InvitationWork, Proposal, ScribeCallback, ScribeState
from sanad.store import keys
from sanad.store.keys import AccountScope, IntakeScope, Key
from sanad.store.records import (
    MODELS,
    NAME_CACHE_SCOPE,
    CommitRequest,
    Doctor,
    NameCache,
    NameMemory,
    Patient,
    from_record,
    model_scope,
)

if TYPE_CHECKING:
    from sanad.store._base import Check, StoreBase

COMMANDS = frozenset(
    {
        "PhotoUnreadable",
        "PhotoAssociate",
        "IntakeCreate",
        "IntakeAction",
        "IntakeReview",
        "IntakeDanger",
        "ScribePropose",
        "ScribeNameCache",
        "ScribeAction",
        "ScribeConfirm",
        "ScribeExpire",
        "ScribeReply",
        "ScribeInvalidate",
        "ScribeWork",
    }
)
PROPOSAL_TYPES = {"scribe_proposal", "scribe_state", "scribe_callback", "scribe_invitation_work"}
CLINICAL_TYPES = {
    "photo_association_work",
    "patient_media",
    "patient",
    "patient_profile",
    "clinical_fact",
    "care_order_head",
    "care_order_version",
    "care_order",
    "care_plan",
    "mission",
    "followup",
    "review",
}


def _patient_revision(previous: Proposal, changed: Proposal) -> bool:
    from sanad.scribe.corrections import valid_patient_revision

    return valid_patient_revision(previous, changed)


def scribe_guards(
    store: "StoreBase",
    request: CommitRequest,
    now: datetime,
) -> tuple[list["Check"], datetime] | None:
    from sanad.store._base import Check

    command, scope = request.command, request.command.scope
    if type(scope) is not TenantScope or store._identity is None:
        return None
    actor, kind = command.principal, command.payload.get("type")
    if not isinstance(kind, str) or kind not in COMMANDS or actor.doctor_id != scope.doctor_id:
        return None
    row = store.get(scope, "doctor", scope.doctor_id)
    if row is None:
        return None
    doctor = from_record(row, Doctor)
    if doctor.telegram_bot_id != store._identity.bot_id:
        return None
    expiry = now + timedelta(days=1)
    checks = [Check(row.key, row.version)]
    if kind in {"ScribeExpire", "ScribeInvalidate", "ScribeWork", "IntakeReview"}:
        worker = command.worker
        if (
            actor.actor_kind != "system"
            or worker is None
            or worker.service_subject != actor.subject
            or worker.resolved_scope != scope
            or "scribe" not in worker.permitted_lanes
            or worker.auth_expiry <= now
        ):
            return None
        expiry = worker.auth_expiry
    else:
        auth = store.authorize(doctor.telegram_bot_id, actor.subject)
        if (
            actor.bot_id != doctor.telegram_bot_id
            or actor.subject != doctor.telegram_user_id
            or actor.actor_kind != "doctor"
            or "doctor" not in actor.verified_roles
            or auth.principal != actor
            or doctor.status != "approved"
            or actor.auth_epoch != doctor.auth_epoch
            or auth.binding is None
        ):
            return None
        bound = store.get(
            AccountScope(bot_id=doctor.telegram_bot_id), "subject_binding", actor.subject
        )
        if bound is None:
            return None
        checks.append(Check(bound.key, bound.version))
    allowed = PROPOSAL_TYPES | (CLINICAL_TYPES if kind == "ScribeConfirm" else set())
    if kind == "ScribeConfirm":
        allowed |= {"name_memory"}
    if kind == "ScribeNameCache":
        allowed = {"name_cache"}
    if kind in {"IntakeCreate", "IntakeAction", "IntakeReview", "IntakeDanger", "ScribePropose"}:
        allowed |= {"intake_draft", "intake_callback", "intake_concern", "review"}
    if kind == "PhotoAssociate":
        allowed = {"photo_association_work", "intake_draft", "intake_concern"}
    if kind == "PhotoUnreadable":
        allowed = {"intake_draft", "patient_media"}
    if any(r.entity_type not in allowed for r in request.puts):
        return None
    for read in request.identity_reads:
        if (
            read.entity_type == "name_memory"
            and kind == "ScribeConfirm"
            and read.scope in {scope, AccountScope(bot_id=doctor.telegram_bot_id)}
        ):
            current = store.get(read.scope, read.entity_type, read.id)
            if (current.version if current else None) != read.version:
                return None
            checks.append(Check(Key(keys.partition(read.scope), f"NAME#{read.id}"), read.version))
            continue
        if not isinstance(read.scope, (TenantScope, PatientScope, IntakeScope)):
            return None
        if read.scope.doctor_id != scope.doctor_id:
            return None
        current = store.get(read.scope, read.entity_type, read.id)
        if (current.version if current else None) != read.version:
            return None
        if current:
            checks.append(Check(current.key, current.version))
        elif read.entity_type in PROPOSAL_TYPES | {"intake_draft", "intake_callback"}:
            checks.append(
                Check(
                    Key(
                        keys.partition(read.scope),
                        f"{read.entity_type.upper()}#{keys.component(read.id)}",
                    ),
                    None,
                )
            )
        else:
            return None
    for written in (*request.puts, *request.events, *request.intents):
        actual = model_scope(from_record(written, MODELS[written.entity_type]))
        if written.entity_type == "name_memory":
            if kind != "ScribeConfirm" or actual not in {
                scope,
                AccountScope(bot_id=doctor.telegram_bot_id),
            }:
                return None
            continue
        if written.entity_type == "name_cache":
            if kind != "ScribeNameCache" or actual != NAME_CACHE_SCOPE:
                return None
            continue
        if isinstance(actual, AccountScope) or actual.doctor_id != scope.doctor_id:
            return None
        if written.entity_type in {"clinical_fact", "care_order_version", "care_plan"}:
            if written.version != 1 or store.get(actual, written.entity_type, written.id):
                return None
        if written.entity_type == "outbound_intent":
            if (
                written.body.get("scope_kind") != "intake"
                or written.body.get("audience") != "doctor"
                or written.body.get("recipient_ref") != doctor.private_chat_id
                or written.body.get("notification_purpose")
                != ("DANGER" if kind == "IntakeDanger" else "solicited_reply")
            ):
                return None
    proposals = [
        from_record(r, Proposal) for r in request.puts if r.entity_type == "scribe_proposal"
    ]
    if kind == "ScribeNameCache":
        from sanad.scribe.policy import DRAFT_SCRIBE_POLICY

        if (
            len(request.puts) != 1
            or request.intents
            or request.identity_reads
            or command.work_claim
        ):
            return None
        cache = from_record(request.puts[0], NameCache)
        if (
            cache.updated_at > now
            or cache.expires_at != cache.updated_at + DRAFT_SCRIBE_POLICY.name_cache_ttl
        ):
            return None
    if kind.startswith("Intake"):
        from sanad.store.intake import intake_guards

        extra = intake_guards(store, request, now)
        if extra is None:
            return None
        extra_checks, deadline = extra
        checks.extend(extra_checks)
        expiry = min(expiry, deadline)
    if kind == "PhotoAssociate":
        from sanad.store.intake import photo_work_guards

        photo_checks = photo_work_guards(store, request, now)
        if photo_checks is None:
            return None
        checks.extend(photo_checks)
    if kind == "PhotoUnreadable":
        from sanad.store.intake import unreadable_media_guards

        media_checks = unreadable_media_guards(store, request, now)
        if media_checks is None:
            return None
        checks.extend(media_checks)
    if kind == "ScribeWork":
        if len(request.puts) != 1 or request.intents or proposals:
            return None
        written = request.puts[0]
        if written.entity_type != "scribe_invitation_work":
            return None
        work = from_record(written, InvitationWork)
        old_work = store.get(scope, work.entity_type, work.id)
        parent = store.get(scope, "scribe_proposal", work.proposal_id)
        if (
            old_work is None
            or old_work.body.get("status") != "pending"
            or work.status == "pending"
            or parent is None
            or parent.body.get("status") != "confirmed"
            or work.patient_id != old_work.body.get("patient_id")
            or work.proposal_id != old_work.body.get("proposal_id")
        ):
            return None
        checks.extend((Check(old_work.key, old_work.version), Check(parent.key, parent.version)))
    elif kind == "ScribeReply":
        if request.puts:
            return None
    elif kind == "ScribePropose":
        from sanad.store.intake import association_guards

        association = association_guards(store, request, now)
        if association is None:
            return None
        extra_checks, deadline = association
        checks.extend(extra_checks)
        expiry = min(expiry, deadline)
        fresh = [p for p in proposals if p.status == "pending"]
        state = next(
            (from_record(r, ScribeState) for r in request.puts if r.entity_type == "scribe_state"),
            None,
        )
        if len(fresh) != 1 or state is None or state.pending_proposal_id != fresh[0].id:
            return None
        if any(p is not fresh[0] and p.status != "superseded" for p in proposals):
            return None
        if fresh[0].version != 1:
            changed = fresh[0]
            old_row = store.get(scope, "scribe_proposal", changed.id)
            state_row = store.get(scope, "scribe_state", "current")
            if old_row is None or state_row is None:
                return None
            old = from_record(old_row, Proposal)
            if (
                len(proposals) != 1
                or old.status != "pending"
                or old.expires_at <= now
                or changed.version != old.version + 1
                or changed.expires_at != old.expires_at
                or changed.selected_patient_id != old.selected_patient_id
                or changed.creating_patient != old.creating_patient
                or not _patient_revision(old, changed)
                or state_row.body.get("pending_proposal_id") != old.id
                or not (changed.corrected or changed.pending_reply)
            ):
                return None
            checks.extend(
                (Check(old_row.key, old_row.version), Check(state_row.key, state_row.version))
            )
            expiry = min(expiry, old.expires_at)
    elif kind in {"ScribeAction", "ScribeExpire", "ScribeInvalidate"}:
        if len(proposals) != 1:
            return None
        changed = proposals[0]
        old_row = store.get(scope, "scribe_proposal", changed.id)
        if old_row is None:
            return None
        old = from_record(old_row, Proposal)
        if old.status != "pending" or changed.version != old.version + 1:
            return None
        checks.append(Check(old_row.key, old_row.version))
        if kind == "ScribeExpire":
            if old.expires_at > now or changed.status != "expired" or request.intents:
                return None
        elif kind == "ScribeInvalidate":
            if (
                doctor.status == "approved"
                or changed.status != "rejected"
                or changed.reason != "authority_changed"
                or request.intents
            ):
                return None
        else:
            current = store.get(scope, "scribe_state", "current")
            if (
                current is None
                or current.body.get("pending_proposal_id") != old.id
                or old.expires_at <= now
            ):
                return None
            checks.append(Check(current.key, current.version))
            expiry = min(expiry, old.expires_at)
            nonce = command.payload.get("nonce_hash")
            if nonce:
                raw = store.get(scope, "scribe_callback", str(nonce))
                if raw is None:
                    return None
                token = from_record(raw, ScribeCallback)
                consumed = next(
                    (
                        r
                        for r in request.puts
                        if r.entity_type == "scribe_callback" and r.id == token.id
                    ),
                    None,
                )
                if (
                    token.proposal_id != old.id
                    or token.proposal_version != old.version
                    or token.actor_subject != actor.subject
                    or token.expires_at <= now
                    or token.consumed_at is not None
                    or consumed is None
                    or not consumed.body.get("consumed_at")
                    or token.action == "confirm"
                ):
                    return None
                if (
                    token.action == "edit"
                    and not changed.editing
                    or token.action == "reject"
                    and changed.status != "rejected"
                    or token.action == "select"
                    and token.patient_id not in {c.patient_id for c in old.choices}
                ):
                    return None
                checks.append(Check(raw.key, raw.version))
            elif changed.status != "rejected":
                return None
    if command.work_claim:
        claim = command.work_claim
        receipt = store.get(claim.record_key.scope, "inbound_receipt", claim.record_key.pk)
        if (
            receipt is None
            or receipt.body.get("source_subject") != doctor.telegram_user_id
            or receipt.body.get("source_chat") != doctor.private_chat_id
        ):
            return None
        expiry = min(expiry, claim.expires_at)
    if kind == "ScribeConfirm":
        proposal_row = store.get(
            scope, "scribe_proposal", str(command.payload.get("proposal_id", ""))
        )
        token_row = store.get(scope, "scribe_callback", str(command.payload.get("nonce_hash", "")))
        state_row = store.get(scope, "scribe_state", "current")
        if proposal_row is None or token_row is None or state_row is None:
            return None
        proposal, token, state = (
            from_record(proposal_row, Proposal),
            from_record(token_row, ScribeCallback),
            from_record(state_row, ScribeState),
        )
        if (
            proposal.status != "pending"
            or proposal.expires_at <= now
            or proposal.editing
            or proposal.pending_reply
            or token.id != proposal.confirmation_nonce_hash
            or token.action != "confirm"
            or token.consumed_at
            or token.actor_subject != actor.subject
            or token.proposal_id != proposal.id
            or token.proposal_version != proposal.version
            or token.expires_at <= now
            or state.pending_proposal_id != proposal.id
        ):
            return None
        expiry = min(expiry, proposal.expires_at, token.expires_at)
        checks.extend(Check(r.key, r.version) for r in (proposal_row, token_row, state_row))
        patient_rows = [r for r in request.puts if r.entity_type == "patient"]
        noop = (
            not patient_rows
            and bool(proposal.amendments)
            and all(a.noop for a in proposal.amendments)
            and not (
                proposal.candidate.facts
                or proposal.candidate.missions
                or proposal.candidate.alerts
                or proposal.creating_patient
            )
        )
        if len(patient_rows) != 1 and not noop:
            return None
        if noop:
            target_scope = PatientScope(
                doctor_id=scope.doctor_id, patient_id=proposal.selected_patient_id or ""
            )
            current_patient = store.get(target_scope, "patient", target_scope.patient_id)
            if current_patient is None or any(
                r.entity_type in CLINICAL_TYPES - {"patient_media", "photo_association_work"}
                for r in request.puts
            ):
                return None
            patient = from_record(current_patient, Patient)
        else:
            patient = from_record(patient_rows[0], Patient)
        target = patient.scope
        if proposal.creating_patient:
            if patient.version != 1 or proposal.blocked("patient"):
                return None
        elif proposal.selected_patient_id != target.patient_id:
            return None
        else:
            if command.fence is None or command.fence.scope != target:
                return None
            for ref in proposal.base_versions:
                current = store.get(target, ref.entity_type, ref.id)
                if current is None or current.version != ref.version:
                    return None
                checks.append(Check(current.key, current.version))
        for written in request.puts:
            actual = model_scope(from_record(written, MODELS[written.entity_type]))
            if isinstance(actual, PatientScope) and actual != target:
                return None
        photo_work = [r for r in request.puts if r.entity_type == "photo_association_work"]
        photo_media = [r for r in request.puts if r.entity_type == "patient_media"]
        if proposal.photo:
            from sanad.store.records import (
                IntakeDraft,
                MediaWork,
                PatientMedia,
                PhotoAssociationWork,
            )

            photo = proposal.photo
            draft_row = store.get(scope, "intake_draft", photo.intake_id)
            if len(photo_work) != 1 or draft_row is None:
                return None
            photo_task = from_record(photo_work[0], PhotoAssociationWork)
            draft = from_record(draft_row, IntakeDraft)
            if (
                photo_task.version != 1
                or photo_task.state != "pending"
                or photo_task.patient_id != target.patient_id
                or photo_task.proposal_id != proposal.id
                or photo_task.intake_id != photo.intake_id
                or draft.state != "associated"
                or draft.reads != photo.reads
                or draft.media_work_ids != photo.media_work_ids
                or {r.id for r in photo_media} != set(photo.media_work_ids)
            ):
                return None
            checks.append(Check(draft_row.key, draft_row.version))
            for media_row in photo_media:
                media = from_record(media_row, PatientMedia)
                source = store.get(media.media_scope, "media_work", media.media_work_id)
                if source is None:
                    return None
                stored = from_record(source, MediaWork)
                if (
                    media.media_scope != IntakeScope(doctor_id=scope.doctor_id, intake_id=draft.id)
                    or media.source_receipt_id != stored.receipt_id
                    or media.kind != photo.kind
                    or media.mime != stored.mime
                ):
                    return None
                checks.append(Check(source.key, source.version))
        elif photo_work or photo_media:
            return None
        changed_row = next((r for r in request.puts if r.entity_type == "scribe_proposal"), None)
        consumed = next((r for r in request.puts if r.entity_type == "scribe_callback"), None)
        if (
            changed_row is None
            or changed_row.id != proposal.id
            or changed_row.body.get("status") != "confirmed"
            or changed_row.body.get("candidate") != proposal_row.body.get("candidate")
            or consumed is None
            or consumed.id != token.id
            or not consumed.body.get("consumed_at")
        ):
            return None
        from sanad.scribe.memory import confirmation_names

        confirmed_at = from_record(changed_row, Proposal).updated_at
        expected_names = confirmation_names(store, doctor, proposal, lambda: confirmed_at)
        supplied_names = tuple(
            from_record(r, NameMemory) for r in request.puts if r.entity_type == "name_memory"
        )
        if {(keys.partition(n.scope), n.id): n for n in expected_names} != {
            (keys.partition(n.scope), n.id): n for n in supplied_names
        }:
            return None
    return checks, expiry
