"""Resolve inert effects before constructing one conditional, atomic store request."""

from datetime import datetime

import sanad.domain.events as ev
from sanad.domain import (
    FollowUpTask,
    Mission,
    PatientScope,
    ReviewAction,
    ReviewObligation,
    VersionRef,
    create_followup,
    create_review,
    transition_followup,
    transition_review,
)
from sanad.steward.types import StewardPolicy, records
from sanad.store import keys
from sanad.store.protocol import Store
from sanad.store.records import (
    AuditEvent,
    CommandEnvelope,
    CommitRequest,
    DoctorAuthority,
    EvidenceAnnotation,
    OperationalClock,
    OutboundIntent,
    PatientProfile,
    ReceiptCompletion,
    StoredRecord,
    from_record,
    to_record,
)


class EffectsRejected(ValueError):
    """A required nested effect cannot be accepted; nothing may be partially committed."""


def audit_id(event_id: str, aggregate: ev.Aggregate) -> str:
    return keys.digest(f"{event_id}|{aggregate.entity_type}|{aggregate.id}")


def is_routine(intent: OutboundIntent) -> bool:
    return intent.audience == "patient" and intent.notification_purpose in {
        "routine_prompt",
        "solicited_reply",
    }


def make_intent(
    scope: PatientScope,
    source_event_id: str,
    source_versions: tuple[VersionRef, ...],
    purpose: str,
    facts_ref: str,
    now: datetime,
    policy: StewardPolicy,
    doctor: DoctorAuthority,
    profile: PatientProfile,
    *,
    audience: str = "doctor",
    slot: str = "",
    order_refs: tuple[VersionRef, ...] = (),
    template_id: str | None = None,
    contact_kind: str | None = None,
    expires_at: datetime | None = None,
) -> OutboundIntent:
    logical = keys.digest(f"{source_event_id}|{audience}|{purpose}|{slot}")
    recipient = doctor.recipient_ref if audience == "doctor" else profile.recipient_ref
    if recipient is None:
        raise EffectsRejected("recipient_authority_missing")
    return OutboundIntent.model_validate(
        {
            "id": logical,
            "scope": scope,
            "scope_kind": "patient",
            "audience": audience,
            "logical_key": logical,
            "source_event_ids": (source_event_id,),
            "source_versions": source_versions,
            "recipient_ref": recipient,
            "notification_purpose": purpose,
            "eligibility_class": "urgent"
            if purpose in {"DANGER", "patient_safety_response"}
            else "routine",
            "payload_ref": facts_ref,
            "payload_digest": keys.digest(facts_ref),
            "conversation_sequence": 0,
            "slot_id": slot or None,
            "expires_at": expires_at or now + policy.timing.overdue_review_interval,
            "contact_kind": contact_kind,
            "contact_feedback": "pending"
            if purpose == "routine_prompt"
            and any(r.entity_type in {"mission", "followup"} for r in source_versions)
            else "not_applicable",
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "delivery_lease_seconds": int(policy.operations.lease_ttl.total_seconds()),
            "work_clock": OperationalClock(next_action_at=now, work_lane="delivery"),
            "recipient_auth_epoch_seen": doctor.auth_epoch
            if audience == "doctor"
            else profile.recipient_auth_epoch,
            "doctor_auth_epoch_seen": doctor.auth_epoch,
            "binding_epoch_seen": profile.binding_epoch,
            "consent_version_seen": profile.consent_version,
            "safety_epoch_seen": profile.safety_epoch,
            "delivery_epoch_seen": profile.delivery_epoch,
            "order_refs": order_refs,
            "template_id": template_id,
        }
    )


class CommitBuilder:
    def __init__(
        self,
        scope: PatientScope,
        command: CommandEnvelope,
        now: datetime,
        policy: StewardPolicy,
        store: Store,
    ):
        self.scope, self.command, self.now = scope, command, now
        self.policy, self.store = policy, store
        self.puts: dict[tuple[str, str], StoredRecord] = {}
        self.events: dict[str, StoredRecord] = {}
        self.intents: dict[str, StoredRecord] = {}
        self.deadline_reviews: dict[tuple[str, str], str] = {}

    def put(self, record: StoredRecord) -> None:
        identity = (record.entity_type, record.id)
        previous = self.puts.get(identity)
        if previous is not None and previous != record:
            raise EffectsRejected("multiple_revisions_in_one_commit")
        self.puts[identity] = record

    def add(
        self, result: ev.TransitionResult | ev.TransitionRejected, *, child: bool = False
    ) -> None:
        if isinstance(result, ev.TransitionRejected):
            if not result.effects:
                raise EffectsRejected(result.reason_code)
            for effect in result.effects:
                self.effect(effect, None)
            self.audit("command_rejected", self.command.command_id, ())
            return
        if result.noop:
            return
        self.put(to_record(result.aggregate, self.scope))
        for effect in result.effects:
            if child and isinstance(effect, ev.RecordAudit):
                effect = ev.RecordAudit.model_validate(
                    effect.model_dump()
                    | {
                        "event_id": audit_id(effect.event_id, result.aggregate),
                    }
                )
            self.effect(effect, result.aggregate)

    def audit(
        self, kind: str, id: str, after: tuple[VersionRef, ...], before: tuple[VersionRef, ...] = ()
    ) -> None:
        self.events[id] = to_record(
            AuditEvent(
                id=id,
                event_id=id,
                command_id=self.command.command_id,
                scope=self.scope,
                event_type=kind,
                actor=self.command.principal,
                accepted_at=self.now,
                created_at=self.now,
                updated_at=self.now,
                aggregate_refs=after,
                before_versions=before,
                after_versions=after,
                policy_versions=(self.policy.timing.policy_version,),
            ),
            self.scope,
        )

    def effect(self, effect: ev.Effect, aggregate: ev.Aggregate | None) -> None:
        if isinstance(effect, ev.RecordAudit):
            assert aggregate is not None
            ref = VersionRef(
                entity_type=aggregate.entity_type, id=aggregate.id, version=effect.after_version
            )
            before = (
                (
                    VersionRef(
                        entity_type=aggregate.entity_type,
                        id=aggregate.id,
                        version=effect.before_version,
                    ),
                )
                if effect.before_version
                else ()
            )
            self.audit(effect.event_type, effect.event_id, (ref,), before)
        elif isinstance(effect, ev.CreateReview):
            result = create_review(effect, self.now, self.policy.timing)
            self.deadline_reviews[effect.source_type, effect.source_id] = result.aggregate.id
            if self.store.get_review(self.scope, result.aggregate.id) is None:
                self.add(result, child=True)  # Same REVIEWKEY as create_or_get_review.
        elif isinstance(effect, ev.CreateFollowUp):
            self.add(create_followup(effect, self.now, self.policy.timing), child=True)
        elif isinstance(effect, ev.AnchorFollowUp):
            tasks = [
                from_record(r, FollowUpTask)
                for r in records(self.store, self.scope, "followup")
                if r.body["parent_mission_id"] == effect.parent_mission_id
                and r.body["kind"] == "MEDICATION_DAY3"
            ]
            if len(tasks) != 1:
                raise EffectsRejected("followup_anchor_ambiguous_or_missing")
            task = tasks[0]
            if task.anchor_time is None:
                self.add(
                    transition_followup(
                        task,
                        ev.AnchorConfirmed(
                            event_id=self.command.command_id + ":anchor",
                            anchor_kind=effect.anchor_kind,
                            anchor_time=effect.anchor_time,
                        ),
                        self.now,
                        self.policy.timing,
                    )
                )
        elif isinstance(effect, ev.ApplyReviewEvent):
            review = self.store.get_review(self.scope, effect.obligation_id)
            if review is None or (
                isinstance(aggregate, Mission) and review.source_mission_id != aggregate.id
            ):
                raise EffectsRejected("review_scope_or_source_mismatch")
            self.add(
                transition_review(review, effect.event, self.now, self.policy.timing), child=True
            )
        elif isinstance(effect, ev.ResolveReviewEffect):
            for row in records(self.store, self.scope, "review"):
                review = from_record(row, ReviewObligation)
                if (review.source_type, review.source_id, review.review_kind) == (
                    effect.source_type,
                    effect.source_id,
                    effect.review_kind,
                ) and review.state != "resolved":
                    self.add(
                        transition_review(
                            review,
                            ev.ResolveReview(
                                event_id=self.command.command_id + ":resolve:" + review.id,
                                action=ReviewAction.extend,
                                expected_source_version=review.source_version,
                                actor_id=self.command.principal.subject,
                                reason=effect.reason,
                            ),
                            self.now,
                            self.policy.timing,
                        )
                    )
        elif isinstance(effect, ev.SuppressRoutineIntents):
            for row in records(self.store, self.scope, "outbound_intent"):
                intent = from_record(row, OutboundIntent)
                if intent.status == "queued" and is_routine(intent):
                    self.put(
                        to_record(
                            OutboundIntent.model_validate(
                                intent.model_dump()
                                | {
                                    "version": intent.version + 1,
                                    "updated_at": self.now,
                                    "status": "suppressed",
                                    "suppression_reason": effect.reason,
                                    "work_clock": None,
                                }
                            ),
                            self.scope,
                        )
                    )
        elif isinstance(effect, ev.EmitIntent):
            assert aggregate is not None
            doctor_record = self.store.get(self.scope, "doctor_authority", self.scope.doctor_id)
            profile = self.store.get_patient_profile(self.scope)
            if doctor_record is None or profile is None:
                raise EffectsRejected("recipient_authority_missing")
            ref = VersionRef(
                entity_type=aggregate.entity_type, id=aggregate.id, version=effect.source_version
            )
            intent = make_intent(
                self.scope,
                effect.source_event_id,
                (ref,),
                effect.purpose,
                effect.facts_ref,
                self.now,
                self.policy,
                from_record(doctor_record, DoctorAuthority),
                profile,
                order_refs=aggregate.order_refs
                if isinstance(aggregate, (Mission, FollowUpTask))
                else (),
                audience=effect.audience,
                slot=effect.slot,
                template_id=effect.template_id,
                contact_kind=effect.contact_kind,
                expires_at=effect.expires_at,
            )
            if effect.purpose == "DEADLINE":
                review_id = self.deadline_reviews.get((aggregate.entity_type, aggregate.id))
                if review_id:
                    intent = intent.model_copy(update={"review_obligation_id": review_id})
            self.intents[intent.id] = to_record(intent, self.scope)
        elif isinstance(
            effect, (ev.RetainObservation, ev.RecordEvidenceAssociation, ev.SupersedeEvidence)
        ):
            assert aggregate is not None
            id = keys.digest(f"{self.command.command_id}|{aggregate.id}|{effect.effect_type}")
            self.put(
                to_record(
                    EvidenceAnnotation(
                        id=id,
                        scope=self.scope,
                        created_at=self.now,
                        updated_at=self.now,
                        source_event_id=self.command.command_id,
                        aggregate_ref=VersionRef(
                            entity_type=aggregate.entity_type,
                            id=aggregate.id,
                            version=aggregate.version,
                        ),
                        annotation=effect,
                    ),
                    self.scope,
                )
            )
        else:
            raise EffectsRejected("unsupported_effect")

    def finish(self, *, complete_receipt: bool = True) -> CommitRequest:
        if not self.events:
            self.audit("command_noop", keys.digest(self.command.command_id + ":noop"), ())
        completion = None
        if (
            self.command.work_claim is not None
            and self.command.work_claim.record_key.pk.startswith("IN#")
            and complete_receipt
        ):
            completion = ReceiptCompletion(
                claim=self.command.work_claim, result_event_ids=tuple(self.events)
            )
        all_records = (*self.puts.values(), *self.events.values(), *self.intents.values())
        return CommitRequest(
            command=self.command,
            expected=tuple(r.ref for r in all_records),
            puts=tuple(self.puts.values()),
            events=tuple(self.events.values()),
            intents=tuple(self.intents.values()),
            receipt_completion=completion,
        )


def build_commit(
    scope: PatientScope,
    command: CommandEnvelope,
    result: ev.TransitionResult | ev.TransitionRejected,
    now: datetime,
    policy: StewardPolicy,
    *,
    store: Store,
) -> CommitRequest:
    builder = CommitBuilder(scope, command, now, policy, store)
    builder.add(result)
    request = builder.finish()
    if isinstance(result, ev.TransitionRejected):
        request = CommitRequest.model_validate(
            request.model_dump()
            | {
                "command_status": "needs_confirmation",
                "reason_code": result.reason_code,
            }
        )
    return request
