"""Durable receipt boundary. A returned accept result is the caller's ACK boundary."""

from datetime import datetime

from pydantic import JsonValue

import sanad.domain.events as ev
from sanad.domain import PatientScope, Principal, ReviewKind
from sanad.domain.operations import transition_operational_clock
from sanad.steward.apply import CommitBuilder
from sanad.steward.service import Steward, system_command
from sanad.steward.types import CommandResult, command_result
from sanad.store import keys
from sanad.store.keys import ScopedKey
from sanad.store.records import (
    Claim,
    CommandEnvelope,
    InboundAccept,
    InboundReceipt,
    OperationalClock,
    ProcessingClaim,
    from_record,
    to_record,
)


class InboundProcessor:
    def __init__(self, steward: Steward, *, transport: str = "synthetic"):
        self.steward, self.store, self.transport = steward, steward.store, transport

    def accept(
        self,
        transport_key: str,
        payload: dict[str, JsonValue],
        scope: PatientScope,
        now: datetime,
        *,
        principal: Principal,
        source_chat: str,
    ) -> InboundAccept:
        if principal.doctor_id != scope.doctor_id or (
            principal.actor_kind == "patient" and principal.patient_id != scope.patient_id
        ):
            return InboundAccept(status="forbidden")
        receipt = InboundReceipt(
            id=keys.inbound(self.transport, keys.digest(transport_key)).pk,
            scope=scope,
            transport=self.transport,
            transport_key=transport_key,
            source_subject=principal.subject,
            source_chat=source_chat,
            principal=principal,
            channel=self.transport,
            kind=str(payload.get("kind", "unknown")),
            payload=payload,
            received_at=now,
            created_at=now,
            updated_at=now,
            work_clock=OperationalClock(next_action_at=now, work_lane="ingress"),
        )
        return self.store.accept_inbound(transport_key, to_record(receipt, scope))

    def process_inbound(
        self, receipt_key: ScopedKey, owner: str, now: datetime
    ) -> CommandResult | None:
        if not isinstance(receipt_key.scope, PatientScope):
            return None
        scope = receipt_key.scope
        row = self.store.get(scope, "inbound_receipt", receipt_key.pk)
        if row is None or row.key != receipt_key.key:
            return None
        receipt = from_record(row, InboundReceipt)
        if receipt.state == "completed":
            return None
        if receipt.state == "needs_attention":
            return self._rearm_attention(receipt, receipt_key, owner, now)
        policy = self.steward.policy_provider(scope)
        assert receipt.work_clock is not None
        exhausted = receipt.work_clock.attempt_count >= policy.operations.max_inbound_attempts
        claim = self.store.claim_work(
            receipt_key,
            row.version,
            owner,
            now,
            policy.operations.claim_ttl,
            count_attempt=not exhausted,
        )
        if claim is None:
            return None
        current = self.store.get(scope, "inbound_receipt", receipt.id)
        assert current is not None
        receipt = from_record(current, InboundReceipt)
        assert receipt.work_clock is not None
        if exhausted:
            return self._failure(receipt, claim, "attempts_exhausted")
        try:
            if receipt.transport == "telegram":
                return self._defer_telegram(receipt, claim)
            command = self._normalize(receipt, claim)
            result = self.steward.handle(command)
            if result.status != "accepted":
                return self._failure_current(receipt, claim, result.reason_code or result.status)
            latest = self.store.get(scope, "inbound_receipt", receipt.id)
            if latest is not None and latest.body["state"] != "completed":
                # A different receipt carried an already accepted command. Own its completion.
                return self._complete_duplicate(from_record(latest, InboundReceipt), claim)
            return result
        except Exception:
            # Store only a fixed code. Neither patient text nor provider exceptions enter logs.
            self._failure_current(receipt, claim, "processing_exception")
            raise

    def _defer_telegram(self, receipt: InboundReceipt, claim: Claim) -> CommandResult:
        """No aggregate association or hidden media interpretation is released in 05."""
        from sanad.domain import create_review
        from sanad.store.records import CommitRequest

        assert isinstance(receipt.scope, PatientScope)
        scope, now = receipt.scope, self.steward.clock()
        policy = self.steward.policy_provider(scope)
        lease = self.store.acquire_patient(
            scope, "telegram-ordinary", now, policy.operations.lease_ttl
        )
        if lease is None:
            return CommandResult(status="stale_version", reason_code="patient_busy")
        try:
            command = system_command(
                scope, "telegram:" + receipt.id, {"receipt_id": receipt.id}, now, lane="ingress"
            )
            command = CommandEnvelope.model_validate(
                command.model_dump() | {"fence": lease, "work_claim": claim}
            )
            builder = CommitBuilder(scope, command, now, policy, self.store)
            if receipt.provider_media_handle is not None:
                builder.add(
                    create_review(
                        ev.CreateReview(
                            event_id=command.command_id,
                            source_type="inbound_receipt",
                            source_id=receipt.id,
                            source_version=receipt.version + 1,
                            review_kind=ReviewKind.media_failure,
                            owner_doctor_id=scope.doctor_id,
                            patient_id=scope.patient_id,
                            review_at=now + policy.timing.result_review_interval,
                        ),
                        now,
                        policy.timing,
                    )
                )
            builder.audit("telegram_deferred_capability", keys.digest(command.command_id), ())
            request = CommitRequest.model_validate(
                builder.finish().model_dump() | {"reason_code": "deferred_capability"}
            )
            return command_result(self.store.commit(request))
        finally:
            self.store.release_patient(lease)

    def _normalize(self, receipt: InboundReceipt, claim: Claim) -> CommandEnvelope:
        scope = receipt.scope
        if (
            not isinstance(scope, PatientScope)
            or receipt.principal is None
            or receipt.payload is None
        ):
            raise ValueError("recoverable normalized input required")
        data = receipt.payload
        kind = data.get("kind")
        if kind == "command":
            command = CommandEnvelope.model_validate(data.get("command"))
            if (
                command.scope != scope
                or command.principal != receipt.principal
                or command.worker is not None
                or command.work_claim is not None
                or command.fence is not None
            ):
                raise ValueError("untrusted command authority")
            return CommandEnvelope.model_validate(command.model_dump() | {"work_claim": claim})
        target = {k: data[k] for k in ("mission_id", "followup_id") if k in data}
        if len(target) != 1:
            raise ValueError("one aggregate target required")
        if kind == "text":
            if (
                set(data) - {"kind", "text", "mission_id"}
                or not isinstance(data.get("text"), str)
                or not str(data["text"]).strip()
            ):
                raise ValueError("text cannot establish a fulfillment predicate")
            payload: dict[str, JsonValue] = {"type": "RecordPatientReply", **target}
        elif kind == "report":
            if set(data) - {
                "kind",
                "mission_id",
                "followup_id",
                "predicate_result",
                "danger_flag",
                "evidence_refs",
                "source_report_ids",
            }:
                raise ValueError("unsupported report field")
            payload = {
                "type": "RecordObjectiveFulfilled",
                **target,
                "predicate_result": data.get("predicate_result"),
                "danger_flag": data.get("danger_flag", False),
                "objective_received_at": receipt.received_at.isoformat(),
            }
            for key in ("evidence_refs", "source_report_ids"):
                if key in data:
                    payload[key] = data[key]
        else:
            raise ValueError("unsupported inbound kind")
        return CommandEnvelope(
            command_id="receipt:" + keys.digest(receipt.id),
            principal=receipt.principal,
            scope=scope,
            payload=payload,
            requested_at=receipt.received_at,
            work_claim=claim,
        )

    def _failure_current(self, receipt: InboundReceipt, claim: Claim, code: str) -> CommandResult:
        row = self.store.get(receipt.scope, "inbound_receipt", receipt.id)
        if row is None or row.body["state"] == "completed":
            return CommandResult(status="accepted")
        current = from_record(row, InboundReceipt)
        if (
            current.processing_claim is None
            or current.processing_claim.generation != claim.generation
        ):
            return CommandResult(status="stale_version")
        claim = Claim.model_validate(claim.model_dump() | {"version": current.version})
        return self._failure(current, claim, code)

    def _failure(self, receipt: InboundReceipt, claim: Claim, code: str) -> CommandResult:
        assert isinstance(receipt.scope, PatientScope) and receipt.work_clock is not None
        scope, now = receipt.scope, self.steward.clock()
        policy = self.steward.policy_provider(scope)
        lease = self.store.acquire_patient(
            scope, "inbound-disposition", now, policy.operations.lease_ttl
        )
        if lease is None:
            return CommandResult(status="stale_version", reason_code="patient_busy")
        try:
            command = system_command(
                scope,
                f"{receipt.id}:failure:{claim.generation}",
                {"code": code},
                now,
                lane="ingress",
            )
            command = CommandEnvelope.model_validate(
                command.model_dump() | {"fence": lease, "work_claim": claim}
            )
            builder = CommitBuilder(scope, command, now, policy, self.store)
            exhausted = receipt.work_clock.attempt_count >= policy.operations.max_inbound_attempts
            review_id = None
            if exhausted:
                from sanad.domain import create_review

                review = create_review(
                    ev.CreateReview(
                        event_id=command.command_id,
                        review_kind=ReviewKind.media_failure
                        if receipt.kind in {"media", "photo", "audio"}
                        else ReviewKind.intake_clarification,
                        source_type="inbound_receipt",
                        source_id=receipt.id,
                        source_version=receipt.version,
                        review_at=now + policy.timing.result_review_interval,
                        owner_doctor_id=scope.doctor_id,
                        patient_id=scope.patient_id,
                    ),
                    now,
                    policy.timing,
                )
                review_id = review.aggregate.id
                builder.add(review)
            changed = transition_receipt_failure(
                receipt,
                now,
                code,
                next_action_at=now
                + (
                    policy.timing.result_review_interval
                    if exhausted
                    else policy.operations.retry_backoff(receipt.work_clock.attempt_count)
                ),
                review_id=review_id,
            )
            builder.put(to_record(changed, scope))
            builder.audit(
                "inbound_needs_attention" if exhausted else "inbound_retry",
                keys.digest(command.command_id),
                (),
            )
            return command_result(self.store.commit(builder.finish(complete_receipt=False)))
        finally:
            self.store.release_patient(lease)

    def _rearm_attention(
        self, receipt: InboundReceipt, key: ScopedKey, owner: str, now: datetime
    ) -> CommandResult | None:
        """Keep the accepted store's unfinished-receipt clock without reprocessing input."""
        assert isinstance(receipt.scope, PatientScope) and receipt.work_clock is not None
        if receipt.work_clock.next_action_at > now:
            return None
        policy = self.steward.policy_provider(receipt.scope)
        claim = self.store.claim_work(
            key, receipt.version, owner, now, policy.operations.claim_ttl, count_attempt=False
        )
        if claim is None:
            return None
        lease = self.store.acquire_patient(receipt.scope, owner, now, policy.operations.lease_ttl)
        if lease is None:
            return CommandResult(status="stale_version")
        try:
            row = self.store.get(receipt.scope, "inbound_receipt", receipt.id)
            assert row is not None
            current = from_record(row, InboundReceipt)
            command = system_command(
                receipt.scope,
                f"attention:{receipt.id}:{claim.generation}",
                {"receipt_id": receipt.id},
                now,
                lane="ingress",
            )
            command = CommandEnvelope.model_validate(
                command.model_dump() | {"fence": lease, "work_claim": claim}
            )
            builder = CommitBuilder(receipt.scope, command, now, policy, self.store)
            builder.put(
                to_record(
                    transition_receipt_failure(
                        current,
                        now,
                        "needs_attention",
                        next_action_at=now + policy.timing.overdue_review_interval,
                        review_id=current.review_obligation_id,
                    ),
                    receipt.scope,
                )
            )
            builder.audit("inbound_attention_rearmed", command.command_id, ())
            return command_result(self.store.commit(builder.finish(complete_receipt=False)))
        finally:
            self.store.release_patient(lease)

    def _complete_duplicate(self, receipt: InboundReceipt, claim: Claim) -> CommandResult:
        assert isinstance(receipt.scope, PatientScope)
        scope, now = receipt.scope, self.steward.clock()
        policy = self.steward.policy_provider(scope)
        lease = self.store.acquire_patient(
            scope, "duplicate-receipt", now, policy.operations.lease_ttl
        )
        if lease is None:
            return CommandResult(status="stale_version")
        try:
            command = system_command(
                scope, "complete:" + receipt.id, {"receipt_id": receipt.id}, now, lane="ingress"
            )
            command = CommandEnvelope.model_validate(
                command.model_dump() | {"work_claim": claim, "fence": lease}
            )
            builder = CommitBuilder(scope, command, now, policy, self.store)
            builder.audit("inbound_duplicate_command", keys.digest(command.command_id), ())
            return command_result(self.store.commit(builder.finish()))
        finally:
            self.store.release_patient(lease)


def transition_receipt_failure(
    receipt: InboundReceipt,
    now: datetime,
    code: str,
    *,
    next_action_at: datetime,
    review_id: str | None,
) -> InboundReceipt:
    assert receipt.work_clock is not None
    token = receipt.processing_claim
    if token is not None:
        token = ProcessingClaim.model_validate(token.model_dump() | {"expires_at": now})
    return InboundReceipt.model_validate(
        receipt.model_dump()
        | {
            "version": receipt.version + 1,
            "updated_at": now,
            "state": "needs_attention" if review_id else "processing",
            "review_obligation_id": review_id,
            "processing_claim": None if review_id else token,
            "work_clock": transition_operational_clock(
                receipt.work_clock, next_action_at, error=code
            ),
        }
    )
