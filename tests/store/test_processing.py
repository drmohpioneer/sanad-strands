from datetime import timedelta
from typing import Any

import pytest
from domain_fixtures import CORRECTION, EVIDENCE, ORDER, predicate_result
from harness import SimulatedCrash, crash_after

from sanad.channels.transport import ProvablyUnsent, SendOutcome
from sanad.domain import MedicationDetails, PatientReportPredicate, ReviewObligation
from sanad.steward.types import records
from sanad.store._base import StoreBase
from sanad.store.records import (
    CommandEnvelope,
    DoctorAuthority,
    PatientProfile,
    from_record,
)
from store.conftest import Clock
from store.fixtures import SCOPE
from store.processing_fixtures import PATIENT, World


@pytest.fixture
def world(store: StoreBase, clock: Clock) -> World:
    return World.create(store, clock)


def report(world: World, key: str = "report", *, followup_id: str | None = None) -> Any:
    return world.accept(
        {
            "kind": "report",
            "followup_id" if followup_id else "mission_id": followup_id or "synthetic-mission",
            "predicate_result": predicate_result(at=world.clock()).model_dump(mode="json"),
            **(
                {"source_report_ids": [key]}
                if followup_id
                else {"evidence_refs": [EVIDENCE.model_dump(mode="json")]}
            ),
        },
        key,
    )


def process(world: World, accepted: Any) -> Any:
    return world.inbound.process_inbound(
        accepted.record.scoped_key(SCOPE), "synthetic-worker", world.clock()
    )


def test_confirm_contact_reply_fulfillment_and_correction(world: World) -> None:
    world.confirm()
    assert world.dispatch(world.prompt()).status == "provider_accepted"
    accepted = world.accept(
        {"kind": "text", "text": "Synthetic reply", "mission_id": "synthetic-mission"}
    )
    assert process(world, accepted).status == "accepted"
    assert world.mission().state == "open"
    assert process(world, report(world)).status == "accepted"
    done = world.queued("DONE:FULFILLMENT")[0]
    assert world.dispatch(done).status == "provider_accepted"
    assert world.mission().state == "fulfilled"
    assert world.mission().fulfillment_validity == "valid"
    assert len(world.transport.calls) == 2
    changed = world.steward.handle(
        world.command(
            "CorrectEvidence",
            superseded_evidence_ref=EVIDENCE.model_dump(mode="json"),
            correcting_evidence_ref=CORRECTION.model_dump(mode="json"),
            predicate_still_holds=False,
        )
    )
    assert changed.status == "accepted"
    assert world.mission().fulfillment_validity == "invalidated_pending_review"
    assert world.mission().state == "fulfilled"
    assert len(list(records(world.store, SCOPE, "evidence_annotation"))) == 1
    reviews = [from_record(r, ReviewObligation) for r in records(world.store, SCOPE, "review")]
    assert {r.review_kind for r in reviews} == {"result_review", "correction_disposition"}


def test_queued_done_is_suppressed_after_correction(world: World) -> None:
    world.confirm()
    assert process(world, report(world)).status == "accepted"
    done = world.queued()[0]
    assert (
        world.steward.handle(
            world.command(
                "CorrectEvidence",
                superseded_evidence_ref=EVIDENCE.model_dump(mode="json"),
                correcting_evidence_ref=CORRECTION.model_dump(mode="json"),
                predicate_still_holds=False,
            )
        ).status
        == "accepted"
    )
    assert world.dispatch(done).status == "suppressed"
    assert not world.transport.calls


def test_deadlines_extend_rearms_once_and_preserves_current_consent(world: World) -> None:
    world.confirm()
    original = world.mission()
    world.clock.advance(original.escalation_at - world.clock())
    assert world.run_sweep("mission").handled == 1
    assert len(world.queued("DEADLINE")) == 1
    stale_deadline = world.queued()[0]
    assert world.run_sweep("mission").handled == 0
    command = world.command(
        "ExtendMission",
        timing={
            "instant": (world.clock() + timedelta(days=2)).isoformat(),
            "original_expression": "synthetic two days",
            "timezone": "Africa/Cairo",
        },
        reason="Synthetic extension",
    )
    assert world.steward.handle(command).status == "accepted"
    assert world.mission().state == "open" and world.mission().deadline_generation == 2
    assert world.dispatch(stale_deadline).status == "suppressed"
    assert world.run_sweep("mission").handled == 0
    world.clock.advance(timedelta(days=2))
    assert world.run_sweep("mission").handled == 1
    assert len(world.queued("DEADLINE")) == 1
    assert world.dispatch(world.queued()[0]).status == "provider_accepted"
    assert len(world.transport.calls) == 1


def test_start_and_independent_day_three_report_send_two_done(world: World) -> None:
    world.confirm(
        kind="MEDICATION",
        details=MedicationDetails(action="START", order_ref=ORDER),
        objective_predicate=PatientReportPredicate(report_kind="started"),
    )
    tasks = list(records(world.store, SCOPE, "followup"))
    assert len(tasks) == 1 and tasks[0].body["state"] == "awaiting_anchor"
    world.clock.advance(timedelta(days=1))
    assert process(world, report(world, "start")).status == "accepted"
    assert world.dispatch(world.queued()[0]).status == "provider_accepted"
    task = world.store.get_followup(SCOPE, tasks[0].id)
    assert task is not None and task.prompt_at == world.clock() + timedelta(days=3)
    world.clock.advance(timedelta(days=3))
    assert world.run_sweep("followup").handled == 1
    task = world.store.get_followup(SCOPE, task.id)
    assert task is not None and task.state == "scheduled"  # No synthetic PromptAccepted.
    assert process(world, report(world, "day-three", followup_id=task.id)).status == "accepted"
    assert world.dispatch(world.queued()[0]).status == "provider_accepted"
    assert [c.payload["purpose"] for c in world.transport.calls] == [
        "DONE:FULFILLMENT",
        "DONE:FULFILLMENT",
    ]


def test_patient_stop_suppresses_routine_but_danger_bypasses_lease(world: World) -> None:
    world.confirm()
    routine = world.prompt()
    lease = world.store.acquire_patient(SCOPE, "slow-worker", world.clock(), timedelta(hours=1))
    assert lease is not None
    first = world.urgent.raise_incident(
        SCOPE, "synthetic-danger-source", {"synthetic": True}, "urgent", world.clock()
    )
    duplicate = world.urgent.raise_incident(
        SCOPE, "synthetic-danger-source", {"different": True}, "other", world.clock()
    )
    assert duplicate == first and world.profile.safety_epoch == 1
    danger = world.queued("DANGER")[0]
    assert world.dispatch(danger).status == "provider_accepted"
    world.store.release_patient(lease)
    assert (
        world.steward.handle(
            world.command(
                "SetContactPreference",
                principal=PATIENT,
                consent_active=False,
                reason="Synthetic stop",
            )
        ).status
        == "accepted"
    )
    assert world.dispatch(routine).status == "suppressed"
    safety = world.queued("patient_safety_response")[0]
    assert world.dispatch(safety).status == "provider_accepted"
    world.urgent.raise_incident(SCOPE, "synthetic-second-danger", {}, "urgent", world.clock())
    assert world.dispatch(world.queued("DANGER")[0]).status == "provider_accepted"
    assert [c.payload["purpose"] for c in world.transport.calls] == [
        "DANGER",
        "patient_safety_response",
        "DANGER",
    ]


@pytest.mark.parametrize("boundary", ["accept_inbound", "claim_work", "commit"])
def test_crash_boundaries_resume_exactly_one_mutation(world: World, boundary: str) -> None:
    world.confirm()
    if boundary == "accept_inbound":
        with pytest.raises(SimulatedCrash), crash_after(world.store, boundary):
            report(world)
        accepted = report(world)
        assert accepted.status == "existing" and accepted.state == "pending"
    else:
        accepted = report(world)
        with pytest.raises(SimulatedCrash), crash_after(world.store, boundary):
            process(world, accepted)
    world.clock.advance(timedelta(minutes=11))
    world.run_sweep("ingress")
    assert world.mission().state == "fulfilled" and world.mission().version == 3
    assert world.receipt(accepted.record.id).state == "completed"
    assert len(world.queued("DONE:FULFILLMENT")) == 1
    world.run_sweep("delivery")
    world.run_sweep("ingress")
    world.run_sweep("delivery")
    assert len(world.transport.calls) == 1


@pytest.mark.parametrize("danger", [False, True])
@pytest.mark.parametrize("boundary", ["start_delivery", "timeout"])
def test_interrupted_delivery_is_uncertain_with_bounded_danger_resend(
    world: World, danger: bool, boundary: str
) -> None:
    world.confirm()
    if danger:
        world.urgent.raise_incident(SCOPE, "synthetic-source", {}, "urgent", world.clock())
        intent = world.queued("DANGER")[0]
    else:
        intent = world.prompt()
    if boundary == "start_delivery":
        with pytest.raises(SimulatedCrash), crash_after(world.store, "start_delivery"):
            world.dispatch(intent)
        assert not world.transport.calls
        world.clock.advance(timedelta(minutes=6))
    else:
        world.transport.script.append(TimeoutError())
    result = world.dispatch(intent)
    assert result.uncertain_retry_count == 1
    assert result.status == ("queued" if danger else "uncertain")
    if danger:
        world.transport.script.append(TimeoutError())
        world.clock.advance(timedelta(minutes=1))
        final = world.dispatch(result)
        assert final.status == "uncertain" and final.uncertain_retry_count == 2
        assert final.review_obligation_id is not None and final.work_clock is None
        assert world.transport.calls[-1].payload["same_incident_resend"] is True
    else:
        assert result.review_obligation_id is not None and result.work_clock is None
        before = len(world.transport.calls)
        world.clock.advance(timedelta(days=3))
        world.dispatch(result)
        assert len(world.transport.calls) == before


@pytest.mark.parametrize("retryable", [False, True])
def test_delivery_failure_exhaustion_has_atomic_review(world: World, retryable: bool) -> None:
    world.confirm()
    intent = world.prompt()
    attempts = world.policy.operations.max_delivery_attempts if retryable else 1
    for i in range(attempts):
        world.transport.script.append(
            SendOutcome(status="failed", retryable=retryable, code="synthetic_blocked")
        )
        intent = world.dispatch(intent)
        assert intent.retry_count == i + 1
        if i < attempts - 1:
            assert intent.status == "queued" and intent.work_clock is not None
            world.clock.advance(intent.work_clock.next_action_at - world.clock())
    assert len(world.transport.calls) == attempts
    assert intent.status == "failed" and intent.work_clock is None
    assert world.store.get_review(SCOPE, intent.review_obligation_id or "") is not None


def test_contact_collision_and_only_proven_unsent_refunds(world: World) -> None:
    world.confirm()
    first, second = world.prompt(), world.prompt()
    world.transport.script.append(ProvablyUnsent())
    assert world.dispatch(first).status == "queued"
    assert world.dispatch(second).status == "provider_accepted"
    assert len(world.transport.possibly_sent) == 1
    world.clock.advance(timedelta(minutes=1))
    # Accepted feedback advances the mission before this retry reaches the consumed budget.
    assert world.dispatch(first).suppression_reason == "stale_source_or_expired"
    assert len(world.transport.possibly_sent) == 1
    uncertain, blocked = world.prompt(slot="day-two"), world.prompt(slot="day-two")
    world.transport.script.append(TimeoutError())
    assert world.dispatch(uncertain).status == "uncertain"
    assert world.dispatch(blocked).suppression_reason == "budget"


def test_duplicate_transport_and_command_payload_identity(world: World) -> None:
    world.confirm()
    accepted = world.accept({"kind": "text", "mission_id": "synthetic-mission", "text": "first"})
    duplicate = world.accept(
        {"kind": "text", "mission_id": "synthetic-mission", "text": "different"}
    )
    assert accepted.record == duplicate.record
    assert process(world, accepted).status == "accepted"
    assert process(world, duplicate) is None
    command = world.command("RecordPatientReply", principal=PATIENT, id="synthetic-command")
    result = world.steward.handle(command)
    assert result.status == "accepted"
    assert world.steward.handle(command) == result
    modified = CommandEnvelope.model_validate(
        command.model_dump()
        | {
            "payload": {
                "type": "CancelMission",
                "mission_id": "synthetic-mission",
                "reason": "synthetic",
            }
        }
    )
    # Using the same authorized caller with changed content must not mutate.
    assert world.steward.handle(modified).status in {"forbidden", "stale_version"}
    assert world.mission().state == "open"


def test_missed_ticks_are_bounded_and_do_not_burst(world: World) -> None:
    world.confirm()
    prompt = world.prompt()
    world.clock.advance(world.mission().due_at - world.clock() + timedelta(days=3))
    assert world.run_sweep("mission").handled == 1
    assert world.run_sweep("mission").handled == 0
    assert world.dispatch(prompt).status == "suppressed"
    assert len(world.queued("DEADLINE")) == 1
    world.run_sweep("delivery")
    world.run_sweep("delivery")
    assert len(world.transport.calls) == 1
    assert world.mission().work_clock.next_action_at > world.clock()  # type: ignore[union-attr]


@pytest.mark.parametrize("changed", ["safety", "consent", "binding", "delivery", "doctor", "order"])
def test_current_truth_invalidates_queued_routine(world: World, changed: str) -> None:
    world.confirm()
    intent = world.prompt()
    if changed == "doctor":
        world.put(
            DoctorAuthority.model_validate(
                world.doctor.model_dump() | {"version": 2, "approved": False, "auth_epoch": 1}
            )
        )
    elif changed == "order":
        from sanad.store.records import OrderAuthority

        row = world.store.get(SCOPE, "care_order", ORDER.id)
        assert row is not None
        world.put(OrderAuthority.model_validate(row.body | {"version": 2, "status": "superseded"}))
    else:
        changes: dict[str, dict[str, object]] = {
            "safety": {"safety_epoch": 1},
            "consent": {"consent_active": False},
            "binding": {"binding_active": False},
            "delivery": {"delivery_epoch": 1},
        }
        world.put(PatientProfile.model_validate(world.profile.model_dump() | changes[changed]))
    assert world.dispatch(intent).status == "suppressed"
    assert not world.transport.calls
