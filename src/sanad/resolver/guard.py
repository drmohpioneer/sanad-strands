"""Additive store rules for exact attempt projections and receipt checkpoints."""

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from sanad.concierge.plan import Snapshot
from sanad.concierge.records import ReportFactPayload
from sanad.domain import DRAFT_POLICY_2026_09, Mission, TransitionResult, transition_mission
from sanad.domain.events import BarrierRecorded
from sanad.resolver.attempts import Action, evolve, resolved_words
from sanad.resolver.templates import question_ok
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT
from sanad.scribe.records import ClinicalFact
from sanad.store.records import (
    CommitRequest,
    InboundReceipt,
    ReceiptCompletion,
    StoredRecord,
    from_record,
)

if TYPE_CHECKING:
    from sanad.store._base import Check, StoreBase


def permits(snap: Snapshot, request: CommitRequest, row: StoredRecord, now: datetime) -> bool:
    """No new mission, clinical field, timer, budget or arbitrary history write."""
    try:
        original = next((m for m in snap.missions if m.id == row.id), None)
        if original is None:
            return False
        action = Action.model_validate(request.command.payload.get("resolver_action"))
        if action.mission_id != original.id:
            # Keeping an unresolved attempt blocked on an unrelated patient turn.
            return hold(original, row, now)
        revised = from_record(row, Mission)
        base = original
        if action.phase == "begin" and not original.barrier_attempts:
            from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY
            from sanad.concierge.reports import recognize_barrier

            kind = recognize_barrier(action.words)
            if not kind:
                return False
            report = next(
                (
                    from_record(r, ClinicalFact).payload
                    for r in request.puts
                    if r.entity_type == "clinical_fact"
                    and isinstance(from_record(r, ClinicalFact).payload, ReportFactPayload)
                    and from_record(r, ClinicalFact).payload.text == action.words
                ),
                None,
            )
            if (
                not isinstance(report, ReportFactPayload)
                or report.report_kind != "barrier"
                or report.barrier_type != kind
                or report.target_ref is None
                or report.target_ref.id != original.id
                or report.target_ref.version != original.version
            ):
                return False
            if not original.barrier_reason or original.state not in {"blocked", "overdue"}:
                result = transition_mission(
                    original,
                    BarrierRecorded(
                        event_id=request.command.command_id + ":barrier:" + original.id,
                        barrier_type=kind,
                        reason=action.words,
                        resume_at=now + DRAFT_CONCIERGE_POLICY.barrier_resume_after,
                    ),
                    now,
                    DRAFT_POLICY_2026_09,
                )
                if not isinstance(result, TransitionResult) or not isinstance(
                    result.aggregate, Mission
                ):
                    return False
                base = result.aggregate
        if action.phase == "begin" and original.barrier_attempts and resolved_words(action.words):
            from sanad.domain.events import BarrierResolved

            resolved = transition_mission(
                original,
                BarrierResolved(event_id=request.command.command_id),
                now,
                DRAFT_POLICY_2026_09,
            )
            if not isinstance(resolved, TransitionResult) or not isinstance(
                resolved.aggregate, Mission
            ):
                return False
            base = resolved.aggregate
        if action.phase == "choose" and action.choice == "ask_patient":
            if not original.barrier_attempts or not question_ok(
                action.question,
                original.barrier_attempts[-1].requested_fact or "detail",
                snap.patient.language,
                SAFETY_POLICY_V1_CARDIOLOGY_DRAFT,
            ):
                return False
        attempts = evolve(
            original,
            action,
            str(request.command.payload["receipt_id"]),
            now,
            contact=snap.profile.routine_contact_enabled,
        )
        expected = Mission.model_validate(
            base.model_dump()
            | {"version": original.version + 1, "updated_at": now, "barrier_attempts": attempts}
        )
        return expected == revised
    except (ValidationError, ValueError, KeyError, TypeError):
        return False


def hold(original: Mission, row: StoredRecord, now: datetime) -> bool:
    try:
        return bool(
            original.barrier_attempts
            and original.barrier_reason
            and original.state in {"blocked", "overdue"}
            and from_record(row, Mission)
            == Mission.model_validate(
                original.model_dump() | {"version": original.version + 1, "updated_at": now}
            )
        )
    except (ValueError, TypeError):
        return False


def checkpoint_guards(
    store: "StoreBase", request: CommitRequest, now: datetime
) -> list["Check"] | None:
    """Reuse every existing Concierge authority check, retaining the live receipt."""
    from sanad.store.concierge import guards

    if (
        request.receipt_completion
        or not request.command.work_claim
        or request.intents
        or request.markers
        or request.identity_reads
    ):
        return None
    claim = request.command.work_claim
    old_row = store.get(claim.record_key.scope, "inbound_receipt", claim.record_key.pk)
    rows = [r for r in request.puts if r.entity_type == "inbound_receipt"]
    if not old_row or len(rows) != 1 or not request.command.payload.get("resolver_action"):
        return None
    old, new = from_record(old_row, InboundReceipt), from_record(rows[0], InboundReceipt)
    if (
        not old.updated_at <= new.updated_at == request.command.requested_at <= now
        or new
        != InboundReceipt.model_validate(
            old.model_dump()
            | {"version": old.version + 1, "updated_at": request.command.requested_at}
        )
    ):
        return None
    try:
        action = Action.model_validate(request.command.payload["resolver_action"])
    except (ValidationError, KeyError):
        return None
    if (
        action.phase == "begin"
        and old.kind == "text"
        and action.words != str((old.payload or {}).get("text", ""))
    ):
        return None
    # The outer StoreBase enforces the *real* request's receipt write, claim,
    # lease, versions and epochs. This view runs the unchanged patient rules.
    view = request.model_copy(
        update={
            "command": request.command.model_copy(
                update={"payload": {**request.command.payload, "type": "RecordPatientReply"}}
            ),
            "puts": tuple(r for r in request.puts if r.entity_type != "inbound_receipt"),
            "receipt_completion": ReceiptCompletion(
                claim=claim, result_event_ids=tuple(r.id for r in request.events)
            ),
        }
    )
    return guards(store, view, now)


def suppression(
    store: "StoreBase",
    snap: Snapshot,
    request: CommitRequest,
    row: StoredRecord,
    now: datetime,
    checks: list["Check"],
) -> bool:
    from pydantic import TypeAdapter

    from sanad.domain.events import SuppressRoutineIntents
    from sanad.steward.apply import is_routine
    from sanad.store._base import Check
    from sanad.store.records import OutboundIntent

    old_row = store.get(snap.scope, "outbound_intent", row.id)
    if not old_row:
        return False
    old, new = from_record(old_row, OutboundIntent), from_record(row, OutboundIntent)
    try:
        effects = TypeAdapter(tuple[SuppressRoutineIntents, ...]).validate_python(
            request.command.payload.get("medication_suppressions", [])
        )
    except ValidationError:
        return False
    fields = {"version", "updated_at", "status", "suppression_reason", "work_clock"}
    if (
        old.status != "queued"
        or not is_routine(old)
        or not old.order_refs
        or new.status != "suppressed"
        or new.work_clock
        or new.version != old.version + 1
        or new.updated_at != now
        or old.model_dump(exclude=fields) != new.model_dump(exclude=fields)
    ):
        return False
    for candidate in request.puts:
        if candidate.entity_type != "mission" or not permits(snap, request, candidate, now):
            continue
        mission = from_record(candidate, Mission)
        if mission.barrier_reason != new.suppression_reason:
            continue
        if any(
            e.reason == mission.barrier_reason
            and e.order_refs == mission.order_refs
            and set(old.order_refs).intersection(e.order_refs or ())
            for e in effects
        ):
            checks.append(Check(old_row.key, old_row.version))
            return True
    return False
