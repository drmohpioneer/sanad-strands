"""The ordinary fenced worker: strong reads, pure transitions, one effect commit."""

from datetime import datetime
from typing import cast
from uuid import uuid4

from pydantic import JsonValue, ValidationError

import sanad.domain.events as ev
from sanad.domain import (
    FollowUpTask,
    Mission,
    PatientScope,
    Principal,
    ReviewObligation,
    VersionRef,
    transition_followup,
    transition_mission,
    transition_review,
)
from sanad.domain.entities import TERMINAL_STATES
from sanad.domain.operations import AccountabilityWake
from sanad.steward.apply import CommitBuilder, EffectsRejected, build_commit
from sanad.steward.types import Clock, CommandResult, PolicyProvider, command_result, records
from sanad.store import keys
from sanad.store.protocol import Store
from sanad.store.records import (
    Claim,
    CommandEnvelope,
    DoctorAuthority,
    InboundReceipt,
    OrderAuthority,
    PatientProfile,
    WorkerCapability,
    from_record,
    to_record,
)

SUPPORTED = frozenset(
    {
        "ConfirmProposal",
        "ExtendMission",
        "ReopenMission",
        "CancelMission",
        "CloseUnfulfilledMission",
        "AcknowledgeReview",
        "ResolveReview",
        "RecordPatientReply",
        "RecordObjectiveFulfilled",
        "CorrectEvidence",
        "CorrectRecord",
        "ValidateCorrection",
        "PreviewReopen",
        "ConfirmReopen",
        "CorrectionResponse",
        "AmendOrder",
        "SetContactPreference",
        "AssociateEvidence",
        "RejectEvidence",
        "AnswerQuestion",
        "SendQuestion",
        "DeferQuestion",
        "ReuseAnswer",
        "AcceptTask",
        "ReopenTask",
    }
)
INTERNAL = frozenset(
    {
        "_Deadline",
        "_FollowupDeadline",
        "_Wake",
        "_ScheduleContact",
        "_ContactFeedback",
        "_EvidenceTurn",
    }
)
DOCTOR_COMMANDS = SUPPORTED - {
    "RecordPatientReply",
    "RecordObjectiveFulfilled",
    "SetContactPreference",
}


def system_command(
    scope: PatientScope,
    id: str,
    payload: dict[str, JsonValue],
    now: datetime,
    *,
    lane: str = "mission",
) -> CommandEnvelope:
    subject = "steward:" + lane
    from datetime import timedelta

    return CommandEnvelope(
        command_id=id,
        scope=scope,
        principal=Principal(subject=subject, actor_kind="system", doctor_id=scope.doctor_id),
        worker=WorkerCapability(
            service_subject=subject,
            permitted_lanes=frozenset({lane}),
            resolved_scope=scope,
            auth_expiry=now + timedelta(minutes=10),
            invocation_id=id,
        ),
        payload=payload,
        requested_at=now,
    )


class InvalidCommandPayload(ValueError):
    pass


class Steward:
    def __init__(self, store: Store, clock: Clock, policy_provider: PolicyProvider):
        self.store, self.clock, self.policy_provider = store, clock, policy_provider

    def prepare_confirmation(
        self,
        command: CommandEnvelope,
        proposed: ev.ProposalCreated,
        profile: PatientProfile,
    ) -> tuple[Mission, tuple[FollowUpTask, ...]]:
        """Prepare ConfirmProposal effects for an atomic Scribe batch, without writes.

        The proposed version exists only on the doctor's card. Its first stored
        version is confirmed; the store checks authority for the whole batch.
        """
        from sanad.domain import create_followup, create_proposed_mission
        from sanad.domain.boundaries import ObservationRef, TimingProposal
        from sanad.domain.deadlines import ExplicitTiming

        scope = PatientScope(doctor_id=profile.doctor_id, patient_id=profile.patient_id)
        if command.payload.get("type") != "ConfirmProposal" or command.scope != scope:
            raise EffectsRejected("confirmation_scope")
        draft = create_proposed_mission(proposed)
        if not isinstance(draft, ev.TransitionResult) or not isinstance(draft.aggregate, Mission):
            raise EffectsRejected("mission_draft_invalid")
        timing = proposed.timing
        payload: dict[str, JsonValue] = {"type": "ConfirmProposal"}
        if timing.due_source == "doctor":
            payload["explicit"] = ExplicitTiming(
                instant=timing.due_at,
                original_expression=timing.original_time_expression or "",
                timezone=timing.timezone,
            ).model_dump(mode="json")
        else:
            payload["proposal"] = TimingProposal(
                proposed_due_at=timing.due_at,
                source="default" if timing.due_source == "default" else "scribe",
                reason=timing.due_reason,
                timezone=timing.timezone,
                anchor_time=timing.timing_anchor.instant,
                anchor_kind="observation_received",
                policy_version=timing.policy_version,
                source_observation_ref=ObservationRef(
                    observation_id=str(command.payload["source_receipt_id"])
                ),
            ).model_dump(mode="json")
        translated = CommandEnvelope.model_validate(command.model_dump() | {"payload": payload})
        event = self._event(translated, draft.aggregate, profile)
        assert isinstance(event, ev.ConfirmMission)
        policy = self.policy_provider(scope).timing
        result = transition_mission(draft.aggregate, event, self.clock(), policy)
        if not isinstance(result, ev.TransitionResult):
            raise EffectsRejected("mission_confirmation_invalid")
        mission = Mission.model_validate(result.aggregate.model_dump() | {"version": 1})
        from sanad.contact.scheduler import prime

        mission = prime(mission, self.clock())
        followups: list[FollowUpTask] = []
        for effect in result.effects:
            if isinstance(effect, ev.CreateFollowUp):
                child = create_followup(effect, self.clock(), policy)
                if not isinstance(child, ev.TransitionResult) or not isinstance(
                    child.aggregate, FollowUpTask
                ):
                    raise EffectsRejected("followup_confirmation_invalid")
                followups.append(child.aggregate)
        return mission, tuple(followups)

    def handle(self, command: CommandEnvelope) -> CommandResult:
        from sanad.domain import TenantScope
        from sanad.steward.reviews import handle as review_handle
        from sanad.store.keys import IntakeScope

        if command.payload.get("type") in {"AcknowledgeReview", "ResolveReview"} and type(
            command.scope
        ) in {TenantScope, IntakeScope}:
            return review_handle(self, command)
        if not isinstance(command.scope, PatientScope):
            return CommandResult(status="unsupported", reason_code="patient_scope_required")
        scope = command.scope
        now = self.clock()
        kind = command.payload.get("type")
        if not isinstance(kind, str) or kind not in SUPPORTED | INTERNAL:
            return CommandResult(status="unsupported")
        doctor_record = self.store.get(scope, "doctor_authority", scope.doctor_id)
        profile = self.store.get_patient_profile(scope)
        if doctor_record is None or profile is None:
            return CommandResult(status="forbidden")
        doctor = from_record(doctor_record, DoctorAuthority)
        actor = command.principal
        if kind == "SetContactPreference" and actor.actor_kind != "patient":
            return CommandResult(status="forbidden")
        if actor.actor_kind == "system":
            worker = command.worker
            if (
                worker is None
                or worker.service_subject != actor.subject
                or worker.resolved_scope != scope
                or worker.auth_expiry <= now
                or kind not in INTERNAL | {"RecordPatientReply", "RecordObjectiveFulfilled"}
            ):
                return CommandResult(status="forbidden")
        elif (
            (kind in INTERNAL and kind != "_EvidenceTurn")
            or actor.doctor_id != scope.doctor_id
            or actor.actor_kind not in actor.verified_roles
        ):
            return CommandResult(status="forbidden")
        elif actor.actor_kind == "doctor":
            if (
                not doctor.approved
                or actor.subject != doctor.subject
                or actor.auth_epoch != doctor.auth_epoch
            ):
                return CommandResult(status="forbidden")
        elif actor.actor_kind == "patient":
            if (
                (kind in DOCTOR_COMMANDS and kind != "_EvidenceTurn")
                or actor.patient_id != scope.patient_id
                or not profile.binding_active
                or actor.subject != profile.recipient_subject
                or (kind != "_EvidenceTurn" and actor.auth_epoch != profile.recipient_auth_epoch)
            ):
                return CommandResult(status="forbidden")
            if kind == "_EvidenceTurn":
                from sanad.concierge.plan import authorized

                auth = self.store.authorize(actor.bot_id or "", actor.subject)
                if not auth.binding or authorized(self.store, actor, auth.binding, now) is None:
                    return CommandResult(status="forbidden")
            if not doctor.approved and kind != "SetContactPreference":
                return CommandResult(status="forbidden")
        else:
            return CommandResult(status="forbidden")
        if kind in {"AcknowledgeReview", "ResolveReview"}:
            from sanad.store.reviews import doctor_checks

            if doctor_checks(self.store, actor, scope.doctor_id) is None:
                return CommandResult(status="forbidden", reason_code="doctor_authority_changed")
        from sanad.steward.corrections import COMMANDS as CORRECTION_COMMANDS

        if kind in CORRECTION_COMMANDS or kind == "CorrectEvidence":
            from sanad.store.corrections import doctor_checks as correction_doctor_checks

            if correction_doctor_checks(self.store, actor, scope, now) is None:
                return CommandResult(status="forbidden", reason_code="doctor_authority_changed")
        if kind == "CorrectEvidence":
            return CommandResult(
                status="invalid_input", reason_code="use_versioned_correction_workflow"
            )
        prior = self.store.lookup_command(command)
        if prior is not None:
            return command_result(prior)
        if kind in CORRECTION_COMMANDS:
            if any(
                getattr(command, field) is not None and getattr(command, field) != actual
                for field, actual in (
                    ("expected_binding_epoch", profile.binding_epoch),
                    ("expected_delivery_epoch", profile.delivery_epoch),
                )
            ):
                return CommandResult(status="stale_version", reason_code="authority_epoch")
            from sanad.evidence.correction_screen import screen as screen_correction

            try:
                screen_correction(self, command)
            except (ValidationError, EffectsRejected):
                return CommandResult(status="invalid_input", reason_code="invalid_correction")
        policy = self.policy_provider(scope)
        # An explicitly supplied old fence is never replaced with a fresh authority token.
        lease = command.fence or self.store.acquire_patient(
            scope, "command:" + uuid4().hex, now, policy.operations.lease_ttl
        )
        if lease is None:
            return CommandResult(status="stale_version", reason_code="patient_busy")
        try:
            profile = self.store.get_patient_profile(scope)
            doctor_record = self.store.get(scope, "doctor_authority", scope.doctor_id)
            if profile is None or doctor_record is None:
                return CommandResult(status="forbidden")
            doctor = from_record(doctor_record, DoctorAuthority)
            if actor.actor_kind == "doctor" and (
                not doctor.approved
                or actor.subject != doctor.subject
                or actor.auth_epoch != doctor.auth_epoch
            ):
                return CommandResult(status="forbidden")
            if actor.actor_kind == "patient" and (
                not profile.binding_active
                or actor.subject != profile.recipient_subject
                or (kind != "_EvidenceTurn" and actor.auth_epoch != profile.recipient_auth_epoch)
                or (not doctor.approved and kind != "SetContactPreference")
            ):
                return CommandResult(status="forbidden")
            for field, actual in (
                ("expected_auth_epoch", doctor.auth_epoch),
                ("expected_binding_epoch", profile.binding_epoch),
                ("expected_consent_version", profile.consent_version),
                ("expected_safety_epoch", profile.safety_epoch),
                ("expected_delivery_epoch", profile.delivery_epoch),
            ):
                if getattr(command, field) is not None and getattr(command, field) != actual:
                    return CommandResult(status="stale_version", reason_code="authority_epoch")
            command = CommandEnvelope.model_validate(
                command.model_dump()
                | {
                    "fence": lease,
                    "expected_safety_epoch": profile.safety_epoch,
                    "expected_delivery_epoch": profile.delivery_epoch,
                    "expected_binding_epoch": profile.binding_epoch,
                    "expected_consent_version": profile.consent_version,
                    "expected_auth_epoch": doctor.auth_epoch if doctor.approved else None,
                    "expected_versions": tuple(
                        dict.fromkeys(
                            (
                                *command.expected_versions,
                                to_record(profile, scope).ref,
                                doctor_record.ref,
                            )
                        )
                    ),
                }
            )
            if kind in {"AcknowledgeReview", "ResolveReview"}:
                return review_handle(self, command)
            if kind in CORRECTION_COMMANDS:
                from sanad.steward.corrections import prepare as prepare_correction

                builder = CommitBuilder(scope, command, now, policy, self.store)
                try:
                    request = prepare_correction(builder)
                except ValidationError:
                    raise InvalidCommandPayload("invalid_correction_payload") from None
                return command_result(self.store.commit(request))
            if kind == "SetContactPreference":
                return self._preference(command, profile)
            if kind in {"SendQuestion", "DeferQuestion", "ReuseAnswer"}:
                from sanad.concierge.reuse import prepare as prepare_reuse
                from sanad.concierge.reuse import remember

                builder = CommitBuilder(scope, command, now, policy, self.store)
                reason = prepare_reuse(builder, profile)
                remember(builder)
                return command_result(
                    self.store.commit(builder.finish().model_copy(update={"reason_code": reason}))
                )
            if kind in {"AnswerQuestion", "AcceptTask", "ReopenTask"}:
                from sanad.concierge.answer_command import prepare_answer, prepare_task

                builder = CommitBuilder(scope, command, now, policy, self.store)
                reason = (
                    prepare_answer(builder, profile)
                    if kind == "AnswerQuestion"
                    else prepare_task(builder, profile)
                )
                if kind == "AnswerQuestion":
                    from sanad.concierge.reuse import remember

                    remember(builder)
                return command_result(
                    self.store.commit(builder.finish().model_copy(update={"reason_code": reason}))
                )
            if kind == "_ContactFeedback":
                from sanad.contact.feedback import prepare

                builder = CommitBuilder(scope, command, now, policy, self.store)
                prepare(builder, profile)
                return command_result(self.store.commit(builder.finish()))
            if command.payload.get("executor") == "evidence-v1":
                from sanad.evidence.commit import prepare as prepare_evidence

                builder = CommitBuilder(scope, command, now, policy, self.store)
                prepare_evidence(builder)
                return command_result(self.store.commit(builder.finish()))
            return self._transition(command, profile)
        except (InvalidCommandPayload, EffectsRejected) as error:
            # Only validation failures are translated. Storage/network failures propagate.
            return CommandResult(
                status="invalid_input",
                reason_code="invalid_payload"
                if not isinstance(error, EffectsRejected)
                else str(error),
            )
        finally:
            if command.fence is None or command.fence == lease:
                self.store.release_patient(lease)

    def _load(self, command: CommandEnvelope) -> ev.Aggregate | None:
        scope = cast(PatientScope, command.scope)
        choices = [
            (key, kind)
            for key, kind in (
                ("mission_id", "mission"),
                ("followup_id", "followup"),
                ("review_id", "review"),
            )
            if key in command.payload
        ]
        if len(choices) != 1:
            return None
        key, kind = choices[0]
        id = command.payload[key]
        if not isinstance(id, str):
            return None
        record = self.store.get(scope, kind, id)
        if record is None:
            return None
        if kind == "mission":
            return from_record(record, Mission)
        if kind == "followup":
            return from_record(record, FollowUpTask)
        return from_record(record, ReviewObligation)

    def _transition(self, command: CommandEnvelope, profile: PatientProfile) -> CommandResult:
        scope = cast(PatientScope, command.scope)
        policy, now = self.policy_provider(scope), self.clock()
        aggregate = self._load(command)
        if aggregate is None:
            return CommandResult(
                status="invalid_input", reason_code="aggregate_missing_or_ambiguous"
            )
        if isinstance(aggregate, Mission) and aggregate.details.kind == "QUESTION":
            from sanad.concierge.answer_command import prepare_answer, reviews

            question_builder = CommitBuilder(scope, command, now, policy, self.store)
            if command.payload.get("type") == "CancelMission" and reviews(
                question_builder, aggregate
            ):
                return CommandResult(
                    status="needs_confirmation", reason_code="question_requires_close"
                )
            if command.payload.get("type") == "_Wake" and aggregate.details.held_answer_ready:
                reason = prepare_answer(question_builder, profile, release=True)
                return command_result(
                    self.store.commit(
                        question_builder.finish().model_copy(update={"reason_code": reason})
                    )
                )
        translated: list[VersionRef] = []
        for ref in command.expected_versions:
            record = self.store.get(scope, ref.entity_type, ref.id)
            if record is None:
                return CommandResult(status="stale_version")
            if record.version != ref.version:
                # The only allowed advancement is this command's own pre-deadline commit.
                if not (
                    isinstance(aggregate, Mission)
                    and ref.entity_type == "mission"
                    and ref.id == aggregate.id
                    and record.version == ref.version + 1
                    and aggregate.latest_deadline_notice_event_id
                    == command.command_id + ":deadline"
                ):
                    return CommandResult(status="stale_version")
            translated.append(record.ref)
        command = CommandEnvelope.model_validate(
            command.model_dump() | {"expected_versions": tuple(translated)}
        )
        if (
            isinstance(aggregate, Mission)
            and aggregate.state not in TERMINAL_STATES | {"proposed"}
            and aggregate.escalation_at <= now
            and aggregate.handled_deadline_generation < aggregate.deadline_generation
        ):
            deadline = transition_mission(
                aggregate,
                ev.DeadlineReached(event_id=command.command_id + ":deadline"),
                now,
                policy.timing,
            )
            child = CommandEnvelope.model_validate(
                command.model_dump()
                | {
                    "command_id": command.command_id + ":deadline",
                    "payload": {"type": "_Deadline", "mission_id": aggregate.id},
                }
            )
            builder = CommitBuilder(scope, child, now, policy, self.store)
            builder.add(deadline)
            claim = command.work_claim
            if claim is not None and claim.record_key.pk.startswith("IN#"):
                receipt_record = self.store.get(scope, "inbound_receipt", claim.record_key.pk)
                if receipt_record is None or receipt_record.version != claim.version:
                    return CommandResult(status="stale_version")
                receipt = from_record(receipt_record, InboundReceipt)
                builder.put(
                    to_record(
                        InboundReceipt.model_validate(
                            receipt.model_dump()
                            | {
                                "version": receipt.version + 1,
                                "updated_at": now,
                            }
                        ),
                        scope,
                    )
                )
            committed = command_result(self.store.commit(builder.finish(complete_receipt=False)))
            if committed.status != "accepted":
                return committed
            assert isinstance(deadline, ev.TransitionResult)
            aggregate = deadline.aggregate
            if claim is not None:
                claim = (
                    Claim.model_validate(claim.model_dump() | {"version": claim.version + 1})
                    if claim.record_key.pk.startswith("IN#")
                    else None
                )
            command = CommandEnvelope.model_validate(
                command.model_dump()
                | {
                    "work_claim": claim,
                    "expected_versions": tuple(
                        VersionRef(
                            entity_type=r.entity_type,
                            id=r.id,
                            version=aggregate.version
                            if r.entity_type == aggregate.entity_type and r.id == aggregate.id
                            else r.version,
                        )
                        for r in command.expected_versions
                    ),
                }
            )
        if command.payload.get("type") == "_ScheduleContact":
            from sanad.contact.scheduler import prepare

            if not isinstance(aggregate, (Mission, FollowUpTask)):
                return CommandResult(status="invalid_input")
            builder = CommitBuilder(scope, command, now, policy, self.store)
            prepare(builder, aggregate, profile)
            return command_result(self.store.commit(builder.finish()))
        try:
            event = self._event(command, aggregate, profile)
        except ValidationError as error:
            raise InvalidCommandPayload("invalid_payload") from error
        if isinstance(event, (ev.ConfirmMission, ev.DoctorReopen)):
            assert isinstance(aggregate, Mission)
            order_refs = (
                event.new_order_refs if isinstance(event, ev.DoctorReopen) else aggregate.order_refs
            )
            for ref in order_refs:
                order = self.store.get(scope, "care_order", ref.id)
                if order is None or order.ref != ref or order.body.get("status") != "active":
                    return CommandResult(
                        status="needs_confirmation", reason_code="current_order_required"
                    )
            command = CommandEnvelope.model_validate(
                command.model_dump()
                | {
                    "expected_versions": tuple(
                        dict.fromkeys((*command.expected_versions, *order_refs))
                    ),
                }
            )
        if isinstance(aggregate, Mission):
            result = transition_mission(
                aggregate, cast(ev.MissionEvent | AccountabilityWake, event), now, policy.timing
            )
        elif isinstance(aggregate, FollowUpTask):
            result = transition_followup(
                aggregate, cast(ev.FollowUpEvent | AccountabilityWake, event), now, policy.timing
            )
        else:
            result = transition_review(
                aggregate, cast(ev.ReviewEvent | AccountabilityWake, event), now, policy.timing
            )
        if isinstance(result, ev.TransitionRejected) and not result.effects:
            return CommandResult(status="needs_confirmation", reason_code=result.reason_code)
        if (
            isinstance(aggregate, Mission)
            and aggregate.kind == "QUESTION"
            and isinstance(event, (ev.DoctorExtend, ev.DoctorReopen))
            and isinstance(result, ev.TransitionResult)
        ):
            from sanad.concierge.answer_command import maintain_question_review

            builder = CommitBuilder(scope, command, now, policy, self.store)
            builder.add(result)
            assert isinstance(result.aggregate, Mission)
            maintain_question_review(builder, aggregate, result.aggregate)
            return command_result(self.store.commit(builder.finish()))
        request = build_commit(scope, command, result, now, policy, store=self.store)
        return command_result(self.store.commit(request))

    def _event(
        self, command: CommandEnvelope, aggregate: ev.Aggregate, profile: PatientProfile
    ) -> object:
        data: dict[str, object] = dict(command.payload)
        kind = data.pop("type")
        for key in ("mission_id", "followup_id", "review_id"):
            data.pop(key, None)
        if any(
            k in data
            for k in (
                "event_id",
                "event_type",
                "actor_id",
                "actor_kind",
                "consent_active",
                "current_active_order_refs",
                "open_incident",
            )
        ):
            raise InvalidCommandPayload("caller cannot supply authority facts")
        data["event_id"] = command.command_id
        actor = command.principal.subject
        active = (
            profile.binding_active
            and profile.consent_active
            and profile.consent_version is not None
        )
        scope = cast(PatientScope, command.scope)
        if kind == "ConfirmProposal":
            return ev.ConfirmMission.model_validate(
                data | {"actor_id": actor, "consent_active": active}
            )
        if kind == "ExtendMission":
            return ev.DoctorExtend.model_validate(
                data | {"actor_id": actor, "consent_active": active}
            )
        if kind == "ReopenMission":
            orders = tuple(
                r.ref
                for r in records(self.store, scope, "care_order")
                if from_record(r, OrderAuthority).status == "active"
            )
            return ev.DoctorReopen.model_validate(
                data
                | {"actor_id": actor, "consent_active": active, "current_active_order_refs": orders}
            )
        if kind == "CancelMission":
            if isinstance(aggregate, Mission) and aggregate.kind == "QUESTION":
                from sanad.concierge.answer_command import reviews

                data["question_review_open"] = bool(
                    reviews(
                        CommitBuilder(
                            scope, command, self.clock(), self.policy_provider(scope), self.store
                        ),
                        aggregate,
                    )
                )
            return ev.DoctorCancel.model_validate(data | {"actor_id": actor})
        if kind == "CloseUnfulfilledMission":
            open_incident = any(
                r.body["state"] == "open" for r in records(self.store, scope, "incident")
            )
            return ev.DoctorCloseUnfulfilled.model_validate(
                data | {"actor_id": actor, "open_incident": open_incident}
            )
        if kind == "AcknowledgeReview":
            return ev.AcknowledgeReview.model_validate(data | {"actor_id": actor})
        if kind == "ResolveReview":
            return ev.ResolveReview.model_validate(data | {"actor_id": actor})
        if kind == "RecordPatientReply" and isinstance(aggregate, Mission):
            return ev.PatientReplied.model_validate(data)
        if kind == "RecordObjectiveFulfilled" or (
            kind == "RecordPatientReply" and isinstance(aggregate, FollowUpTask)
        ):
            if isinstance(aggregate, FollowUpTask):
                return ev.ResponseReceived.model_validate(data)
            return ev.ObjectiveFulfilled.model_validate(
                data
                | {
                    "actor_kind": command.principal.actor_kind,
                    "fulfillment_event_id": command.command_id,
                }
            )
        if kind == "CorrectEvidence":
            raise InvalidCommandPayload("use_versioned_correction_workflow")
        if kind == "ValidateCorrection":
            raise InvalidCommandPayload("use_reviewed_validation_workflow")
        if kind == "_Deadline":
            return ev.DeadlineReached.model_validate(data)
        if kind == "_FollowupDeadline":
            return ev.FollowUpDeadline.model_validate(data)
        if kind == "_Wake":
            return AccountabilityWake.model_validate(data)
        raise InvalidCommandPayload("unsupported aggregate command")

    def _preference(self, command: CommandEnvelope, profile: PatientProfile) -> CommandResult:
        scope = cast(PatientScope, command.scope)
        active, reason = command.payload.get("consent_active"), command.payload.get("reason")
        if (
            set(command.payload) != {"type", "consent_active", "reason"}
            or type(active) is not bool
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            return CommandResult(status="invalid_input")
        if active and (not profile.binding_active or profile.consent_version is None):
            return CommandResult(status="needs_confirmation", reason_code="binding_required")
        now, policy = self.clock(), self.policy_provider(scope)
        builder = CommitBuilder(scope, command, now, policy, self.store)
        revised = PatientProfile.model_validate(
            profile.model_dump()
            | {
                "version": profile.version + 1,
                "updated_at": now,
                "consent_active": active,
                "consent_version": (profile.consent_version or 0) + 1,
                "delivery_epoch": profile.delivery_epoch + 1,
            }
        )
        builder.put(to_record(revised, scope))
        builder.audit(
            "CONTACT_PREFERENCE_CHANGED",
            keys.digest(command.command_id),
            (to_record(revised, scope).ref,),
        )
        if not active:
            builder.effect(ev.SuppressRoutineIntents(reason=reason), None)
            for row in records(self.store, scope, "followup"):
                task = from_record(row, FollowUpTask)
                if task.state not in {"fulfilled", "cancelled", "contact_suppressed"}:
                    builder.add(
                        transition_followup(
                            task,
                            ev.SuppressFollowUpContact(
                                event_id=command.command_id + ":" + task.id, reason=reason
                            ),
                            now,
                            policy.timing,
                        )
                    )
        return command_result(self.store.commit(builder.finish()))
