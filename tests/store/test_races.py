from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import Any

import pytest
from domain_fixtures import NOW, followup, mission, review

from sanad.domain import MissionState, PatientScope, TenantScope
from sanad.store import keys
from sanad.store._base import Check, Item, StoreBase, Write
from sanad.store.keys import ScopedKey
from sanad.store.records import (
    Accepted,
    CommitRequest,
    Duplicate,
    Forbidden,
    MarkerRecord,
    StaleVersion,
    StoredRecord,
    record_item,
    to_record,
)
from store.fixtures import OTHER, SCOPE, TENANT, capability, intent, profile, request


@pytest.mark.parametrize("model", [mission(), followup(), review(), intent()])
def test_claimed_work_requires_live_token_and_preserves_generation(
    store: StoreBase,
    model: Any,
    clock: Any,
) -> None:
    original = to_record(model, SCOPE)
    assert isinstance(store.commit(request(original)), Accepted)
    first = store.claim_work(
        original.scoped_key(SCOPE), 1, "worker-one", NOW, timedelta(seconds=10)
    )
    assert first is not None
    clock.now = NOW + timedelta(seconds=10)
    second = store.claim_work(
        original.scoped_key(SCOPE), 2, "worker-two", clock.now, timedelta(seconds=10)
    )
    assert second is not None and second.generation == 2
    current = store.get(SCOPE, original.entity_type, original.id)
    assert current is not None and current.version == 3
    updated_model = type(model).model_validate(
        current.body | {"version": 4, "updated_at": clock.now}
    )
    updated = to_record(updated_model, SCOPE)
    base = request(updated)
    assert isinstance(store.commit(base), StaleVersion)
    for token, expected in [(first, StaleVersion), (second, Accepted)]:
        batch = CommitRequest.model_validate(
            base.model_dump()
            | {
                "command": base.command.model_dump() | {"work_claim": token},
            }
        )
        assert isinstance(store.commit(batch), expected)
    released = store.get(SCOPE, original.entity_type, original.id)
    assert released is not None and released.processing_claim is None
    third = store.claim_work(
        original.scoped_key(SCOPE), 4, "worker-three", clock.now, timedelta(seconds=10)
    )
    assert third is not None and third.generation == 3


def test_active_and_replaced_patient_fences_cannot_be_omitted(store: StoreBase, clock: Any) -> None:
    assert isinstance(
        store.commit(request(to_record(profile(), SCOPE), to_record(mission(), SCOPE))), Accepted
    )
    first = store.acquire_patient(SCOPE, "one", NOW, timedelta(seconds=5))
    assert first is not None
    batch = request(to_record(mission(version=2), SCOPE))
    assert isinstance(store.commit(batch), StaleVersion)
    fenced = CommitRequest.model_validate(
        batch.model_dump()
        | {
            "command": batch.command.model_dump() | {"fence": first},
        }
    )
    clock.now = NOW + timedelta(seconds=5)
    second = store.acquire_patient(SCOPE, "two", clock.now, timedelta(seconds=5))
    assert second is not None
    assert isinstance(store.commit(fenced), StaleVersion)
    current = CommitRequest.model_validate(
        batch.model_dump()
        | {
            "command": batch.command.model_dump() | {"fence": second},
        }
    )
    assert isinstance(store.commit(current), Accepted)
    store.release_patient(second)
    assert isinstance(store.commit(request(to_record(mission(version=3), SCOPE))), StaleVersion)


def test_two_concurrent_versions_have_one_winner(
    store: StoreBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isinstance(store.commit(request(to_record(mission(), SCOPE))), Accepted)
    barrier = Barrier(2, timeout=10)
    real_atomic = store._atomic

    def racing(writes: list[Write], checks: list[Check]) -> bool:
        barrier.wait()
        return real_atomic(writes, checks)

    monkeypatch.setattr(store, "_atomic", racing)
    requests = [
        request(to_record(mission(version=2, title=f"synthetic-{i}"), SCOPE)) for i in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(store.commit, requests))
    assert sum(isinstance(outcome, Accepted) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, StaleVersion) for outcome in outcomes) == 1
    winner = next(i for i, result in enumerate(outcomes) if isinstance(result, Accepted))
    assert store.get_mission(SCOPE, "synthetic-mission").title == f"synthetic-{winner}"  # type: ignore[union-attr]


def test_patient_takeover_between_read_and_commit_rolls_back(
    store: StoreBase, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isinstance(store.commit(request(to_record(profile(), SCOPE))), Accepted)
    lease = store.acquire_patient(SCOPE, "old", NOW, timedelta(seconds=10))
    assert lease is not None
    batch = request(to_record(mission(), SCOPE))
    batch = CommitRequest.model_validate(
        batch.model_dump()
        | {
            "command": batch.command.model_dump() | {"fence": lease},
        }
    )
    real_atomic = store._atomic
    injected = False

    def takeover(writes: list[Write], checks: list[Check]) -> bool:
        nonlocal injected
        if not injected:
            injected = True
            clock.now = NOW + timedelta(seconds=10)
            assert store.acquire_patient(SCOPE, "new", clock.now, timedelta(seconds=10)) is not None
        return real_atomic(writes, checks)

    monkeypatch.setattr(store, "_atomic", takeover)
    assert isinstance(store.commit(batch), StaleVersion)
    assert store.get_mission(SCOPE, "synthetic-mission") is None


def test_delivery_source_change_during_start_is_atomic(
    store: StoreBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = to_record(mission(), SCOPE)
    outgoing = to_record(intent(source_versions=(source.ref,)), SCOPE)
    assert isinstance(store.commit(request(source, outgoing)), Accepted)
    real_atomic = store._atomic
    changed = False

    def amend(writes: list[Write], checks: list[Check]) -> bool:
        nonlocal changed
        if not changed:
            changed = True
            assert store._update(record_item(to_record(mission(version=2), SCOPE)), 1)
        return real_atomic(writes, checks)

    monkeypatch.setattr(store, "_atomic", amend)
    assert (
        store.start_delivery(outgoing.id, (outgoing.ref, source.ref), "worker", NOW, scope=SCOPE)
        is None
    )
    assert store.get(SCOPE, "outbound_intent", outgoing.id) == outgoing
    assert store._query(outgoing.pk, prefix="ATTEMPT#")[0] == []


def test_reconciliation_conflict_does_not_replace_new_clinical_state(
    store: StoreBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = to_record(mission(), SCOPE)
    assert isinstance(store.commit(request(original)), Accepted)
    damaged = record_item(original)
    damaged.pop("due_lane_shard")
    assert store._update(damaged, 1)
    real_update = store._update
    replacement = to_record(mission(version=2, title="synthetic revised title"), SCOPE)

    def conflict(item: Item, before: int) -> bool:
        assert real_update(record_item(replacement), before)
        return real_update(item, before)

    monkeypatch.setattr(store, "_update", conflict)
    report = store.reconcile_partition(SCOPE)
    assert report.conflicts == (original.key,) and report.repaired == ()
    assert store.get(SCOPE, "mission", original.id) == replacement


def test_due_cursors_do_not_disclose_or_cross_scopes(store: StoreBase) -> None:
    from sanad.domain import WorkClock

    for scope in [SCOPE, OTHER]:
        model = mission(
            doctor_id=scope.doctor_id,
            id=f"{scope.doctor_id}-mission",
            work_clock=WorkClock(next_action_at=NOW, work_lane="mission"),
        )
        batch = request(to_record(model, scope))
        batch = CommitRequest.model_validate(
            batch.model_dump()
            | {
                "command": batch.command.model_dump()
                | {
                    "scope": scope,
                    "principal": batch.command.principal.model_dump()
                    | {"doctor_id": scope.doctor_id},
                },
            }
        )
        assert isinstance(store.commit(batch), Accepted)
    rows, cursor = store.query_due("mission", "0", NOW, limit=1, capability=capability(TENANT))
    assert cursor is not None and set(cursor.position) == {"token"}
    assert OTHER.doctor_id not in cursor.model_dump_json()
    assert store.query_due("mission", "0", NOW, cursor, 1, capability=capability(OTHER)) == (
        (),
        None,
    )
    assert len(rows) == 1 and isinstance(rows[0].record_key.scope, PatientScope)
    assert store.get(rows[0].record_key.scope, rows[0].entity_type, rows[0].id) is not None


def test_explicit_uniqueness_markers_are_verified_and_ttl_is_only_stored(store: StoreBase) -> None:
    outgoing = to_record(intent(), SCOPE)
    key = keys.uniqueness(SCOPE, "OUTKEY", keys.digest(intent().logical_key))
    marker = MarkerRecord(
        scope=SCOPE, pk=key.pk, sk=key.sk, target=outgoing.scoped_key(SCOPE), created_at=NOW, ttl=1
    )
    batch = request(outgoing, markers=(marker,))
    assert isinstance(store.commit(batch), Accepted)
    assert store._read(key)["ttl"] == 1  # type: ignore[index]
    assert isinstance(store.commit(batch), Duplicate)
    forged = MarkerRecord.model_validate(
        marker.model_dump()
        | {
            "sk": "OUTKEY#" + "f" * 64,
        }
    )
    assert isinstance(store.commit(request(outgoing, markers=(forged,))), Forbidden)
    assert isinstance(store.commit(request(outgoing, markers=(marker, marker))), StaleVersion)


def test_independent_review_survives_terminal_mission(store: StoreBase) -> None:
    obligation = to_record(review(), SCOPE)
    assert isinstance(
        store.commit(request(to_record(mission(MissionState.fulfilled), SCOPE), obligation)),
        Accepted,
    )
    assert (
        store.query_due("mission", "0", NOW + timedelta(days=100), capability=capability())[0] == ()
    )
    hints, _ = store.query_due("review", "0", NOW + timedelta(days=100), capability=capability())
    assert len(hints) == 1 and hints[0].id == obligation.id
    assert store.get_review(SCOPE, obligation.id) == review()


def test_transaction_at_supported_100_action_boundary(store: StoreBase) -> None:
    # 98 new missions + CMD marker + patient-profile absence check = 100 actions.
    records = [to_record(mission(id=f"synthetic-boundary-{i}"), SCOPE) for i in range(98)]
    result = store.commit(request(*records))
    assert isinstance(result, Accepted) and len(result.resulting_versions) == 98


def test_forged_envelope_and_marker_scope_are_rejected_without_writes(store: StoreBase) -> None:
    original = to_record(mission(), SCOPE)
    forged = StoredRecord.model_validate(original.model_dump() | {"pk": keys.partition(OTHER)})
    assert isinstance(store.commit(request(forged)), Forbidden)
    global_key = keys.token("login", "e" * 64)
    marker = MarkerRecord(
        scope=OTHER,
        pk=global_key.pk,
        sk=global_key.sk,
        target=ScopedKey(scope=OTHER, pk=original.pk, sk=original.sk),
        created_at=NOW,
    )
    assert isinstance(store.commit(request(original, markers=(marker,))), Forbidden)
    assert store._read(global_key) is None and store.get_mission(SCOPE, original.id) is None


def test_same_patient_id_under_another_doctor_never_broadens_queries(store: StoreBase) -> None:
    original = to_record(mission(), SCOPE)
    assert isinstance(store.commit(request(original)), Accepted)
    assert store.get_mission(OTHER, original.id) is None
    assert store.list_events(OTHER) == ((), None)
    assert store.list_reviews(TenantScope(doctor_id=OTHER.doctor_id)) == ((), None)
    assert store.get_followup(OTHER, "synthetic-followup") is None
    assert store.get_review(OTHER, "synthetic-review") is None
    assert (
        store.claim_work(original.scoped_key(OTHER), 1, "other", NOW, timedelta(minutes=1)) is None
    )


def test_unassigned_intake_reviews_and_delivery_stay_doctor_private(store: StoreBase) -> None:
    from domain_fixtures import POLICY, review_payload

    from sanad.domain import create_review
    from sanad.store.keys import IntakeScope
    from sanad.store.records import OutboundIntent, ReviewCreation

    scope = IntakeScope(doctor_id=SCOPE.doctor_id, intake_id="synthetic-intake")
    foreign = IntakeScope(doctor_id=OTHER.doctor_id, intake_id=scope.intake_id)
    model = create_review(
        review_payload(patient_id=None, source_type="intake", source_id=scope.intake_id),
        NOW,
        POLICY,
    ).aggregate
    assert isinstance(model, type(review()))
    record, created = store.create_or_get_review(ReviewCreation(scope=scope, review=model), NOW)
    assert record is not None and created
    assert record.pk == "D#synthetic-doctor#INTAKE#synthetic-intake"
    assert store.get_review(foreign, record.id) is None
    assert store.get_review(SCOPE, record.id) is None
    assert store.get_review(scope, record.id) == model
    assert store.list_reviews(TENANT)[0] == (record,)
    outgoing_model = OutboundIntent.model_validate(
        intent().model_dump()
        | {
            "scope": scope,
            "scope_kind": "intake",
            "audience": "doctor",
            "source_versions": (record.ref,),
        }
    )
    outgoing = to_record(outgoing_model, scope)
    batch = request(outgoing)
    batch = CommitRequest.model_validate(
        batch.model_dump()
        | {
            "command": batch.command.model_dump() | {"scope": scope},
        }
    )
    assert isinstance(store.commit(batch), Accepted)
    assert (
        store.start_delivery(outgoing.id, (outgoing.ref, record.ref), "other", NOW, scope=foreign)
        is None
    )
    attempt = store.start_delivery(
        outgoing.id, (outgoing.ref, record.ref), "owner", NOW, scope=scope
    )
    assert attempt is not None
    result = store.complete_delivery(
        attempt.id, "provider_accepted", "synthetic-provider-id", scope=scope
    )
    assert result is not None and result.patient_id is None


@pytest.mark.parametrize(
    "record",
    [
        to_record(profile(normalized_name="x" * 1024), SCOPE),
        to_record(mission(id="x" * 1024), SCOPE),
    ],
)
def test_oversized_primary_or_index_key_never_reaches_backend(
    store: StoreBase,
    record: StoredRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sanad.store.records import TooLarge

    def no_read(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("an oversized key reached the backend")

    monkeypatch.setattr(store, "_read", no_read)
    result = store.commit(request(record))
    assert isinstance(result, TooLarge) and result.reason == "item_bytes"


def test_oversized_review_creation_has_same_rejection_on_both_stores(store: StoreBase) -> None:
    from domain_fixtures import POLICY, review_payload

    from sanad.domain import ReviewObligation, create_review
    from sanad.store.records import ReviewCreation

    model = create_review(review_payload(source_id="x" * (300 * 1024)), NOW, POLICY).aggregate
    assert isinstance(model, ReviewObligation)
    assert store.create_or_get_review(ReviewCreation(scope=SCOPE, review=model), NOW) == (
        None,
        False,
    )
