from datetime import datetime, timedelta
from typing import Any

import pytest
from domain_fixtures import NOW
from harness import FakeClock

from sanad.auth.service import revise
from sanad.channels.transport import SendOutcome
from sanad.contact import bundle
from sanad.domain import Mission, PatientScope, ReviewObligation
from sanad.domain.entities import MonitorDetails
from sanad.steward.dispatch import Dispatcher, freshness
from sanad.steward.service import Steward
from sanad.steward.types import records
from sanad.store._base import StoreBase
from sanad.store.records import (
    BundleSchedule,
    OutboundIntent,
    StoredRecord,
    from_record,
)
from store.concierge_fixtures import PatientWorld
from store.contact_fixtures import add_mission, dispatch, plan, required, routine, tick, world


def reviews(w: PatientWorld) -> list[ReviewObligation]:
    return [from_record(r, ReviewObligation) for r in w.rows("review")]


def deadline(w: PatientWorld, clock: FakeClock, name: str = "deadline") -> ReviewObligation:
    m = add_mission(w, name, due=clock() + timedelta(hours=4))
    clock.now = m.due_at
    tick(w)
    return next(r for r in reviews(w) if r.source_id == name and r.review_kind == "unmet_objective")


def bundle_intent(w: PatientWorld) -> OutboundIntent:
    values = list(records(w.store, w.doctor.scope, "outbound_intent"))
    return next(
        from_record(r, OutboundIntent)
        for r in values
        if r.body.get("eligibility_class") == "bundle"
    )


def test_outage_12_due_items_four_patients_no_backlog_burst(
    store: StoreBase, clock: FakeClock
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    start = clock()
    scopes = [w.patient_scope]
    for i in range(1, 4):
        patient = w.stub()
        pending = w.consent(
            w.claim(w.invite(patient), str(30003 + i), id=500 + i * 2), id=501 + i * 2
        )
        assert w.claims.confirm(w.confirm_command(pending)).status == "accepted"
        scopes.append(patient.scope)
    snapshots: list[tuple[PatientScope, str, datetime, datetime, str]] = []
    for i, scope in enumerate(scopes):
        w.patient_scope = scope
        a = add_mission(w, f"chase-a-{i}")
        b = add_mission(w, f"chase-b-{i}")
        m = add_mission(
            w,
            f"monitor-{i}",
            kind="MONITOR",
            details=MonitorDetails(
                metric="الضغط", unit="mmHg", slots=(start,), required_coverage=1
            ),
        )
        snapshots.extend(
            (scope, value.id, value.due_at, value.escalation_at, value.details.model_dump_json())
            for value in (a, b, m)
        )
        plan(w, a)  # queued before outage; must expire without a late send.
        plan(w, b)
    clock.now = start + timedelta(days=3)
    tick(w)
    sent = missed = deferred = 0
    for scope in scopes:
        w.patient_scope = scope
        outgoing = routine(w)
        accepted = [i for i in outgoing if i.status == "provider_accepted"]
        assert len([i for i in accepted if i.contact_kind == "chase"]) <= 1
        assert not [i for i in accepted if i.contact_kind == "scheduled"]
        sent += len(accepted)
        missed += sum(
            r.body["event_type"] == "CONTACT_WINDOW_MISSED" for r in w.rows("audit_event")
        )
        for r in w.rows("mission"):
            m = from_record(r, Mission)
            assert m.work_clock and m.work_clock.next_action_at > clock()
            deferred += m.details.kind != "MONITOR"
    assert (sent, deferred, missed) == (0, 8, 12)
    for scope, id, due, escalation, details in snapshots:
        m = required(store.get_mission(scope, id))
        assert (m.due_at, m.escalation_at, m.details.model_dump_json()) == (
            due,
            escalation,
            details,
        )
    # Next local morning: only four accepted chases, one for each patient.
    clock.now += timedelta(days=1)
    tick(w)
    assert (
        sum(
            r.body["status"] == "provider_accepted"
            for scope in scopes
            for r in records(store, scope, "outbound_intent")
            if r.body["notification_purpose"] == "routine_prompt"
        )
        == 4
    )


def test_bundle_crash_after_commit_resolved_reread_and_auth_epoch(
    store: StoreBase, clock: FakeClock
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    review = deadline(w, clock)
    clock.now += timedelta(days=7)
    row = required(store.get(w.doctor.scope, "bundle_schedule", w.doctor.id))
    bundle.wake(w.runtime.steward, row)
    intent = bundle_intent(w)
    # Repeated worker at the old generation cannot produce a second intent.
    bundle.wake(w.runtime.steward, row)
    assert len(list(records(store, w.doctor.scope, "outbound_intent"))) == 1
    revised = revise(w.doctor, clock(), auth_epoch=w.doctor.auth_epoch + 1)
    w.seed(revised)
    assert (
        freshness(store, intent, clock(), settings=w.runtime.dispatcher.settings)
        == "recipient_authority"
    )
    w.seed(revised.model_copy(update={"auth_epoch": intent.recipient_auth_epoch_seen}))
    review = required(store.get_review(w.patient_scope, review.id))
    w.seed(
        revise(
            review,
            clock(),
            state="resolved",
            resolved_by=w.doctor.id,
            resolved_at=clock(),
            resolved_reason="done",
            resolved_action_event_id="resolved",
            work_clock=None,
        )
    )
    assert (
        freshness(store, intent, clock(), settings=w.runtime.dispatcher.settings) == "bundle_empty"
    )
    before = len(w.transport.calls)
    assert dispatch(w, intent).suppression_reason == "bundle_empty"
    assert len(w.transport.calls) == before
    clock.now += timedelta(minutes=1)
    tick(w)
    assert (
        from_record(
            required(store.get(w.doctor.scope, "bundle_schedule", w.doctor.id)), BundleSchedule
        ).work_clock
        is None
    )


def test_bundle_restart_sent_once_and_uncertain_does_not_advance(
    store: StoreBase, clock: FakeClock
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    deadline(w, clock)
    clock.now += timedelta(days=7)
    row = required(store.get(w.doctor.scope, "bundle_schedule", w.doctor.id))
    bundle.wake(w.runtime.steward, row)
    intent = bundle_intent(w)
    w.runtime.steward = Steward(store, clock, w.runtime.steward.policy_provider)
    w.runtime.dispatcher = Dispatcher(
        w.runtime.steward, w.transport, settings=w.runtime.dispatcher.settings
    )
    w.transport.script.append(SendOutcome(status="uncertain"))
    assert dispatch(w, intent).status == "uncertain"
    schedule = from_record(
        required(store.get(w.doctor.scope, "bundle_schedule", w.doctor.id)), BundleSchedule
    )
    assert schedule.generation == 1 and schedule.last_provider_accepted_at is None
    before = len(w.transport.calls)
    clock.now += timedelta(days=8)
    tick(w)
    assert len(w.transport.calls) == before
    assert (
        from_record(
            required(store.get(w.doctor.scope, "bundle_schedule", w.doctor.id)), BundleSchedule
        ).generation
        == 1
    )


def test_first_notice_version_conflict_retry_preserves_accepted_delivery(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    m = add_mission(w, due=clock() + timedelta(hours=4))
    original = store.complete_delivery
    count: list[int] = []

    def conflict(*args: Any, **kwargs: Any) -> StoredRecord | None:
        resolution = kwargs.get("resolution")
        if resolution and resolution.obligation_stamp and not count:
            stamp = from_record(resolution.obligation_stamp, ReviewObligation)
            review = required(store.get_review(w.patient_scope, stamp.id))
            w.seed(
                revise(
                    review,
                    clock(),
                    state="acknowledged",
                    acknowledged_by=w.doctor.id,
                    acknowledged_at=clock(),
                )
            )
            count.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "complete_delivery", conflict)
    clock.now = m.due_at
    tick(w)
    notice = next(
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if r.body["notification_purpose"] == "DEADLINE"
    )
    assert len(count) == 1 and notice.status == "provider_accepted"
    assert (
        required(
            store.get_review(w.patient_scope, required(notice.review_obligation_id))
        ).first_notice_at
        == clock()
    )


def test_repeated_first_notice_conflict_delivery_sweep_recovers_stamp(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    m = add_mission(w, due=clock() + timedelta(hours=4))
    actual = store.complete_delivery
    count: list[int] = []

    def conflict(*args: Any, **kwargs: Any) -> StoredRecord | None:
        resolution = kwargs.get("resolution")
        if resolution and resolution.obligation_stamp:
            count.append(1)
            return None
        return actual(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, "complete_delivery", conflict)
        clock.now = m.due_at
        tick(w)
    notice = next(
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if r.body["notification_purpose"] == "DEADLINE"
    )
    assert (
        len(count) == 2 and notice.status == "provider_accepted" and notice.notice_feedback_pending
    )
    first = notice.accepted_at
    before = len(w.transport.calls)
    assert notice.work_clock
    clock.now = notice.work_clock.next_action_at
    tick(w)
    assert len(w.transport.calls) == before
    assert (
        required(
            store.get_review(w.patient_scope, required(notice.review_obligation_id))
        ).first_notice_at
        == first
    )
    assert (
        from_record(
            required(store.get(w.patient_scope, "outbound_intent", notice.id)), OutboundIntent
        ).notice_feedback_pending
        is False
    )
