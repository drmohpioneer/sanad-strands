from datetime import timedelta
from typing import Any

import pytest
from domain_fixtures import NOW, POLICY, followup, mission, review
from harness import SimulatedCrash, crash_after
from pydantic import JsonValue

from sanad.domain import MissionState, WorkClock
from sanad.steward.sweep import next_work
from sanad.steward.types import records
from sanad.store._base import Check, Write
from sanad.store.records import (
    AuditEvent,
    CommandEnvelope,
    DoctorAuthority,
    InboundReceipt,
    PatientProfile,
    from_record,
    record_item,
    to_record,
)
from store.fixtures import OTHER, SCOPE
from store.processing_fixtures import PATIENT, World
from store.test_processing import process, report
from store.test_processing import world as world


@pytest.mark.parametrize("crash", [False, True])
def test_past_deadline_commits_before_reply_and_receipt_recovery(world: World, crash: bool) -> None:
    world.confirm()
    world.clock.advance(world.mission().escalation_at - world.clock() + timedelta(hours=1))
    command = world.command("RecordPatientReply", principal=PATIENT, id="late-reply")
    accepted = world.accept({"kind": "command", "command": command.model_dump(mode="json")})
    if crash:
        with pytest.raises(SimulatedCrash), crash_after(world.store, "commit"):
            process(world, accepted)
        assert world.mission().state == "overdue"
        assert world.receipt(accepted.record.id).state == "processing"
        world.clock.advance(timedelta(minutes=11))
        world.run_sweep("ingress")
    else:
        assert process(world, accepted).status == "accepted"
    events = world.store.list_events(SCOPE)[0]
    deadline = [r for r in events if r.body["event_type"] == "DEADLINE_REACHED"]
    reply = [r for r in events if r.body["event_type"] == "PATIENT_REPLIED"]
    assert len(deadline) == len(reply) == 1
    assert (
        from_record(deadline[0], AuditEvent).after_versions[0].version
        < from_record(reply[0], AuditEvent).after_versions[0].version
    )
    assert world.mission().version == 4 and world.mission().state == "overdue"
    assert world.receipt(accepted.record.id).state == "completed"


def test_stale_patient_fence_and_receipt_generation_cannot_write(world: World) -> None:
    world.confirm()
    old = world.store.acquire_patient(SCOPE, "old", world.clock(), timedelta(seconds=1))
    assert old is not None
    command = CommandEnvelope.model_validate(
        world.command("RecordPatientReply", principal=PATIENT).model_dump() | {"fence": old}
    )
    world.clock.advance(timedelta(seconds=1))
    fresh = world.store.acquire_patient(SCOPE, "fresh", world.clock(), timedelta(seconds=30))
    assert fresh is not None
    assert world.steward.handle(command).status == "stale_version"
    assert world.profile.lease_owner == "fresh"
    world.store.release_patient(fresh)
    accepted = report(world)
    key = accepted.record.scoped_key(SCOPE)
    first = world.store.claim_work(key, 1, "first", world.clock(), timedelta(seconds=1))
    assert first is not None
    assert (
        world.store.claim_work(key, first.version, "second", world.clock(), timedelta(seconds=1))
        is None
    )
    original = world.inbound._normalize(world.receipt(key.pk), first)
    world.clock.advance(timedelta(seconds=1))
    second = world.store.claim_work(
        key, first.version, "second", world.clock(), timedelta(minutes=1)
    )
    assert second is not None and second.generation == 2
    assert world.steward.handle(original).status == "stale_version"
    assert world.mission().state == "open"
    revised = CommandEnvelope.model_validate(original.model_dump() | {"work_claim": second})
    assert world.steward.handle(revised).status == "accepted"
    assert world.mission().state == "fulfilled"


@pytest.mark.parametrize(
    "kind,expected_kind", [("unsupported", "intake_clarification"), ("photo", "media_failure")]
)
def test_exhausted_receipt_hands_off_to_one_timed_review(
    world: World, kind: str, expected_kind: str
) -> None:
    accepted = world.accept({"kind": kind})
    for count in range(1, world.policy.operations.max_inbound_attempts + 1):
        with pytest.raises(ValueError, match="target|required|unsupported"):
            process(world, accepted)
        row = world.store.get(SCOPE, "inbound_receipt", accepted.record.id)
        assert row is not None
        current = from_record(row, InboundReceipt)
        if count < world.policy.operations.max_inbound_attempts:
            assert current.state == "processing" and current.work_clock is not None
            assert current.work_clock.attempt_count == count
            assert (
                current.work_clock.next_action_at
                == world.clock() + world.policy.operations.retry_backoff(count)
            )
            assert process(world, accepted) is None
            world.clock.advance(current.work_clock.next_action_at - world.clock())
        else:
            assert current.state == "needs_attention" and current.work_clock is not None
            assert current.work_clock.next_action_at > world.clock()
            review = world.store.get_review(SCOPE, current.review_obligation_id or "")
            assert review is not None and review.review_kind == expected_kind
            assert (
                review.work_clock is not None and review.work_clock.next_action_at > world.clock()
            )
    assert len(list(records(world.store, SCOPE, "review"))) == 1
    assert process(world, accepted) is None
    current = world.receipt(accepted.record.id)
    assert current.work_clock is not None
    world.clock.advance(current.work_clock.next_action_at - world.clock())
    assert world.run_sweep("ingress").handled == 1
    rearmed = world.receipt(accepted.record.id)
    assert rearmed.state == "needs_attention" and rearmed.work_clock is not None
    assert rearmed.work_clock.attempt_count == 5
    assert rearmed.work_clock.next_action_at > world.clock()
    assert len(list(records(world.store, SCOPE, "review"))) == 1


def test_ack_is_impossible_when_durable_accept_fails(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    acknowledgments = []

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic store outage")

    monkeypatch.setattr(world.store, "accept_inbound", unavailable)
    with pytest.raises(RuntimeError):
        saved = world.accept(
            {"kind": "text", "mission_id": "synthetic-mission", "text": "synthetic"}
        )
        acknowledgments.append(saved)
    assert acknowledgments == []


def test_new_receipt_for_same_command_completes_without_second_mutation(world: World) -> None:
    world.confirm()
    command = world.command("RecordPatientReply", principal=PATIENT)
    payload: dict[str, JsonValue] = {"kind": "command", "command": command.model_dump(mode="json")}
    first, second = world.accept(payload, "one"), world.accept(payload, "two")
    assert process(world, first).status == "accepted"
    assert process(world, second).status == "accepted"
    assert world.mission().version == 3
    assert world.receipt(second.record.id).state == "completed"


def test_sweep_budget_stale_index_terminal_hint_and_leftovers(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(4):
        world.put(
            mission(
                id=f"synthetic-{i}", work_clock=WorkClock(next_action_at=NOW, work_lane="mission")
            )
        )
    first = world.run_sweep("mission", max_items=2, page_size=1)
    assert first.examined == first.handled == 2 and first.budget_exhausted
    due = [r for r in records(world.store, SCOPE, "mission") if next_work(r) == NOW]
    assert len(due) == 2
    assert world.run_sweep("mission").handled == 2
    stale, _ = world.store._query("mission#0", index="GSI_DUE")
    for item in stale:
        item["due_sort"] = "2026-09-06T12:00:00.000000Z#mission#" + item["id"]
    world.put(mission(MissionState.cancelled, id="synthetic-0", version=3, updated_at=NOW))
    original_query = world.store._query

    def lagged(*args: Any, **kwargs: Any) -> Any:
        return (
            (stale, None) if kwargs.get("index") == "GSI_DUE" else original_query(*args, **kwargs)
        )

    monkeypatch.setattr(world.store, "_query", lagged)
    result = world.run_sweep("mission")
    assert result.handled == 0 and result.skipped == 4
    assert not world.transport.calls


def test_seconds_budget_and_zero_budget_leave_due_work(world: World) -> None:
    world.put(mission(work_clock=WorkClock(next_action_at=NOW, work_lane="mission")))
    assert world.run_sweep("mission", max_items=0).examined == 0
    assert world.run_sweep("mission", max_seconds=0).examined == 0
    times = iter((0.0, 0.0, 1.0))
    world.sweep.elapsed_clock = lambda: next(times)
    result = world.run_sweep("mission", max_seconds=0.5)
    assert result.budget_exhausted and result.examined == 0
    assert world.mission().work_clock.next_action_at == NOW  # type: ignore[union-attr]


def test_reviews_and_followups_rearm_without_fabricating_events(world: World) -> None:
    world.put(review(review_at=NOW, work_clock=WorkClock(next_action_at=NOW, work_lane="review")))
    world.put(followup(work_clock=WorkClock(next_action_at=NOW, work_lane="followup")))
    assert world.run_sweep("review").handled == 1
    assert world.run_sweep("followup").handled == 1
    assert world.run_sweep("review").handled == 0
    saved = list(records(world.store, SCOPE, "review"))[0]
    assert from_record(saved, type(review())).review_at == NOW
    assert next_work(saved) == NOW + POLICY.overdue_review_interval
    kinds = {r.body["event_type"] for r in world.store.list_events(SCOPE)[0]}
    assert kinds == {"ACCOUNTABILITY_WAKE"}
    assert not world.queued()


def test_reconcile_reports_repair_without_changing_truth(world: World) -> None:
    world.confirm()
    row = to_record(world.mission(), SCOPE)
    damaged = record_item(row)
    damaged.pop("due_sort")
    assert world.store._update(damaged, row.version)
    report = world.sweep.reconcile(SCOPE, None, 100)
    assert report.repaired == (row.key,)
    assert world.store.get(SCOPE, "mission", row.id) == row
    assert world.sweep.reconcile(OTHER).examined == 0


def test_urgent_epoch_bump_fences_inflight_ordinary_commit(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.confirm()
    original = world.store._atomic
    injected = False

    def danger_before_commit(writes: list[Write], checks: list[Check]) -> bool:
        nonlocal injected
        if not injected and any(w.item.get("entity_type") == "mission" for w in writes):
            injected = True
            world.urgent.raise_incident(
                SCOPE, "synthetic-concurrent-danger", {}, "urgent", world.clock()
            )
        return original(writes, checks)

    monkeypatch.setattr(world.store, "_atomic", danger_before_commit)
    assert (
        world.steward.handle(world.command("RecordPatientReply", principal=PATIENT)).status
        == "stale_version"
    )
    assert world.mission().version == 2 and world.profile.safety_epoch == 1
    assert len(world.queued("DANGER")) == 1


def test_authority_changes_between_initial_read_and_lease_are_rejected(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.confirm()
    acquire = world.store.acquire_patient

    def suspend(*args: Any, **kwargs: Any) -> Any:
        world.put(
            DoctorAuthority.model_validate(
                world.doctor.model_dump() | {"version": 2, "approved": False, "auth_epoch": 1}
            )
        )
        return acquire(*args, **kwargs)

    monkeypatch.setattr(world.store, "acquire_patient", suspend)
    assert (
        world.steward.handle(world.command("CancelMission", reason="synthetic")).status
        == "forbidden"
    )
    assert world.mission().state == "open"


@pytest.mark.parametrize("purpose", ["routine_prompt", "DANGER"])
def test_dispatch_checks_freshness_after_attempt_start(
    world: World, monkeypatch: pytest.MonkeyPatch, purpose: str
) -> None:
    world.confirm()
    if purpose == "DANGER":
        world.urgent.raise_incident(SCOPE, "synthetic", {}, "urgent", world.clock())
        intent = world.queued("DANGER")[0]
    else:
        intent = world.prompt()
    start = world.store.start_delivery

    def change(*args: Any, **kwargs: Any) -> Any:
        attempt = start(*args, **kwargs)
        world.put(
            PatientProfile.model_validate(
                world.profile.model_dump()
                | {"safety_epoch": 42, "delivery_epoch": 42, "consent_active": False}
            )
        )
        return attempt

    monkeypatch.setattr(world.store, "start_delivery", change)
    final = world.dispatch(intent)
    assert final.status == ("provider_accepted" if purpose == "DANGER" else "suppressed")
    assert len(world.transport.calls) == (1 if purpose == "DANGER" else 0)


def test_delayed_provider_response_is_uncertain_and_review_owned(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.confirm()
    intent = world.prompt()
    send = world.transport.send

    def late(*args: Any, **kwargs: Any) -> Any:
        outcome = send(*args, **kwargs)
        world.clock.advance(timedelta(minutes=6))
        return outcome

    monkeypatch.setattr(world.transport, "send", late)
    result = world.dispatch(intent)
    assert result.status == "uncertain" and result.accepted_message_id is None
    result = world.dispatch(result)
    assert result.review_obligation_id is not None and result.work_clock is None
    assert len(world.transport.calls) == 1


def test_crash_after_provider_completion_does_not_resend(world: World) -> None:
    world.confirm()
    prompt = world.prompt()
    with pytest.raises(SimulatedCrash), crash_after(world.store, "complete_delivery"):
        world.dispatch(prompt)
    world.clock.advance(timedelta(minutes=6))
    world.run_sweep("delivery")
    assert len(world.transport.calls) == 1
    assert world.dispatch(prompt).status == "provider_accepted"


def test_last_claim_crash_exhausts_without_a_sixth_processing_attempt(world: World) -> None:
    world.confirm()
    accepted = report(world)
    for _ in range(world.policy.operations.max_inbound_attempts):
        with pytest.raises(SimulatedCrash), crash_after(world.store, "claim_work"):
            process(world, accepted)
        world.clock.advance(world.policy.operations.claim_ttl)
    row = world.receipt(accepted.record.id)
    assert row.work_clock is not None and row.work_clock.attempt_count == 5
    assert world.run_sweep("ingress").handled == 1
    assert world.receipt(accepted.record.id).state == "needs_attention"
    assert world.mission().state == "open"


def test_claimed_mission_handles_deadline_then_reply_under_one_fence(world: World) -> None:
    world.confirm()
    world.clock.advance(world.mission().due_at - world.clock())
    row = to_record(world.mission(), SCOPE)
    claim = world.store.claim_work(
        row.scoped_key(SCOPE),
        row.version,
        "synthetic-claimed-mission",
        world.clock(),
        timedelta(minutes=5),
    )
    assert claim is not None
    command = CommandEnvelope.model_validate(
        world.command("RecordPatientReply", principal=PATIENT).model_dump() | {"work_claim": claim}
    )
    assert world.steward.handle(command).status == "accepted"
    assert world.mission().state == "overdue" and world.mission().version == 5


def test_danger_ignores_stale_source_order_expiry_and_routine_budget(world: World) -> None:
    from sanad.domain import VersionRef
    from sanad.steward.apply import make_intent

    intent = make_intent(
        SCOPE,
        "synthetic-danger",
        (VersionRef(entity_type="mission", id="missing", version=99),),
        "DANGER",
        "synthetic-facts",
        NOW,
        world.policy,
        world.doctor,
        world.profile,
    )
    world.put(intent)
    world.clock.advance(timedelta(days=30))
    lease = world.store.acquire_patient(SCOPE, "slow-patient", world.clock(), timedelta(hours=1))
    assert lease is not None
    assert world.dispatch(intent).status == "provider_accepted"
    world.store.release_patient(lease)
    assert len(world.transport.calls) == 1


@pytest.mark.parametrize("action", ["CancelMission", "CloseUnfulfilledMission"])
def test_doctor_disposition_and_explicit_reopen_use_current_consent(
    world: World, action: str
) -> None:
    world.confirm()
    assert (
        world.steward.handle(world.command(action, reason="Synthetic disposition")).status
        == "accepted"
    )
    assert world.mission().work_clock is None
    world.put(PatientProfile.model_validate(world.profile.model_dump() | {"consent_active": False}))
    command = world.command(
        "ReopenMission",
        timing={
            "instant": (world.clock() + timedelta(days=1)).isoformat(),
            "original_expression": "Synthetic new day",
            "timezone": "Africa/Cairo",
        },
        new_objective_predicate=world.mission().objective_predicate.model_dump(mode="json"),
        new_order_refs=[r.model_dump(mode="json") for r in world.mission().order_refs],
        reason="Synthetic reopen",
    )
    assert world.steward.handle(command).status == "accepted"
    assert world.mission().state == "awaiting_link"


def test_review_commands_keep_acknowledgment_separate_from_resolution(world: World) -> None:
    world.confirm()
    assert process(world, report(world)).status == "accepted"
    obligation = list(records(world.store, SCOPE, "review"))[0]
    acknowledged = world.command("AcknowledgeReview", target="review_id", target_id=obligation.id)
    first = world.steward.handle(acknowledged)
    assert first.status == "accepted" and world.steward.handle(acknowledged) == first
    current = world.store.get_review(SCOPE, obligation.id)
    assert (
        current is not None and current.state == "acknowledged" and current.work_clock is not None
    )
    wrong = world.command(
        "ResolveReview",
        target="review_id",
        target_id=current.id,
        action="cancel",
        expected_source_version=current.source_version,
        reason="synthetic",
    )
    assert world.steward.handle(wrong).status == "needs_confirmation"
    right = world.command(
        "ResolveReview",
        target="review_id",
        target_id=current.id,
        action="review",
        expected_source_version=current.source_version,
        reason="Synthetic reviewed",
    )
    assert world.steward.handle(right).status == "accepted"
    saved = world.store.get_review(SCOPE, current.id)
    assert saved is not None and saved.state == "resolved" and saved.work_clock is None


def test_open_incident_prevents_close_without_mission_projection(world: World) -> None:
    world.confirm()
    world.urgent.raise_incident(SCOPE, "synthetic-danger", {}, "urgent", world.clock())
    assert not world.mission().danger_history
    assert (
        world.steward.handle(world.command("CloseUnfulfilledMission", reason="synthetic")).status
        == "needs_confirmation"
    )
    assert world.mission().state == "open"


def test_unknown_cross_scope_and_forged_payloads_do_not_mutate(world: World) -> None:
    world.confirm()
    command = world.command("RecordPatientReply", principal=PATIENT, id="duplicate-payload")
    result = world.steward.handle(command)
    assert result.status == "accepted"
    duplicate = CommandEnvelope.model_validate(
        command.model_dump() | {"payload": command.payload | {"unexpected": True}}
    )
    assert world.steward.handle(duplicate).status == "stale_version"
    foreign = CommandEnvelope.model_validate(command.model_dump() | {"scope": OTHER})
    assert world.steward.handle(foreign).status == "forbidden"
    assert world.steward.handle(world.command("NotReleased")).status == "unsupported"
    assert (
        world.steward.handle(world.command("ExtendMission", consent_active=True)).status
        == "invalid_input"
    )
    assert (
        world.steward.handle(
            world.command("SetContactPreference", consent_active=True, reason="synthetic")
        ).status
        == "forbidden"
    )


def test_store_failure_is_not_misreported_as_bad_input(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.confirm()

    def bad_backend(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("synthetic corrupt storage")

    monkeypatch.setattr(world.store, "commit", bad_backend)
    with pytest.raises(ValueError, match="corrupt storage"):
        world.steward.handle(world.command("RecordPatientReply", principal=PATIENT))


def test_unclassified_failed_store_attempt_is_transferred_to_review(world: World) -> None:
    world.confirm()
    intent = world.prompt()
    row = to_record(intent, SCOPE)
    attempt = world.store.start_delivery(
        intent.id, (row.ref, *intent.source_versions), "synthetic", NOW, scope=SCOPE
    )
    assert attempt is not None
    failed = world.store.complete_delivery(attempt.id, "definite_failure", None, scope=SCOPE)
    assert failed is not None and failed.due_sort is not None
    world.clock.advance(world.policy.operations.lease_ttl)
    final = world.dispatch(intent)
    assert (
        final.status == "failed"
        and final.work_clock is None
        and final.review_obligation_id is not None
    )
    assert not world.transport.calls
