"""11b actual authenticated worker calls and the unchanged tick recovery lane."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from harness import FakeClock

from sanad.api.internal import internal_router
from sanad.ops.nonce_store import NonceStore, TickVerifier
from sanad.ops.sweep import sweep_due
from sanad.ops.tick_signing import signed_headers
from sanad.ops.worker import worker_body
from sanad.steward.inline import DeliveryScope, dispatch_inline
from sanad.store._base import StoreBase, Write
from sanad.store.records import record_item, to_record
from store.account_fixtures import PATIENT, update
from store.concierge_fixtures import PatientWorld
from store.scribe_fixtures import ScribeWorld


def worker_calls(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], dict[str, str]]:
    queue = []
    monkeypatch.setattr(
        "store.login_fixtures.process_event", lambda runtime, event: queue.append(event)
    )
    verifier = TickVerifier("synthetic-11b-secret", NonceStore(world.store, "11b"), world.clock)
    world.app.include_router(
        internal_router(verifier, lambda: sweep_due(world.runtime, world.store), world.runtime)
    )

    def run() -> dict[str, str]:
        event = queue.pop(0)
        event["headers"] = signed_headers(
            "synthetic-11b-secret",
            str(int(world.clock().timestamp())),
            uuid4().hex * 2,
            worker_body(event),
        )
        with world.client() as client:
            response = client.post("/events", json=event)
        assert response.status_code == 200
        return cast(dict[str, str], response.json())

    return run


def test_qr_same_events_call_and_inline_failure_recovers_once(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve()
    world.dictate(
        "مريض جديد آدم taking كونكور 5 مج",
        {
            "patient": {"name_as_spoken": "آدم"},
            "orders": [{"action": "continue", "drug": "كونكور", "dose": "5 مج"}],
        },
    )
    run = worker_calls(world, monkeypatch)
    world.tap()
    assert not any(i.template_id == "scribe_invitation" for i in world.intents())
    run()
    invite = next(i for i in world.intents() if i.template_id == "scribe_invitation")
    assert invite.status == "provider_accepted"
    patient = world.claims.patient(
        world.doctor.id,
        str(
            world.store.list_records(world.doctor.scope, "scribe_invitation_work")[0][0].body[
                "patient_id"
            ]
        ),
    )
    assert patient
    assert len(store.list_records(patient.scope, "care_order_head")[0]) == 1
    assert not store.list_records(patient.scope, "followup")[0]

    def crash(*args: Any) -> None:
        raise RuntimeError("synthetic inline failure")

    monkeypatch.setattr("sanad.api.internal.dispatch_inline", crash)
    # A second explicit QR request, with the same POST /events crash boundary.
    world.post(update(world.owner.subject, "/qr آدم", 30))
    run()
    pending = [
        i for i in world.intents() if i.template_id == "scribe_invitation" and i.status == "queued"
    ]
    assert len(pending) == 1
    before = len(world.transport.calls)
    sweep_due(world.runtime, store)
    assert len(world.transport.calls) == before + 1
    sweep_due(world.runtime, store)
    assert len(world.transport.calls) == before + 1


def test_done_same_events_call_after_arabic_start_report(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = cast(PatientWorld, PatientWorld.create(store, clock))
    world.enroll()
    run = worker_calls(world, monkeypatch)
    world.post(update(PATIENT, "بدأت أتورفاستاتين", 800))
    run()
    done = [
        r
        for r in world.rows("outbound_intent")
        if r.body["notification_purpose"] == "DONE:FULFILLMENT"
    ]
    assert len(done) == 1 and done[0].body["status"] == "provider_accepted"


def test_inline_scope_due_limit_stale_and_elapsed_bound(store: StoreBase, clock: FakeClock) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve()
    target = None
    for n in range(13):
        intent = world.scribe.repo.intent(
            world.doctor, "synthetic_11b", {"text": "synthetic"}, f"inline-{n}"
        )
        if n == 0:
            intent = intent.model_copy(update={"expires_at": clock() - timedelta(seconds=1)})
        if n == 12:
            assert intent.work_clock is not None
            intent = intent.model_copy(
                update={
                    "work_clock": intent.work_clock.model_copy(
                        update={"next_action_at": clock() + timedelta(hours=1)}
                    )
                }
            )
        target = intent.scope
        row = to_record(intent, intent.scope)
        assert store._atomic([Write(record_item(row), None)], [])
    assert target
    others = world.intents()
    before = {i.id: i.model_dump() for i in others}
    assert dispatch_inline(world.runtime.dispatcher, (DeliveryScope(target),)) == 10
    assert before == {i.id: i.model_dump() for i in world.intents()}
    rows = store.list_records(target, "outbound_intent")[0]
    assert sum(r.body["status"] == "queued" for r in rows) == 3
    assert sum(r.body["status"] == "suppressed" for r in rows) == 1
    # No extra operation begins after the 15-second deadline, including pagination.
    elapsed = iter([0, 15])
    assert (
        dispatch_inline(
            world.runtime.dispatcher, (DeliveryScope(target),), elapsed_clock=lambda: next(elapsed)
        )
        == 0
    )
