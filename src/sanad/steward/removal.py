"""Authenticated removal and recoverable cancellation, without deleting records."""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from unicodedata import normalize
from uuid import uuid4

from pydantic import BaseModel

from sanad.domain import FollowUpTask, Mission, PatientScope
from sanad.domain import events as ev
from sanad.domain.entities import TERMINAL_STATES
from sanad.domain.transitions import transition_followup, transition_mission
from sanad.steward.apply import CommitBuilder, EffectsRejected, is_routine
from sanad.steward.types import CommandResult, StewardPolicy, command_result, records
from sanad.store import keys
from sanad.store.keys import AccountScope
from sanad.store.protocol import Store
from sanad.store.records import (
    CommandEnvelope,
    CommitRequest,
    Consent,
    Doctor,
    IdentityRead,
    Invitation,
    OperationalClock,
    OutboundIntent,
    Patient,
    PatientBinding,
    PatientClaim,
    PatientRemoval,
    StoredRecord,
    SubjectBinding,
    from_record,
    model_scope,
    to_record,
)

if TYPE_CHECKING:
    from sanad.steward.service import Steward

REFUSED = frozenset(
    {
        "ConfirmProposal",
        "ExtendMission",
        "ReopenMission",
        "CancelMission",
        "CloseUnfulfilledMission",
        "SetContactPreference",
        "AcceptTask",
        "ReopenTask",
        "AmendOrder",
        "PreviewReopen",
        "ConfirmReopen",
    }
)


def normalized_name(value: str) -> str:
    return " ".join(normalize("NFKC", value).casefold().split())


def revised[T: BaseModel](value: T, now: datetime, **changes: object) -> T:
    data = value.model_dump()
    version = int(data["version"]) + 1
    if isinstance(value, Patient):
        changes["record_version"] = version
    return type(value).model_validate(data | changes | {"version": version, "updated_at": now})


def prepare(
    store: Store, command: CommandEnvelope, now: datetime, policy: StewardPolicy
) -> CommitRequest:
    scope = command.scope
    if not isinstance(scope, PatientScope):
        raise EffectsRejected("patient_scope_required")
    profile = store.get_patient_profile(scope)
    patient_row = store.get(scope, "patient", scope.patient_id)
    doctor_row = store.get(scope, "doctor", scope.doctor_id)
    if profile is None or patient_row is None or doctor_row is None:
        raise EffectsRejected("patient_missing")
    patient, doctor = from_record(patient_row, Patient), from_record(doctor_row, Doctor)
    builder = CommitBuilder(scope, command, now, policy, store)
    reads: list[IdentityRead] = []

    def put(value: BaseModel) -> None:
        target = model_scope(value)
        row = to_record(value, target)
        builder.put(row)
        if row.version > 1:
            reads.append(
                IdentityRead(
                    scope=target, entity_type=row.entity_type, id=row.id, version=row.version - 1
                )
            )

    if command.payload.get("type") == "RemovePatient":
        if profile.removed_at:
            raise EffectsRejected("already_removed")
        if set(command.payload) != {"type", "expected_version", "name"}:
            raise EffectsRejected("invalid_removal")
        name = command.payload.get("name")
        if not isinstance(name, str) or normalized_name(name) != normalized_name(
            patient.display_name
        ):
            raise EffectsRejected("patient_name")
        if command.payload["expected_version"] != profile.version - 1:
            raise EffectsRejected("profile_version")
        account = AccountScope(bot_id=doctor.telegram_bot_id)
        epoch = profile.binding_epoch + 1
        if patient.active_binding_id:
            row = store.get(scope, "patient_binding", patient.active_binding_id)
            if row:
                binding = from_record(row, PatientBinding)
                if binding.status == "active":
                    subject_row = store.get(account, "subject_binding", binding.subject)
                    if subject_row is None:
                        raise EffectsRejected("binding_missing")
                    subject = from_record(subject_row, SubjectBinding)
                    if (subject.doctor_id, subject.patient_id) != (
                        scope.doctor_id,
                        scope.patient_id,
                    ):
                        raise EffectsRejected("binding_scope")
                    epoch = binding.binding_epoch + 1
                    put(
                        revised(
                            binding,
                            now,
                            status="revoked",
                            binding_epoch=epoch,
                            reason_code="patient_removed",
                        )
                    )
                    put(revised(subject, now, status="revoked", binding_epoch=epoch))
        if patient.invitation_id:
            row = store.get(account, "invitation", patient.invitation_id)
            if row:
                invitation = from_record(row, Invitation)
                if (invitation.doctor_id, invitation.patient_id) != (
                    scope.doctor_id,
                    scope.patient_id,
                ):
                    raise EffectsRejected("invitation_scope")
                if invitation.state in {"issued", "claimed"}:
                    put(revised(invitation, now, state="revoked", work_clock=None))
                if invitation.pending_claim_id:
                    row = store.get(account, "patient_claim", invitation.pending_claim_id)
                    if row:
                        claim = from_record(row, PatientClaim)
                        if claim.state == "pending":
                            put(revised(claim, now, state="rejected", work_clock=None))
        for row in records(store, scope, "consent"):
            consent = from_record(row, Consent)
            if consent.withdrawn_at is None:
                put(revised(consent, now, withdrawn_at=now))
        put(
            revised(
                patient,
                now,
                contact_status="frozen",
                binding_epoch=epoch,
                delivery_epoch=profile.delivery_epoch + 1,
            )
        )
        put(
            revised(
                profile,
                now,
                removed_at=now,
                removed_by=command.principal.subject,
                purge_due_at=now + timedelta(days=30),
                binding_active=False,
                consent_active=False,
                binding_epoch=epoch,
                delivery_epoch=profile.delivery_epoch + 1,
            )
        )
        put(
            PatientRemoval(
                id=scope.patient_id,
                scope=scope,
                created_at=now,
                updated_at=now,
                work_clock=OperationalClock(next_action_at=now, work_lane="operational"),
            )
        )
        builder.audit(
            "PatientRemoved",
            keys.digest(command.command_id),
            tuple(r.ref for r in builder.puts.values()),
        )
    else:
        if not profile.removed_at:
            raise EffectsRejected("patient_not_removed")
        work_row = store.get(scope, "patient_removal", scope.patient_id)
        if work_row is None:
            raise EffectsRejected("removal_missing")
        work = from_record(work_row, PatientRemoval)
        if work.phase == "completed" or command.payload != {
            "type": "_RemovalBatch",
            "version": work.version,
        }:
            raise EffectsRejected("removal_version")
        phase: str = work.phase
        count = 0
        if phase == "suppress":
            pending = [
                from_record(r, OutboundIntent)
                for r in records(store, scope, "outbound_intent")
                if r.body.get("status") in {"queued", "uncertain"}
            ]
            pending = [i for i in pending if is_routine(i)]
            for intent in pending[:25]:
                put(
                    revised(
                        intent,
                        now,
                        status="suppressed",
                        suppression_reason="patient_removed",
                        work_clock=None,
                    )
                )
                count += 1
            if len(pending) <= 25:
                phase = "cancel"
        else:
            candidates: list[Mission | FollowUpTask] = []
            for row in records(store, scope, "mission"):
                mission = from_record(row, Mission)
                if (
                    mission.state not in TERMINAL_STATES
                    and not mission.danger_history
                    and mission.kind != "QUESTION"
                ):
                    candidates.append(mission)
            candidates.extend(
                from_record(r, FollowUpTask)
                for r in records(store, scope, "followup")
                if r.body["state"] not in {"fulfilled", "cancelled"}
            )
            # Count expanded effects, uniqueness markers and authority/work fences.
            for item in candidates[:25]:
                if (item.entity_type, item.id) in builder.puts:
                    continue
                event_id = command.command_id + ":" + item.id
                result = (
                    transition_mission(
                        item,
                        ev.DoctorCancel(
                            event_id=event_id,
                            actor_id=profile.removed_by or "removal",
                            reason="patient_removed",
                        ),
                        now,
                        policy.timing,
                    )
                    if isinstance(item, Mission)
                    else transition_followup(
                        item,
                        ev.CancelFollowUp(event_id=event_id, reason="patient_removed"),
                        now,
                        policy.timing,
                    )
                )
                trial = CommitBuilder(scope, command, now, policy, store)
                trial.puts = builder.puts.copy()
                trial.events = builder.events.copy()
                trial.intents = builder.intents.copy()
                trial.deadline_reviews = builder.deadline_reviews.copy()
                trial.add(result)
                rows = (*trial.puts.values(), *trial.events.values(), *trial.intents.values())
                operations = (
                    4
                    + len(rows)
                    + sum(
                        r.version == 1 and r.entity_type in {"outbound_intent", "review"}
                        for r in rows
                    )
                )
                if operations > 80 and count:
                    break
                if operations > 100:
                    raise EffectsRejected("removal_transaction_over_100")
                builder = trial
                count += 1
                if operations > 80:
                    break
            if all((item.entity_type, item.id) in builder.puts for item in candidates):
                phase = "completed"
        put(
            revised(
                work,
                now,
                phase=phase,
                processed=work.processed + count,
                work_clock=None
                if phase == "completed"
                else OperationalClock(
                    next_action_at=now + timedelta(seconds=1), work_lane="operational"
                ),
            )
        )
        if not builder.events:
            builder.audit(
                "PatientRemovalProgress",
                keys.digest(command.command_id),
                tuple(r.ref for r in builder.puts.values()),
            )
    all_rows = (*builder.puts.values(), *builder.events.values(), *builder.intents.values())
    return CommitRequest(
        command=command,
        puts=tuple(builder.puts.values()),
        events=tuple(builder.events.values()),
        intents=tuple(builder.intents.values()),
        expected=tuple(r.ref for r in all_rows),
        identity_reads=tuple(reads),
    )


def handle(steward: "Steward", command: CommandEnvelope) -> CommandResult:
    scope = command.scope
    assert isinstance(scope, PatientScope)
    profile = steward.store.get_patient_profile(scope)
    if profile is None:
        return CommandResult(status="forbidden")
    prior = steward.store.lookup_command(command)
    if prior is not None:
        return command_result(prior)
    initial = command.payload.get("type") == "RemovePatient"
    if initial and profile.removed_at:
        return CommandResult(status="accepted", reason_code="already_removed")
    expected = command.payload.get("expected_version") if initial else None
    if initial and (type(expected) is not int or expected != profile.version):
        return CommandResult(status="stale_version", reason_code="profile_version")
    policy = steward.policy_provider(scope)
    lease = steward.store.acquire_patient(
        scope,
        "removal:" + uuid4().hex,
        steward.clock(),
        policy.operations.lease_ttl,
        expected_version=expected if isinstance(expected, int) else None,
    )
    if lease is None:
        return CommandResult(status="stale_version", reason_code="profile_version")
    try:
        profile = steward.store.get_patient_profile(scope)
        assert profile
        fenced = command.model_copy(
            update={
                "requested_at": steward.clock(),
                "fence": lease,
                "expected_versions": (to_record(profile, scope).ref,),
            }
        )
        return command_result(
            steward.store.commit(prepare(steward.store, fenced, fenced.requested_at, policy))
        )
    except EffectsRejected as error:
        return CommandResult(status="invalid_input", reason_code=str(error))
    finally:
        steward.store.release_patient(lease)


def wake(steward: "Steward", row: StoredRecord) -> None:
    from sanad.steward.service import system_command

    work = from_record(row, PatientRemoval)
    if work.phase == "completed":
        return
    command = system_command(
        work.scope,
        f"removal:{work.id}:{work.version}",
        {"type": "_RemovalBatch", "version": work.version},
        steward.clock(),
        lane="operational",
    )
    result = steward.handle(command)
    if result.status != "accepted":
        raise EffectsRejected("removal_batch_" + result.status)


def terminal_request(
    store: Store, command: CommandEnvelope, policy: StewardPolicy
) -> CommitRequest:
    """Finish an authenticated pre-removal receipt/media without reassigning its identity."""
    from sanad.safety.models import ScreenVerdict
    from sanad.steward.apply import make_intent
    from sanad.store.records import DoctorAuthority, InboundReceipt, MediaWork, ReceiptCompletion

    scope, claim = command.scope, command.work_claim
    if not isinstance(scope, PatientScope) or claim is None or claim.record_key.scope != scope:
        raise EffectsRejected("removed_work_scope")
    profile = store.get_patient_profile(scope)
    kind = "media_work" if command.payload.get("type") == "_RemovedMedia" else "inbound_receipt"
    row = store.get(scope, kind, str(command.payload.get("id", "")))
    if not profile or not profile.removed_at or not row or row.key != claim.record_key.key:
        raise EffectsRejected("removed_work_missing")
    if command.payload.get("type") == "_RemovedMedia":
        work = from_record(row, MediaWork)
        changed = revised(
            work, command.requested_at, state="removed", processing_claim=None, work_clock=None
        )
        record = to_record(changed, scope)
        return CommitRequest(
            command=command, puts=(record,), expected=(record.ref,), reason_code="patient_removed"
        )
    receipt = from_record(row, InboundReceipt)
    if (
        receipt.principal is None
        or receipt.principal.actor_kind != "patient"
        or receipt.principal.patient_id != scope.patient_id
        or receipt.principal.doctor_id != scope.doctor_id
        or receipt.source_subject != profile.recipient_subject
        or receipt.source_chat != profile.recipient_ref
        or receipt.safety_result is None
    ):
        raise EffectsRejected("removed_receipt_identity")
    intents: tuple[StoredRecord, ...] = ()
    verdict = ScreenVerdict.model_validate(receipt.safety_result)
    if verdict.level == "danger":
        doctor_row = store.get(scope, "doctor_authority", scope.doctor_id)
        if not doctor_row:
            raise EffectsRejected("doctor_authority_missing")
        source = revised(
            receipt, command.requested_at, state="completed", work_clock=None, processing_claim=None
        )
        intent = make_intent(
            scope,
            receipt.id,
            (to_record(source, scope).ref,),
            "patient_safety_response",
            receipt.id,
            command.requested_at,
            policy,
            from_record(doctor_row, DoctorAuthority),
            profile,
            audience="patient",
            template_id="patient_emergency",
        )
        intents = (to_record(intent, scope),)
    return CommitRequest(
        command=command,
        intents=intents,
        expected=tuple(r.ref for r in intents),
        receipt_completion=ReceiptCompletion(claim=claim, result_event_ids=()),
        reason_code="removed_patient_unbound",
    )


def finish_removed_work(steward: "Steward", row: StoredRecord) -> CommandResult:
    """Acquire both durable work and the patient lease; failed completion stays recoverable."""
    from sanad.steward.service import system_command
    from sanad.store.records import InboundReceipt, MediaWork

    scope = (
        from_record(row, MediaWork).scope
        if row.entity_type == "media_work"
        else from_record(row, InboundReceipt).scope
    )
    if not isinstance(scope, PatientScope):
        return CommandResult(status="forbidden")
    lane = "media" if row.entity_type == "media_work" else "ingress"
    policy, now = steward.policy_provider(scope), steward.clock()
    owner = "removed-work:" + uuid4().hex
    lease = steward.store.acquire_patient(scope, owner, now, policy.operations.lease_ttl)
    if lease is None:
        return CommandResult(status="stale_version")
    try:
        claim = steward.store.claim_work(
            row.scoped_key(scope), row.version, owner, now, policy.operations.claim_ttl
        )
        if claim is None:
            return CommandResult(status="stale_version")
        command = system_command(
            scope,
            f"removed-work:{row.entity_type}:{row.id}",
            {"type": "_RemovedMedia" if lane == "media" else "_RemovedReceipt", "id": row.id},
            now,
            lane=lane,
        )
        command = command.model_copy(update={"fence": lease, "work_claim": claim})
        return command_result(
            steward.store.commit(terminal_request(steward.store, command, policy))
        )
    finally:
        steward.store.release_patient(lease)
