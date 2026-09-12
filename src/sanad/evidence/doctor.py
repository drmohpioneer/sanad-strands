"""Scoped doctor decisions shared by session HTTP and Telegram cards."""

from typing import TYPE_CHECKING

from sanad.domain import Principal, TenantScope
from sanad.evidence import templates
from sanad.scribe.patients import panel
from sanad.steward.service import Steward
from sanad.steward.types import CommandResult, records
from sanad.store.protocol import Store
from sanad.store.records import CommandEnvelope, Evidence, EvidenceAction, EvidenceHead, from_record

if TYPE_CHECKING:
    from sanad.channels.telegram.router import RouteResult
    from sanad.scribe.turn import ScribeTurn
    from sanad.store.records import Authorization, InboundReceipt


def owned(store: Store, doctor_id: str) -> tuple[Evidence, ...]:
    values = []
    for patient in panel(store, TenantScope(doctor_id=doctor_id)):
        for row in records(store, patient.scope, "evidence_head"):
            head = from_record(row, EvidenceHead)
            version = store.get(patient.scope, "evidence", f"{head.id}:{head.current_version}")
            if version:
                values.append(from_record(version, Evidence))
    return tuple(values)


def decide(
    steward: Steward,
    actor: Principal,
    evidence: Evidence,
    action: str,
    command_id: str,
    *,
    mission_id: str | None = None,
    reason: str | None = None,
    token_hash: str | None = None,
) -> CommandResult:
    kind = (
        "RejectEvidence"
        if action == "reject"
        else "_EvidenceTurn"
        if action == "doctor_card"
        else "AssociateEvidence"
    )
    command = CommandEnvelope.model_validate(
        {
            "command_id": command_id,
            "scope": evidence.scope,
            "principal": actor,
            "requested_at": steward.clock(),
            "payload": {
                "type": kind,
                "executor": "evidence-v1",
                "action": action,
                "evidence_id": evidence.evidence_id,
                "evidence_version": evidence.version,
                "mission_id": mission_id,
                "reason": reason,
                "token_hash": token_hash,
            },
        }
    )
    return steward.handle(command)


def route(
    turn: "ScribeTurn", receipt: "InboundReceipt", auth: "Authorization"
) -> "RouteResult | None":
    actor = auth.principal
    if actor.actor_kind != "doctor":
        return None
    inbox = (
        receipt.kind == "text" and str((receipt.payload or {}).get("text", "")).strip() == "/inbox"
    )
    listing = (
        receipt.kind == "text"
        and str((receipt.payload or {}).get("text", "")).strip() == "/evidence"
    )
    token_hash = (
        str((receipt.payload or {}).get("callback_token_hash", ""))
        if receipt.kind == "callback"
        else ""
    )
    if not listing and not inbox and not token_hash:
        return None
    evidence = owned(turn.repo.store, actor.doctor_id or "")
    if inbox:
        # Solicited evidence decisions accompany the existing Liaison inbox;
        # its ordinary handler still owns the receipt and all other reviews.
        for value in evidence:
            if value.association_state == "candidate" and value.mission_id:
                decide(
                    turn.runtime.steward,
                    actor,
                    value,
                    "doctor_card",
                    f"evidence-card:{receipt.id}:{value.evidence_id}",
                )
        return None
    match = (
        next(
            (
                (e, row)
                for e in evidence
                if (row := turn.repo.store.get(e.scope, "evidence_action", token_hash))
            ),
            None,
        )
        if token_hash
        else None
    )
    if not listing and not match:
        return None
    claimed = turn._claim(receipt)
    if not claimed:
        from sanad.channels.telegram.router import RouteResult

        return RouteResult(route="busy", status="processing")
    receipt, claim = claimed
    doctor = turn.claims.doctor(actor)
    if doctor is None:
        turn.runtime.accounts.finish_receipt(receipt, claim, result_code="authority_changed")
        from sanad.channels.telegram.router import RouteResult

        return RouteResult(route="refused", status="authority_changed")
    key, status = "doctor_evidence_empty", "empty"
    if listing:
        pending = [
            e
            for e in evidence
            if (
                e.association_state in {"candidate", "unmatched", "accepted_pending_identity"}
                or any(
                    r.body.get("source_type") == "evidence"
                    and r.body.get("source_id") == e.evidence_id
                    and r.body.get("state") != "resolved"
                    for r in records(turn.repo.store, e.scope, "review")
                )
            )
            and not (
                e.category == "monitor_screen"
                and e.required_predicate_results
                and e.required_predicate_results[0].missing == ("slot_assignment",)
            )
        ]
        for value in pending:
            decide(
                turn.runtime.steward,
                actor,
                value,
                "doctor_card",
                f"evidence-card:{receipt.id}:{value.evidence_id}",
            )
        if pending:
            turn.runtime.accounts.finish_receipt(receipt, claim, result_code="evidence_listed")
            from sanad.channels.telegram.router import RouteResult

            return RouteResult(route="doctor", status="evidence_listed")
    elif match:
        value, row = match
        token = from_record(row, EvidenceAction)
        result = decide(
            turn.runtime.steward,
            actor,
            value,
            token.action,
            "evidence-doctor:" + receipt.id,
            mission_id=token.mission_id,
            reason="Doctor rejected via evidence card" if token.action == "reject" else None,
            token_hash=token.id,
        )
        status = result.status
        key = (
            "doctor_evidence_action_recorded"
            if status in {"accepted", "duplicate"}
            else "doctor_evidence_already_handled"
            if token.consumed_at
            else "doctor_evidence_stale"
        )
    if receipt.kind == "callback":
        turn.runtime.transport.answer_callback(
            str((receipt.payload or {}).get("callback_query_id", "")), ""
        )
    return turn._reply(
        receipt, actor, claim, key, status, text=templates.render(key, doctor.language)
    )
