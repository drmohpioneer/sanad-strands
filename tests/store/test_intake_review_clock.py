"""Contract 17b: exercise the production tick's review route in both stores."""

from datetime import datetime, timedelta

import pytest
from domain_fixtures import review
from harness import FakeClock

from sanad.contact import bundle
from sanad.domain import DRAFT_POLICY_2026_09 as POLICY
from sanad.domain import ReviewKind, ReviewObligation, ReviewState, TenantScope
from sanad.domain.entities import review_source_key
from sanad.ops import sweep
from sanad.steward.sweep import SweepBudget, Sweeper
from sanad.steward.types import records
from sanad.store._base import StoreBase
from sanad.store.keys import IntakeScope
from sanad.store.records import StoredRecord, model_scope, to_record
from store.concierge_fixtures import PatientWorld
from store.contact_fixtures import world


def seed_review(
    w: PatientWorld,
    *,
    kind: ReviewKind = ReviewKind.intake_clarification,
    state: ReviewState = ReviewState.open,
    source_type: str = "intake",
    patient_id: str | None = None,
) -> ReviewObligation:
    source_id = "synthetic-intake-17b"
    value = review(
        state,
        id="synthetic-review-17b",
        owner_doctor_id=w.doctor.id,
        patient_id=patient_id,
        source_type=source_type,
        source_id=source_id,
        source_version=1,
        source_mission_id=None,
        review_kind=kind,
        unique_source_key=review_source_key(w.doctor.id, source_type, source_id, 1, kind),
        created_at=w.clock(),
        updated_at=w.clock(),
    )
    w.seed(value)
    w.clock.now = value.review_at + timedelta(seconds=1)
    return value


def review_tick(w: PatientWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    # Select only the review lane; its production discovery, routing and commit run unchanged.
    monkeypatch.setattr(sweep, "LANES", ("review",))
    result = sweep.sweep_due(w.runtime, w.store, budget=SweepBudget(max_seconds=60))
    assert result["errors"] == []
    assert not result["budget_exhausted"]


def audits(w: PatientWorld, source: ReviewObligation) -> list[StoredRecord]:
    return list(records(w.store, model_scope(source), "audit_event"))


def protected_rows(w: PatientWorld, source: ReviewObligation) -> list[StoredRecord]:
    return [
        row
        for scope in (model_scope(source), w.doctor.scope, w.patient_scope)
        for kind in ("outbound_intent", "delivery_attempt", "patient", "patient_profile")
        for row in records(w.store, scope, kind)
    ]


def assert_rearmed(w: PatientWorld, source: ReviewObligation) -> None:
    changed = w.store.get_review(model_scope(source), source.id)
    assert changed is not None and changed.work_clock is not None
    assert changed.work_clock.next_action_at == w.clock() + POLICY.overdue_review_interval
    assert changed.version == source.version + 1
    assert changed.updated_at == w.clock()
    assert changed.last_work_generation == source.last_work_generation + 1
    mutable = {"work_clock", "version", "updated_at", "last_work_generation"}
    assert changed.model_dump(exclude=mutable) == source.model_dump(exclude=mutable)
    wake_audits = [
        row
        for row in audits(w, source)
        if row.body["command_id"] == f"sweep:review:{source.id}:{source.version}"
    ]
    assert len(wake_audits) == 1
    assert wake_audits[0].body["scope"] == model_scope(source).model_dump(mode="json")


@pytest.mark.parametrize(
    "state", [ReviewState.open, ReviewState.acknowledged, ReviewState.resolved]
)
def test_intake_review_clock_and_disposition(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, state: ReviewState
) -> None:
    w = world(store, clock)
    source = seed_review(w, state=state)
    assert type(model_scope(source)) is IntakeScope
    before, before_audits = protected_rows(w, source), audits(w, source)
    calls = len(w.transport.calls)
    review_tick(w, monkeypatch)
    if state == ReviewState.resolved:
        assert store.get_review(model_scope(source), source.id) == source
        assert audits(w, source) == before_audits
        bundle.review_wake(w.runtime.steward, to_record(source, model_scope(source)))
        assert store.get_review(model_scope(source), source.id) == source
        assert audits(w, source) == before_audits
    else:
        assert_rearmed(w, source)
        assert len(audits(w, source)) == len(before_audits) + 1
    assert protected_rows(w, source) == before
    assert len(w.transport.calls) == calls


def test_same_instant_tick_and_stale_version_do_not_duplicate_audit(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(store, clock)
    source = seed_review(w)
    stale = to_record(source, model_scope(source))
    review_tick(w, monkeypatch)
    assert_rearmed(w, source)
    changed, before_audits = store.get_review(model_scope(source), source.id), audits(w, source)
    review_tick(w, monkeypatch)
    bundle.review_wake(w.runtime.steward, stale)
    assert store.get_review(model_scope(source), source.id) == changed
    assert audits(w, source) == before_audits


@pytest.mark.parametrize("kind", [ReviewKind.result_review, ReviewKind.delivery_failure])
def test_other_intake_review_kinds_are_untouched(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, kind: ReviewKind
) -> None:
    w = world(store, clock)
    source = seed_review(w, kind=kind)
    assert type(model_scope(source)) is IntakeScope
    before, before_audits = protected_rows(w, source), audits(w, source)
    review_tick(w, monkeypatch)
    assert store.get_review(model_scope(source), source.id) == source
    assert audits(w, source) == before_audits
    assert protected_rows(w, source) == before


def test_tenant_delivery_failure_still_rearms(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(store, clock)
    source = seed_review(w, kind=ReviewKind.delivery_failure, source_type="outbound_intent")
    assert type(model_scope(source)) is TenantScope
    before, before_audits = protected_rows(w, source), audits(w, source)
    review_tick(w, monkeypatch)
    assert_rearmed(w, source)
    assert len(audits(w, source)) == len(before_audits) + 1
    assert protected_rows(w, source) == before


def test_patient_intake_clarification_uses_existing_accountability(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(store, clock)
    source = seed_review(w, source_type="inbound_receipt", patient_id=w.patient_scope.patient_id)
    before_audits = audits(w, source)
    # The patient shape is rejected even if a caller bypasses tick routing.
    bundle.review_wake(w.runtime.steward, to_record(source, w.patient_scope))
    assert store.get_review(w.patient_scope, source.id) == source
    assert audits(w, source) == before_audits
    seen: list[str] = []
    original = Sweeper.accountability

    def accountability(worker: Sweeper, row: StoredRecord, now: datetime) -> None:
        seen.append(row.id)
        original(worker, row, now)

    def wrong_route(*args: object) -> None:
        pytest.fail("Patient-bearing review reached patientless review_wake")

    monkeypatch.setattr(Sweeper, "accountability", accountability)
    monkeypatch.setattr(bundle, "review_wake", wrong_route)
    review_tick(w, monkeypatch)
    assert seen == [source.id]
    assert_rearmed(w, source)
