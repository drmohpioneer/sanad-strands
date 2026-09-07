"""Contract 11 state, recovery, scheduling and integration on both store fixtures."""

from datetime import timedelta

import pytest
from domain_fixtures import NOW
from harness import FakeClock

from sanad.auth.service import revise
from sanad.contact import feedback
from sanad.domain import FollowUpTask, Mission
from sanad.domain.entities import MonitorDetails
from sanad.store._base import StoreBase
from sanad.store.records import (
    BundleSchedule,
    Consent,
    OutboundIntent,
    from_record,
)
from store.contact_fixtures import (
    add_mission,
    current,
    dispatch,
    plan,
    required,
    routine,
    tick,
    world,
)


def at_ten(clock: FakeClock) -> None:
    clock.now = NOW.replace(hour=7)


def test_chase_schedules_accepts_and_replay_counts_once(store: StoreBase, clock: FakeClock) -> None:
    at_ten(clock)
    w = world(store, clock)
    m = add_mission(w)
    plan(w, m)
    intent = routine(w)[0]
    assert intent.template_id == "patient_chase_test"
    assert intent.slot_id == "chase:2026-09-06"
    assert current(w, m).contact_count == 0
    accepted = dispatch(w, intent)
    assert accepted.status == "provider_accepted" and accepted.contact_feedback == "applied"
    assert current(w, m).contact_count == 1
    assert current(w, m).state == "waiting_patient"
    feedback.apply(
        w.runtime.steward,
        intent.model_copy(update={"status": "provider_accepted", "accepted_at": clock()}),
    )
    assert current(w, m).contact_count == 1
    assert dispatch(w, accepted).status == "provider_accepted"
    assert current(w, m).due_at == m.due_at


def test_day3_whole_decision012_chain(store: StoreBase, clock: FakeClock) -> None:
    at_ten(clock)
    w = world(store, clock, medication=True)
    mission = from_record(w.rows("mission")[0], Mission)
    assert mission.state == "waiting_patient"
    task = from_record(w.rows("followup")[0], FollowUpTask)
    assert task.state == "awaiting_anchor"
    w.send("بدأت الدوا")
    task = from_record(w.rows("followup")[0], FollowUpTask)
    assert task.prompt_at == clock() + timedelta(days=3)
    assert current(w, mission).state == "fulfilled"
    original = (
        task.prompt_at,
        task.due_at,
        current(w, mission).due_at,
        current(w, mission).escalation_at,
    )
    assert task.prompt_at
    clock.now = task.prompt_at
    tick(w)
    task = from_record(w.rows("followup")[0], FollowUpTask)
    assert task.state == "waiting_response"
    prompt = next(i for i in routine(w) if i.template_id == "patient_day3_prompt")
    assert prompt.status == "provider_accepted" and prompt.contact_feedback == "applied"
    w.send("تمام")
    tick(w)
    task = from_record(w.rows("followup")[0], FollowUpTask)
    assert task.state == "fulfilled"
    done = [
        r for r in w.rows("outbound_intent") if r.body["notification_purpose"] == "DONE:FULFILLMENT"
    ]
    assert len(done) == 2 and all(r.body["status"] == "provider_accepted" for r in done)
    assert (
        task.prompt_at,
        task.due_at,
        current(w, mission).due_at,
        current(w, mission).escalation_at,
    ) == original


def test_acceptance_crash_recovers_without_send(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    at_ten(clock)
    w = world(store, clock)
    m = add_mission(w)
    plan(w, m)
    intent = routine(w)[0]
    with monkeypatch.context() as patch:
        patch.setattr(feedback, "apply", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
        with pytest.raises(RuntimeError, match="crash"):
            dispatch(w, intent)
    saved = routine(w)[0]
    assert saved.status == "provider_accepted" and saved.contact_feedback == "pending"
    assert saved.work_clock
    calls = len(w.transport.calls)
    assert saved.work_clock
    clock.now = saved.work_clock.next_action_at
    tick(w)
    assert len(w.transport.calls) == calls
    assert current(w, m).contact_count == 1
    assert routine(w)[0].contact_feedback == "applied"
    tick(w)
    assert current(w, m).contact_count == 1


def test_three_chases_unreachable_reply_and_deadline(store: StoreBase, clock: FakeClock) -> None:
    at_ten(clock)
    w = world(store, clock)
    m = add_mission(w)
    for day in (0, 7, 13):
        clock.now = NOW.replace(hour=7) + timedelta(days=day)
        tick(w)
        assert current(w, m).contact_count == (1 if day == 0 else 2 if day == 7 else 3)
    assert current(w, m).state == "unreachable"
    patient = required(w.claims.patient(w.patient_scope.doctor_id, w.patient_scope.patient_id))
    assert patient and patient.contact_status == "unreachable"
    w.send("تمام")
    tick(w)
    assert current(w, m).state == "open"
    assert current(w, m).unanswered_delivered_count == 0
    assert (
        required(
            w.claims.patient(w.patient_scope.doctor_id, w.patient_scope.patient_id)
        ).contact_status
        == "active"
    )
    assert len(routine(w)) == 3
    clock.now = m.escalation_at
    tick(w)
    assert current(w, m).state == "overdue"
    assert sum(r.body["notification_purpose"] == "DEADLINE" for r in w.rows("outbound_intent")) == 1


def test_two_missions_one_daily_chase_then_tomorrow(store: StoreBase, clock: FakeClock) -> None:
    at_ten(clock)
    w = world(store, clock)
    a, b = add_mission(w, "a"), add_mission(w, "b")
    tick(w)
    assert sum(m.contact_count for m in (current(w, a), current(w, b))) == 1
    tick(w)
    assert sum(i.status == "provider_accepted" for i in routine(w)) == 1
    clock.now += timedelta(days=1)
    tick(w)
    assert sum(m.contact_count for m in (current(w, a), current(w, b))) == 2
    assert all(i.slot_id in {"chase:2026-09-06", "chase:2026-09-07"} for i in routine(w))


def test_monitor_quiet_one_consented_one_review(store: StoreBase, clock: FakeClock) -> None:
    at_ten(clock)
    w = world(store, clock)
    slots = (clock(), clock() + timedelta(minutes=30))
    m = add_mission(
        w,
        "monitor",
        kind="MONITOR",
        details=MonitorDetails(metric="الضغط", unit="mmHg", slots=slots, required_coverage=2),
    )
    patient = required(w.claims.patient(w.patient_scope.doctor_id, w.patient_scope.patient_id))
    row = required(store.get(w.patient_scope, "consent", required(patient.consent_id)))
    consent = from_record(row, Consent)
    w.seed(
        consent.model_copy(
            update={
                "quiet_hours": ("09:00", "12:00"),
                "scheduled_slot_consents": ("monitor:monitor:0",),
            }
        )
    )
    tick(w)
    assert len(routine(w)) == 1 and routine(w)[0].status == "provider_accepted"
    clock.now = slots[1]
    tick(w)
    reviews = [r for r in w.rows("review") if r.body["source_type"] == "slot"]
    assert len(reviews) == 1 and reviews[0].body["source_id"] == "monitor:monitor:1"
    tick(w)
    assert len([r for r in w.rows("review") if r.body["source_type"] == "slot"]) == 1
    details = current(w, m).details
    assert isinstance(details, MonitorDetails) and details.slots == slots


def test_first_notice_weekly_bundle_rearmed_and_empty(store: StoreBase, clock: FakeClock) -> None:
    at_ten(clock)
    w = world(store, clock)
    m = add_mission(w, due=clock() + timedelta(hours=4))
    clock.now = m.due_at
    tick(w)
    deadline = next(
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if r.body["notification_purpose"] == "DEADLINE"
    )
    assert deadline.status == "provider_accepted"
    review = store.get_review(w.patient_scope, required(deadline.review_obligation_id))
    assert review and review.first_notice_at == clock()
    tenant = w.doctor.scope
    row = required(store.get(tenant, "bundle_schedule", tenant.doctor_id))
    assert row
    schedule = from_record(row, BundleSchedule)
    assert schedule.next_action_at == clock() + timedelta(days=7)
    # Acknowledged is still unresolved.
    w.seed(
        revise(
            review,
            clock(),
            state="acknowledged",
            acknowledged_by=w.doctor.id,
            acknowledged_at=clock(),
        )
    )
    assert schedule.next_action_at
    clock.now = schedule.next_action_at
    tick(w)
    row = required(store.get(tenant, "bundle_schedule", tenant.doctor_id))
    schedule = from_record(row, BundleSchedule)
    assert schedule.generation == 2 and schedule.last_provider_accepted_at == clock()
    assert schedule.next_action_at == clock() + timedelta(days=7)
    messages = [c for c in w.transport.calls if "بنود لسه" in str(c.payload)]
    assert len(messages) == 1 and "Synthetic Patient" in str(messages[0].payload)
    tick(w)
    assert len([c for c in w.transport.calls if "بنود لسه" in str(c.payload)]) == 1
    assert schedule.next_action_at
    clock.now = schedule.next_action_at
    tick(w)
    assert len([c for c in w.transport.calls if "بنود لسه" in str(c.payload)]) == 2
    review = required(store.get_review(w.patient_scope, review.id))
    w.seed(
        revise(
            review,
            clock(),
            state="resolved",
            resolved_by=w.doctor.id,
            resolved_at=clock(),
            resolved_reason="reviewed",
            resolved_action_event_id="resolved",
            work_clock=None,
        )
    )
    clock.now += timedelta(days=7)
    tick(w)
    schedule = from_record(
        required(store.get(tenant, "bundle_schedule", tenant.doctor_id)), BundleSchedule
    )
    assert schedule.work_clock is None and schedule.next_action_at is None
