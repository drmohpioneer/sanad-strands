"""Synthetic enrolled contact world, real Steward/store/dispatcher with a fake clock."""

import json
from datetime import datetime, timedelta
from typing import Any, cast

from domain_fixtures import mission
from harness import FakeClock
from pydantic import BaseModel

from sanad.contact.scheduler import schedule
from sanad.domain import Mission, MissionState, WorkClock
from sanad.ops.sweep import sweep_due
from sanad.store._base import StoreBase
from sanad.store.records import OutboundIntent, from_record, to_record
from store.concierge_fixtures import PatientWorld, no_problem_readers


def world(store: StoreBase, clock: FakeClock, *, medication: bool = False) -> PatientWorld:
    value = cast(PatientWorld, PatientWorld.create(store, clock))
    value.enroll(medication=medication)
    value.concierge.barrier_model_factory = no_problem_readers
    tick(value)
    return value


def add_mission(
    w: PatientWorld,
    id: str = "test",
    *,
    due: datetime | None = None,
    state: MissionState = MissionState.open,
    **fields: object,
) -> Mission:
    now = w.clock()
    due = due or now + timedelta(days=14)
    value = mission(
        state=state,
        id=id,
        doctor_id=w.patient_scope.doctor_id,
        patient_id=w.patient_scope.patient_id,
        created_at=now,
        updated_at=now,
        confirmed_at=now,
        confirmed_by=w.doctor.id,
        due_at=due,
        escalation_at=due,
        review_at=due,
        order_refs=(),
        title="تحليل مطلوب",
        work_clock=WorkClock(work_lane="mission", next_action_at=now),
        **fields,
    )
    w.seed(value)
    return value


def routine(w: PatientWorld) -> list[OutboundIntent]:
    return [i for i in w.patient_intents() if i.notification_purpose == "routine_prompt"]


def plan(w: PatientWorld, source: BaseModel) -> list[OutboundIntent]:
    before = clinical_snapshot(w)
    result = schedule(w.runtime.steward, to_record(source, w.patient_scope))
    assert_clinical_unchanged(before, clinical_snapshot(w))
    assert result.status == "accepted", result
    return routine(w)


def dispatch(w: PatientWorld, intent: OutboundIntent) -> OutboundIntent:
    result = w.runtime.dispatcher.dispatch_one(
        to_record(intent, intent.scope).scoped_key(intent.scope), "contact-test", w.clock()
    )
    assert result
    return result


def tick(w: PatientWorld) -> dict[str, Any]:
    before = clinical_snapshot(w)
    result = sweep_due(w.runtime, w.store, elapsed_clock=lambda: 0)
    assert_clinical_unchanged(before, clinical_snapshot(w))
    assert result["errors"] == [], result
    return result


def current(w: PatientWorld, m: Mission) -> Mission:
    row = w.store.get(w.patient_scope, "mission", m.id)
    assert row
    return from_record(row, Mission)


def required[T](value: T | None) -> T:
    assert value is not None
    return value


def clinical_snapshot(w: PatientWorld) -> dict[str, str]:
    return {
        r.entity_type + ":" + r.id: json.dumps(
            {k: r.body.get(k) for k in ("due_at", "escalation_at", "prompt_at", "details")},
            sort_keys=True,
            ensure_ascii=False,
        )
        for kind in ("mission", "followup")
        for r in w.rows(kind)
    }


def assert_clinical_unchanged(before: dict[str, str], after: dict[str, str]) -> None:
    for key, value in before.items():
        assert after[key] == value, key
