"""Exercise stamp guards with distinct instants on every read, not a frozen clock."""

from datetime import datetime, timedelta

import pytest
from domain_fixtures import NOW
from harness import FakeClock
from store import evidence_fixtures as evidence
from store import medication_fixtures as medication
from store import resolver_fixtures as resolver
from store import test_executors_15 as executors
from store import test_medication_guards as guards
from store import test_question_digest_17c as digest_tests
from store.test_evidence import test_doctor_actions_resolve_review as evidence_case

from sanad.store._base import StoreBase


class AdvancingClock(FakeClock):
    ticking: bool = True

    def __call__(self) -> datetime:
        return self.advance(timedelta(microseconds=1)) if self.ticking else self.now


@pytest.fixture
def clock() -> AdvancingClock:
    return AdvancingClock(NOW)


def test_clarification_and_suppression_guards(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    guards.test_clarification_guard_accepts_only_field_projection(store, clock, monkeypatch)


def test_medication_barrier_and_scoped_suppression(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    guards.test_barrier_and_scoped_suppression_guards(store, clock, monkeypatch)


def test_visit_window_and_fulfillment(store: StoreBase, clock: FakeClock) -> None:
    w = medication.world(store, clock)
    executors.test_booking_local_window_and_early_attendance(w)


def test_task_fulfillment(store: StoreBase, clock: FakeClock) -> None:
    w = medication.world(store, clock)
    executors.test_task_done_doctor_accept_or_reopen(w, True)


def test_monitor_revision(store: StoreBase, clock: FakeClock) -> None:
    from store.test_monitor import current, monitor

    w = evidence.world(store, clock)
    monitor(w, count=1)
    w.send("BP 120/80")
    assert current(w).state == "fulfilled"
    from sanad.domain.entities import MonitorDetails

    details = current(w).details
    assert isinstance(details, MonitorDetails) and len(details.readings) == 1


@pytest.mark.parametrize("kind", ["MEDICATION", "TEST", "VISIT", "TASK", "MONITOR", "SEND_RECORDS"])
def test_resolver_checkpoint_hold_and_suppression(
    store: StoreBase, clock: AdvancingClock, kind: str
) -> None:
    w, capture = resolver.setup(store, clock)
    clock.ticking = False
    before = resolver.add(w, kind)
    clock.ticking = True
    queued = None
    if kind == "MEDICATION":
        from sanad.steward.apply import make_intent
        from sanad.store.records import to_record

        snap = medication.snapshot(w)
        queued = make_intent(
            w.patient_scope,
            "resolver-prompt",
            (to_record(before, w.patient_scope).ref,),
            "routine_prompt",
            "synthetic",
            clock(),
            w.runtime.steward.policy_provider(w.patient_scope),
            snap.authority,
            snap.profile,
            audience="patient",
            slot="resolver-prompt",
            order_refs=before.order_refs,
        )
        w.seed(queued)
    model, reply = w.send(
        "I cannot afford it", {"step": "ask_patient", "question": resolver.QUESTION}
    )
    changed = resolver.get(w, before.id)
    assert changed.state == "blocked" and changed.due_at == before.due_at
    assert changed.barrier_attempts[-1].questions_spent == 1
    assert changed.barrier_attempts[-1].question == resolver.QUESTION
    assert resolver.QUESTION in str(reply.payload) and not capture.requests
    assert len(model.script.calls) == 1
    if queued:
        row = store.get(w.patient_scope, "outbound_intent", queued.id)
        assert row and row.body["status"] == "suppressed"
    w.send("what is my plan")
    assert resolver.get(w, before.id).state == "blocked"


@pytest.mark.parametrize("method", ["api", "telegram"])
def test_evidence_acceptance(store: StoreBase, clock: FakeClock, method: str) -> None:
    evidence_case(evidence.world(store, clock), method, "accept")


def test_question_digest_arm_and_delivery(store: StoreBase, clock: FakeClock) -> None:
    from store.contact_fixtures import dispatch, required
    from store.executors_15_fixtures import world

    from sanad.contact import question_digest
    from sanad.store.records import to_record

    w = world(store, clock)
    first, second = digest_tests.two(w)
    digest_tests.overdue(w, first, second)
    armed = digest_tests.schedule(w)
    assert armed.version == 2
    clock.now = required(armed.next_action_at)
    question_digest.wake(w.runtime.steward, to_record(armed, w.doctor.scope))
    sent = dispatch(w, digest_tests.packed(w)[0])
    assert sent.status == "provider_accepted" and not sent.notice_feedback_pending
    assert len(sent.question_listing_targets) == 2


def test_resolved_barrier_projection(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    guards.test_resolved_barrier_projection_accepts_only_exact_event(store, clock, monkeypatch)
