"""Solicited doctor correction commands; notice buttons open current guidance only."""

import shlex
from copy import deepcopy
from typing import TYPE_CHECKING

from pydantic import JsonValue

from sanad.domain import TenantScope, VersionRef
from sanad.scribe.patients import panel
from sanad.steward.corrections import current_facts
from sanad.steward.types import records
from sanad.store import keys
from sanad.store.records import CommandEnvelope, OutboundIntent

if TYPE_CHECKING:
    from sanad.channels.telegram.router import RouteResult
    from sanad.scribe.turn import ScribeTurn
    from sanad.store.records import Authorization, InboundReceipt

VERBS = {
    "/corrections",
    "/correct",
    "/validate-correction",
    "/reopen",
    "/confirm-reopen",
    "/correction-response",
}


def callback(intent: OutboundIntent) -> str:
    return "c19:" + keys.digest(intent.logical_key)[:40]


def correction_keyboard(
    intent: OutboundIntent, payload: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    from sanad.domain import PatientScope

    if not isinstance(intent.scope, PatientScope) or intent.notification_purpose not in {
        "DONE:FULFILLMENT",
        "DONE:CORRECTION",
        "DEADLINE",
    }:
        return payload
    rendered = deepcopy(payload)
    markup = rendered.setdefault("reply_markup", {})
    if isinstance(markup, dict):
        keyboard = markup.setdefault("inline_keyboard", [])
        if isinstance(keyboard, list):
            keyboard.append([{"text": "Correct record", "callback_data": callback(intent)}])
    return rendered


def route(
    turn: "ScribeTurn", receipt: "InboundReceipt", auth: "Authorization"
) -> "RouteResult | None":
    from sanad.channels.telegram.router import RouteResult

    actor = auth.principal
    if actor.actor_kind != "doctor":
        return None
    text = str((receipt.payload or {}).get("text", ""))
    token_hash = str((receipt.payload or {}).get("callback_token_hash", ""))
    try:
        args = shlex.split(text)
    except ValueError:
        args = text.split()[:1]
    if receipt.kind != "callback" and (not args or args[0] not in VERBS):
        return None
    patients = panel(turn.repo.store, TenantScope(doctor_id=actor.doctor_id or ""))
    matched = None
    if receipt.kind == "callback":
        from sanad.store.records import from_record

        matched = next(
            (
                p
                for p in patients
                for row in records(turn.repo.store, p.scope, "outbound_intent")
                if token_hash == keys.digest(callback(from_record(row, OutboundIntent)))
                and row.body.get("audience") == "doctor"
            ),
            None,
        )
        if matched:
            args = ["/corrections", matched.id]
    if not args or args[0] not in VERBS:
        return None
    claim_result = turn._claim(receipt)
    if not claim_result:
        return RouteResult(route="busy", status="processing")
    receipt, claim = claim_result
    patient = next((p for p in patients if len(args) > 1 and p.id == args[1]), None)
    message = "Use /corrections PATIENT_ID to inspect current correctable versions."
    status = "invalid_input"
    if patient:
        scope = patient.scope
        if args[0] == "/corrections":
            evidence = [
                r
                for r in records(turn.repo.store, scope, "evidence_head")
                if r.body.get("status") in {"accepted", "detached"}
            ]
            message = (
                "Correct the record for "
                + patient.display_name
                + ". A reason is required; originals stay retained.\n"
            )
            lines = []
            for row in evidence:
                version = int(str(row.body["current_version"]))
                lines.append(
                    f'/correct {patient.id} evidence {row.id}:{version} {version} detach "reason"'
                )
                lines.append(
                    f"/correct {patient.id} evidence {row.id}:{version} {version} "
                    '0.value "correct value" "reason"'
                )
            for row in current_facts(turn.repo.store, scope, include_detached=True):
                lines.append(
                    f"/correct {patient.id} clinical_fact {row.id} {row.version} "
                    'text "correct text" "reason"'
                )
            message += "\n".join(lines[:8]) or "No accepted records."
            message += (
                "\nThe record view exposes all rows, validation, order amendments "
                "and confirmed reopening."
            )
            status = "listed"
        else:
            try:
                payload: dict[str, object]
                if args[0] == "/correct":
                    ref = VersionRef(entity_type=args[2], id=args[3], version=int(args[4]))
                    field = args[5]
                    payload = {
                        "type": "CorrectRecord",
                        "predecessor": ref.model_dump(),
                        "operation": "detach" if field == "detach" else "replace",
                        "reason": args[6] if field == "detach" else args[7],
                    }
                    if field != "detach":
                        index, sep, name = field.partition(".")
                        payload["changes"] = {name if sep else field: args[6]}
                        if sep:
                            payload["row_index"] = int(index)
                elif args[0] == "/reopen":
                    payload = {
                        "type": "PreviewReopen",
                        "mission_ref": VersionRef(
                            entity_type="mission", id=args[2], version=int(args[3])
                        ).model_dump(),
                        "due_at": args[4],
                        "reason": args[5],
                    }
                elif args[0] == "/confirm-reopen":
                    payload = {
                        "type": "ConfirmReopen",
                        "offer_ref": VersionRef(
                            entity_type="correction_offer", id=args[2], version=int(args[3])
                        ).model_dump(),
                    }
                elif args[0] == "/validate-correction":
                    payload = {
                        "type": "ValidateCorrection",
                        "correction_id": args[2],
                        "mission_ref": VersionRef(
                            entity_type="mission", id=args[3], version=int(args[4])
                        ).model_dump(),
                        "review_ref": VersionRef(
                            entity_type="review", id=args[5], version=int(args[6])
                        ).model_dump(),
                        "reason": args[7],
                    }
                else:
                    payload = {
                        "type": "CorrectionResponse",
                        "correction_id": args[2],
                        "review_ref": VersionRef(
                            entity_type="review", id=args[3], version=int(args[4])
                        ).model_dump(),
                        "reason": args[5],
                    }
                command = CommandEnvelope.model_validate(
                    {
                        "command_id": "doctor-correction:" + receipt.id,
                        "scope": scope,
                        "principal": actor,
                        "requested_at": turn.repo.clock(),
                        "payload": payload,
                    }
                )
                result = turn.runtime.steward.handle(command)
                status = result.status
                message = (
                    "Correction action recorded. No patient message was sent by this action."
                    if status == "accepted"
                    else "Correction refused: " + (result.reason_code or status)
                )
                for ref in result.resulting_versions:
                    if ref.entity_type == "correction_offer":
                        offer = turn.repo.store.get(scope, ref.entity_type, ref.id)
                        assert offer
                        message = (
                            str(offer.body["preview"])
                            + f"\nConfirm explicitly: /confirm-reopen {patient.id} "
                            f"{ref.id} {ref.version}"
                        )
            except (ValueError, IndexError):
                message = (
                    "Incomplete correction command. Include the exact version, "
                    "new value and quoted reason."
                )
    if receipt.kind == "callback":
        turn.runtime.transport.answer_callback(
            str((receipt.payload or {}).get("callback_query_id", "")), ""
        )
    return turn._reply(receipt, actor, claim, "doctor_correction", status, text=message)
