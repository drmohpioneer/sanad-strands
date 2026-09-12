"""T51 controlled worker overlap and T12 old-generation delivery of ticks."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, local
from typing import Any

import pytest
from harness import FakeClock
from store.contact_fixtures import add_mission, dispatch, plan, required, routine, tick, world

from sanad.channels.transport import SendOutcome
from sanad.domain import FollowUpTask
from sanad.domain.entities import MonitorDetails
from sanad.steward.dispatch import Dispatcher
from sanad.steward.service import Steward
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.records import from_record, to_record
from system.subjects import DOCTOR_A, PATIENT_A, SubjectWorld


def test_t51_two_workers_one_chase_with_independent_scheduled_prompts(
    store: StoreBase,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock.now = clock().replace(hour=7)
    w = world(store, clock, medication=True)
    w.send("بدأت الدوا")
    follow = from_record(w.rows("followup")[0], FollowUpTask)
    clock.now = required(follow.prompt_at)
    first, second = add_mission(w, "first"), add_mission(w, "second")
    monitor = add_mission(
        w,
        "bp",
        kind="MONITOR",
        details=MonitorDetails(
            metric="blood pressure", unit="mmHg", slots=(clock(),), required_coverage=1
        ),
    )
    plan(w, first)
    first_intent = next(i for i in routine(w) if i.status == "queued" and i.contact_kind == "chase")
    # Fixture two independently prepared outboxes. The scheduler normally coalesces
    # them; dispatch must still fence competing recovered intents with the same day.
    w.seed(first_intent.model_copy(update={"status": "failed"}))
    plan(w, second)
    w.seed(first_intent)
    for source in (monitor, follow):
        plan(w, source)
    chasing = [i for i in routine(w) if i.status == "queued" and i.contact_kind == "chase"]
    assert len(chasing) == 2
    rendezvous = Barrier(2, timeout=20)
    seen = local()
    acquire = store.acquire_patient
    outcomes: list[bool] = []

    def overlap(*args: Any, **kwargs: Any) -> Any:
        if getattr(seen, "entered", False):
            return acquire(*args, **kwargs)
        seen.entered = True
        rendezvous.wait()
        result = acquire(*args, **kwargs)
        outcomes.append(result is not None)
        # The winner cannot reserve/release until the competing acquisition returned.
        rendezvous.wait()
        return result

    w.transport.script.append(SendOutcome(status="uncertain"))
    workers = [
        Dispatcher(
            Steward(store, clock, w.runtime.steward.policy_provider),
            w.transport,
            settings=w.runtime.dispatcher.settings,
        )
        for _ in range(2)
    ]
    with monkeypatch.context() as patch:
        patch.setattr(store, "acquire_patient", overlap)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    worker.dispatch_one,
                    to_record(intent, intent.scope).scoped_key(intent.scope),
                    f"worker-{index}",
                    clock(),
                )
                for index, (worker, intent) in enumerate(zip(workers, chasing, strict=True))
            ]
            results = [future.result(timeout=30) for future in futures]
    assert sorted(outcomes) == [False, True]
    assert sum(result is not None and result.status == "uncertain" for result in results) == 1
    reservation = required(store._read(keys.contact(w.patient_scope, required(chasing[0].slot_id))))
    assert reservation["state"] == "reserved"
    before = len(w.transport.calls)
    for intent in chasing:
        dispatch(w, intent)
    assert len(w.transport.calls) == before
    scheduled = [i for i in routine(w) if i.status == "queued" and i.contact_kind == "scheduled"]
    assert {i.template_id for i in scheduled} == {"patient_monitor_prompt", "patient_day3_prompt"}
    for intent in scheduled:
        assert dispatch(w, intent).status == "provider_accepted"
    assert len(w.transport.calls) == before + 2
    tick(w)
    assert len(w.transport.calls) == before + 2
    assert (
        required(store._read(keys.contact(w.patient_scope, required(chasing[0].slot_id))))["state"]
        == "reserved"
    )


def test_t12_out_of_order_tick_after_extension_cannot_restore_old_deadline(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.domain import ExplicitTiming
    from sanad.store.records import CommandEnvelope

    w = SubjectWorld.for_subjects(store, clock, doctor=DOCTOR_A, patient=PATIENT_A)
    w.approve(DOCTOR_A, language="en")
    w.enroll_named("Synthetic Tick Patient", id=100)
    mission = add_mission(w, "out-of-order", due=clock() + timedelta(hours=4))
    old = to_record(mission, w.patient_scope)
    clock.now = mission.due_at
    later = clock() + timedelta(days=45)
    assert (
        w.runtime.steward.handle(
            CommandEnvelope(
                command_id="extend-system",
                principal=w.owner,
                scope=w.patient_scope,
                requested_at=clock(),
                payload={
                    "type": "ExtendMission",
                    "mission_id": mission.id,
                    "timing": ExplicitTiming(
                        instant=later, original_expression="in 45 days", timezone="Africa/Cairo"
                    ).model_dump(mode="json"),
                    "reason": "Synthetic extension",
                },
            )
        ).status
        == "accepted"
    )
    notices_before = [
        r for r in w.rows("outbound_intent") if r.body["notification_purpose"] == "DEADLINE"
    ]
    # Deliver an old persisted wake after the new generation has committed.
    for _ in range(2):
        from sanad.steward.service import system_command

        w.runtime.steward.handle(
            system_command(
                w.patient_scope,
                f"sweep:mission:{old.id}:{old.version}",
                {"type": "_Deadline", "mission_id": old.id},
                clock(),
                lane="mission",
            )
        )
    current = required(store.get_mission(w.patient_scope, mission.id))
    assert current.due_at == later and current.state != "overdue"
    assert notices_before == [
        r for r in w.rows("outbound_intent") if r.body["notification_purpose"] == "DEADLINE"
    ]
