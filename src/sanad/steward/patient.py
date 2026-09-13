"""Atomic accepted patient-turn effects, audit, outbox and receipt completion."""

from secrets import token_urlsafe
from typing import Literal

from pydantic import BaseModel, JsonValue

from sanad.concierge.plan import Snapshot
from sanad.concierge.records import PatientAction
from sanad.domain import Principal, VersionRef
from sanad.steward.apply import CommitBuilder, make_intent
from sanad.steward.service import Steward
from sanad.steward.types import CommandResult, command_result
from sanad.store import keys
from sanad.store.records import (
    Claim,
    CommandEnvelope,
    InboundReceipt,
    Lease,
    PatientProfile,
    canonical_json,
    from_record,
    to_record,
)


class PatientTurnCommit:
    def __init__(
        self,
        steward: Steward,
        snapshot: Snapshot,
        receipt: InboundReceipt,
        principal: Principal,
        claim: Claim,
        lease: Lease,
    ):
        self.steward, self.store, self.snapshot = steward, steward.store, snapshot
        self.receipt, self.principal, self.claim, self.lease = receipt, principal, claim, lease
        self.now = steward.clock()
        self.id = "patient-turn:" + receipt.id
        self.profile: PatientProfile = snapshot.profile
        self.builder = CommitBuilder(
            snapshot.scope,
            CommandEnvelope(
                command_id=self.id,
                principal=principal,
                scope=snapshot.scope,
                requested_at=self.now,
                fence=lease,
                work_claim=claim,
                expected_versions=(*snapshot.expected, *snapshot.order_refs),
                expected_auth_epoch=snapshot.authority.auth_epoch,
                expected_binding_epoch=snapshot.profile.binding_epoch,
                expected_consent_version=snapshot.profile.consent_version,
                expected_delivery_epoch=snapshot.profile.delivery_epoch,
                expected_safety_epoch=snapshot.profile.safety_epoch,
                payload={
                    "type": "ConciergeReply",
                    "executor": "concierge-v1",
                    "receipt_id": receipt.id,
                },
            ),
            self.now,
            steward.policy_provider(snapshot.scope),
            self.store,
        )
        self.buttons: list[JsonValue] = []

    def put(self, model: BaseModel) -> None:
        self.builder.put(to_record(model, self.snapshot.scope))

    def kind(self, name: str) -> None:
        command = self.builder.command
        self.builder.command = CommandEnvelope.model_validate(
            command.model_dump() | {"payload": {**command.payload, "type": name}}
        )

    def button(
        self,
        action: Literal["resume", "start", "quiet_slot"],
        label: str,
        *,
        target: VersionRef | None = None,
        slot: str | None = None,
    ) -> None:
        raw = token_urlsafe(32)
        value = PatientAction(
            id=keys.digest(raw),
            scope=self.snapshot.scope,
            created_at=self.now,
            updated_at=self.now,
            expires_at=self.now
            + self.steward.policy_provider(self.snapshot.scope).timing.overdue_review_interval,
            actor_subject=self.principal.subject,
            action=action,
            target_ref=target,
            slot_id=slot,
            source_receipt_id=self.receipt.id,
            delivery_epoch=self.profile.delivery_epoch,
            binding_epoch=self.profile.binding_epoch,
            consent_version=self.profile.consent_version or 1,
        )
        self.put(value)
        self.buttons.append([{"text": label, "callback_data": raw}])

    def consume(self, token: PatientAction) -> None:
        self.put(
            PatientAction.model_validate(
                token.model_dump()
                | {"version": token.version + 1, "updated_at": self.now, "consumed_at": self.now}
            )
        )

    def finish(self, template: str, text: str, *, emit: bool = True) -> CommandResult:
        from sanad.contact.scheduler import prime
        from sanad.domain import Mission, PatientReplied, TransitionResult, transition_mission

        for mission in self.snapshot.missions:
            if ("mission", mission.id) in self.builder.puts or mission.state not in {
                "open",
                "waiting_patient",
                "blocked",
                "unreachable",
                "overdue",
            }:
                continue
            result = transition_mission(
                mission,
                PatientReplied(event_id=self.id + ":reply:" + mission.id),
                self.now,
                self.builder.policy.timing,
            )
            if isinstance(result, TransitionResult) and isinstance(result.aggregate, Mission):
                self.builder.add(
                    result.model_copy(update={"aggregate": prime(result.aggregate, self.now)})
                )
                # Buttons made during this turn must name its accepted reply revision.
                for key, row in tuple(self.builder.puts.items()):
                    if row.entity_type != "patient_action" or row.version != 1:
                        continue
                    action = from_record(row, PatientAction)
                    if action.target_ref == to_record(mission, self.snapshot.scope).ref:
                        self.builder.puts[key] = to_record(
                            action.model_copy(
                                update={
                                    "target_ref": self.builder.puts[("mission", mission.id)].ref
                                }
                            ),
                            self.snapshot.scope,
                        )
        from sanad.steward.browser_session import advance_preference_session

        advance_preference_session(self)
        self.builder.audit(
            str(self.builder.command.payload["type"]),
            keys.digest(self.id),
            tuple(r.ref for r in self.builder.puts.values()),
        )
        if emit:
            payload: dict[str, JsonValue] = {"text": text}
            if self.buttons:
                payload["reply_markup"] = {"inline_keyboard": self.buttons}
            intent = make_intent(
                self.snapshot.scope,
                self.id,
                tuple(
                    r
                    for r in self.builder.command.expected_versions
                    if r.entity_type == "reusable_answer"
                ),
                "solicited_reply",
                self.id,
                self.now,
                self.builder.policy,
                self.snapshot.authority,
                self.profile,
                audience="patient",
                order_refs=self.snapshot.order_refs,
                template_id=template,
            )
            intent = intent.model_copy(
                update={
                    "payload": payload,
                    "payload_digest": keys.digest(canonical_json(payload).decode()),
                    "recipient_subject": self.principal.subject,
                    "bot_id": self.snapshot.binding.bot_id,
                }
            )
            self.builder.intents[intent.id] = to_record(intent, self.snapshot.scope)
        committed = command_result(self.store.commit(self.builder.finish()))
        if committed.status != "forbidden":
            return committed
        return self.refuse(committed)

    def refuse(self, committed: CommandResult) -> CommandResult:
        """Complete a refused turn with the existing safe reply and no candidate effects."""
        from sanad.safety import render_urgent
        from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT
        from sanad.steward.service import system_command

        now = self.steward.clock()
        command = system_command(
            self.snapshot.scope,
            self.id + ":refused",
            {"type": "_InboundRefused"},
            now,
            lane="ingress",
        ).model_copy(update={"fence": self.lease, "work_claim": self.claim})
        neutral = CommitBuilder(self.snapshot.scope, command, now, self.builder.policy, self.store)
        neutral.audit("patient_turn_refused", keys.digest(command.command_id), ())
        intent = make_intent(
            self.snapshot.scope,
            command.command_id,
            (),
            "solicited_reply",
            command.command_id,
            now,
            self.builder.policy,
            self.snapshot.authority,
            self.snapshot.profile,
            audience="patient",
            order_refs=(),
            template_id="patient_safety_ack",
        )
        payload = {
            "text": render_urgent(
                "patient_safety_ack",
                language=self.snapshot.patient.language,
                gender="u",
                policy=SAFETY_POLICY_V1_CARDIOLOGY_DRAFT,
            )
        }
        intent = intent.model_copy(
            update={
                "payload": payload,
                "payload_digest": keys.digest(canonical_json(payload).decode()),
                "source_event_ids": (self.id,),
                "recipient_subject": self.principal.subject,
                "bot_id": self.snapshot.binding.bot_id,
            }
        )
        neutral.intents[intent.id] = to_record(intent, self.snapshot.scope)
        completed = command_result(self.store.commit(neutral.finish()))
        return committed if completed.status in {"accepted", "duplicate"} else completed
