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


def action_choices(
    store: Store, evidence: Evidence, language: str
) -> list[tuple[str, str | None, str]]:
    """The same current-state action model for Telegram and the browser."""
    from sanad.domain import Mission
    from sanad.evidence import associate

    if evidence.association_state in {"rejected", "superseded", "detached"}:
        return []
    missions = tuple(from_record(row, Mission) for row in records(store, evidence.scope, "mission"))
    if associate.identity_required(evidence):
        return [
            (
                "confirm_identity",
                evidence.mission_id,
                templates.button("confirm_identity", language),
            ),
            ("reject", None, templates.button("reject_identity", language)),
        ]
    choices: list[tuple[str, str | None, str]] = [
        ("associate", m.id, templates.button("associate", language) + ": " + m.title)
        for m in associate.choose(missions, evidence, "")[2]
    ]
    if any(
        m.id == evidence.mission_id
        and m.objective_predicate.kind == "evidence"
        and m.objective_predicate.evaluator == "task_evidence"
        for m in associate.open_missions(missions)
    ):
        choices.append(("accept", evidence.mission_id, templates.button("accept", language)))
    if not any(m.id == evidence.mission_id and m.state == "fulfilled" for m in missions):
        choices.append(("reject", None, templates.button("reject", language)))
    return choices


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
    return steward.handle(
        command_for(
            steward,
            actor,
            evidence,
            action,
            command_id,
            mission_id=mission_id,
            reason=reason,
            token_hash=token_hash,
        )
    )


def command_for(
    steward: Steward,
    actor: Principal,
    evidence: Evidence,
    action: str,
    command_id: str,
    *,
    mission_id: str | None = None,
    reason: str | None = None,
    token_hash: str | None = None,
) -> CommandEnvelope:
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
    return command


def refusal_key(reason: str | None, status: str) -> str:
    if reason in {"identity_not_confirmed", "identity_confirmation_not_pending"}:
        return "doctor_evidence_identity_required"
    if reason == "mission_missing":
        return "doctor_evidence_mission_closed"
    if reason == "evidence_already_decided":
        return "doctor_evidence_already_handled"
    if (
        reason in {"evidence_stale", "evidence_token_stale", "evidence_token_target"}
        or status == "stale_version"
    ):
        return "doctor_evidence_stale"
    return "doctor_evidence_refused"


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
            if value.association_state in {"candidate", "unmatched", "accepted_pending_identity"}:
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
                and row.body.get("evidence_id") == e.evidence_id
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
            "doctor_evidence_already_handled"
            if token.consumed_at
            else "doctor_evidence_action_recorded"
            if status in {"accepted", "duplicate"}
            else refusal_key(result.reason_code, status)
        )
    if receipt.kind == "callback":
        turn.runtime.transport.answer_callback(
            str((receipt.payload or {}).get("callback_query_id", "")), ""
        )
    return turn._reply(
        receipt, actor, claim, key, status, text=templates.render(key, doctor.language)
    )
