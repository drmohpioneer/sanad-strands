"""Shared Scribe read and transaction assembly, with private recoverable outbox."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, JsonValue

from sanad.domain import Principal, TenantScope
from sanad.scribe.proposal import Proposal, ScribeState
from sanad.store import keys
from sanad.store.keys import IntakeScope, Scope
from sanad.store.protocol import Store
from sanad.store.records import (
    AuditEvent,
    Claim,
    CommandEnvelope,
    CommitRequest,
    CommitResult,
    Doctor,
    IdentityRead,
    Lease,
    OperationalClock,
    OutboundIntent,
    ReceiptCompletion,
    WorkerCapability,
    canonical_json,
    from_record,
    model_scope,
    to_record,
)


class ScribeRepository:
    def __init__(self, store: Store, clock: Callable[[], datetime]):
        self.store, self.clock = store, clock

    def load[T: BaseModel](self, scope: Scope, kind: str, id: str, schema: type[T]) -> T | None:
        row = self.store.get(scope, kind, id) if id else None
        return from_record(row, schema) if row else None

    def state(self, scope: TenantScope) -> ScribeState | None:
        return self.load(scope, "scribe_state", "current", ScribeState)

    def pending(self, scope: TenantScope) -> Proposal | None:
        state = self.state(scope)
        return (
            self.load(scope, "scribe_proposal", state.pending_proposal_id or "", Proposal)
            if state
            else None
        )

    def intent(
        self,
        doctor: Doctor,
        template: str,
        payload: dict[str, JsonValue],
        suffix: str,
        *,
        proposal: Proposal | None = None,
        sequence: int = 0,
    ) -> OutboundIntent:
        now = self.clock()
        scope = IntakeScope(doctor_id=doctor.id, intake_id="scribe")
        logical = keys.digest(f"scribe:{suffix}:{template}:{sequence}")
        refs = (to_record(proposal, proposal.scope).ref,) if proposal else ()
        return OutboundIntent(
            id=logical,
            scope=scope,
            scope_kind="intake",
            audience="doctor",
            logical_key=logical,
            source_event_ids=(suffix,),
            source_versions=refs,
            recipient_ref=doctor.private_chat_id,
            recipient_subject=doctor.telegram_user_id,
            bot_id=doctor.telegram_bot_id,
            notification_purpose="solicited_reply",
            eligibility_class="routine",
            payload_ref="scribe:" + logical,
            payload=payload,
            payload_digest=keys.digest(canonical_json(payload).decode()),
            conversation_sequence=sequence,
            expires_at=proposal.expires_at
            if proposal and proposal.status == "pending"
            else now + timedelta(hours=24),
            status="queued",
            delivery_lease_seconds=300,
            recipient_auth_epoch_seen=doctor.auth_epoch,
            doctor_auth_epoch_seen=doctor.auth_epoch,
            template_id=template,
            created_at=now,
            updated_at=now,
            work_clock=OperationalClock(next_action_at=now, work_lane="delivery"),
        )

    def commit(
        self,
        actor: Principal,
        kind: Literal[
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
        ],
        command_id: str,
        models: tuple[BaseModel, ...] = (),
        intents: tuple[OutboundIntent, ...] = (),
        *,
        reads: tuple[IdentityRead, ...] = (),
        claim: Claim | None = None,
        fence: Lease | None = None,
        payload: dict[str, JsonValue] | None = None,
        reason: str | None = None,
    ) -> CommitResult:
        scope = TenantScope(doctor_id=actor.doctor_id or "")
        now = self.clock()
        rows = tuple(to_record(m, model_scope(m)) for m in models)
        outgoing = tuple(to_record(i, i.scope) for i in intents)
        event_id = keys.digest("scribe:" + command_id)
        event = to_record(
            AuditEvent(
                id=event_id,
                event_id=event_id,
                command_id=command_id,
                scope=scope,
                event_type=kind,
                actor=actor,
                accepted_at=now,
                created_at=now,
                updated_at=now,
                aggregate_refs=tuple(r.ref for r in rows),
                after_versions=tuple(r.ref for r in rows),
                policy_versions=("scribe-v1",),
            ),
            scope,
        )
        commit = self.store.raise_intake_concern if kind == "IntakeDanger" else self.store.commit
        return commit(
            CommitRequest(
                command=CommandEnvelope(
                    command_id=command_id,
                    principal=actor,
                    scope=scope,
                    payload={"type": kind, **(payload or {})},
                    requested_at=now,
                    fence=fence,
                    work_claim=claim,
                    worker=WorkerCapability(
                        service_subject=actor.subject,
                        permitted_lanes=frozenset({"scribe"}),
                        resolved_scope=scope,
                        auth_expiry=now + timedelta(minutes=2),
                        invocation_id=command_id,
                    )
                    if kind in {"ScribeExpire", "ScribeInvalidate", "ScribeWork", "IntakeReview"}
                    else None,
                ),
                identity_reads=reads,
                puts=rows,
                intents=outgoing,
                events=(event,),
                expected=tuple(r.ref for r in (*rows, *outgoing, event)),
                receipt_completion=ReceiptCompletion(claim=claim, result_event_ids=(event_id,))
                if claim
                else None,
                reason_code=reason,
            )
        )
