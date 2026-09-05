from datetime import timedelta
from typing import Any

import pytest
from domain_fixtures import NOW, followup, mission, review

from sanad.domain import Mission, PatientScope, TenantScope
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.keys import ScopedKey
from sanad.store.protocol import Store
from sanad.store.records import (
    Accepted,
    CommitRequest,
    DeliveryAttempt,
    Duplicate,
    Forbidden,
    InboundReceipt,
    MarkerRecord,
    ReceiptCompletion,
    ReviewCreation,
    SessionSnapshot,
    StaleVersion,
    StoredRecord,
    TooLarge,
    from_record,
    record_item,
    to_record,
)
from store.fixtures import (
    ACTOR,
    OTHER,
    SCOPE,
    TENANT,
    capability,
    event,
    inbound,
    intent,
    profile,
    request,
)


def accepted(store: StoreBase, *records: StoredRecord) -> Accepted:
    result = store.commit(request(*records))
    assert isinstance(result, Accepted), result
    return result


def test_protocol_and_detached_reads(store: StoreBase) -> None:
    protocol: Store = store
    record = to_record(mission(), SCOPE)
    accepted(store, record)
    loaded = protocol.get(SCOPE, "mission", record.id)
    assert loaded == record
    assert loaded is not None
    loaded.body["title"] = "mutated caller object"
    assert store.get_mission(SCOPE, record.id) == mission()
    record.body["title"] = "mutated input object"
    assert store.get_mission(SCOPE, record.id) == mission()


@pytest.mark.parametrize("stale_index", [0, 2, 4])
def test_stale_one_of_five_puts_rolls_back_events_intents_markers_and_command(
    store: StoreBase,
    stale_index: int,
) -> None:
    originals = [to_record(mission(id=f"synthetic-mission-{i}"), SCOPE) for i in range(5)]
    accepted(store, *originals)
    updates = [to_record(mission(id=r.id, version=2), SCOPE) for r in originals]
    updates[stale_index] = to_record(mission(id=originals[stale_index].id, version=3), SCOPE)
    audit = to_record(event("atomic-command"), SCOPE)
    outgoing = to_record(intent(), SCOPE)
    marker_key = keys.subject("synthetic-bot", "90071992547409931234567890")
    marker = MarkerRecord(
        scope=SCOPE,
        pk=marker_key.pk,
        sk=marker_key.sk,
        target=originals[0].scoped_key(SCOPE),
        created_at=NOW,
    )
    batch = request(
        *updates,
        command_id="atomic-command",
        events=(audit,),
        intents=(outgoing,),
        expected=tuple(r.ref for r in [*updates, audit, outgoing]),
        markers=(marker,),
    )
    result = store.commit(batch)
    assert isinstance(result, StaleVersion)
    for original in originals:
        assert store.get(SCOPE, "mission", original.id) == original
    assert store.list_events(SCOPE)[0] == ()
    assert store.get(SCOPE, "outbound_intent", outgoing.id) is None
    assert store._read(marker_key) is None
    assert store._read(keys.uniqueness(SCOPE, "CMD", "atomic-command")) is None
    assert store._read(keys.uniqueness(SCOPE, "OUTKEY", keys.digest(intent().logical_key))) is None


def test_successful_atomic_batch_and_replay_returns_original_effects(store: StoreBase) -> None:
    mission_record = to_record(mission(), SCOPE)
    audit = to_record(event("synthetic-command"), SCOPE)
    outgoing = to_record(intent(), SCOPE)
    batch = request(
        mission_record,
        command_id="synthetic-command",
        events=(audit,),
        intents=(outgoing,),
        expected=(mission_record.ref, audit.ref, outgoing.ref),
    )
    result = store.commit(batch)
    assert isinstance(result, Accepted) and result.event_ids == (audit.id,)
    replay = store.commit(batch)
    assert isinstance(replay, Duplicate) and replay.original == result
    assert store.list_events(SCOPE)[0] == (audit,)
    changed = CommitRequest.model_validate(
        batch.model_dump()
        | {
            "command": batch.command.model_dump() | {"payload": {"synthetic": "different"}},
        }
    )
    assert isinstance(store.commit(changed), StaleVersion)
    edited = to_record(mission(title="synthetic altered effect"), SCOPE)
    assert isinstance(
        store.commit(
            CommitRequest.model_validate(
                batch.model_dump()
                | {
                    "puts": (edited,),
                    "command": batch.command.model_dump()
                    | {"requested_at": NOW + timedelta(seconds=1)},
                }
            )
        ),
        Duplicate,
    )
    assert store.get_mission(SCOPE, mission_record.id) == mission()


def test_insert_and_missing_expected_versions_are_conditional(store: StoreBase) -> None:
    record = to_record(mission(), SCOPE)
    accepted(store, record)
    assert isinstance(store.commit(request(record)), StaleVersion)
    assert isinstance(store.commit(request(record, expected=())), StaleVersion)
    assert isinstance(store.commit(request(to_record(mission(version=3), SCOPE))), StaleVersion)
    accepted(store, to_record(mission(version=2), SCOPE))
    assert store.get_mission(SCOPE, record.id).version == 2  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "kind",
    [
        "mission",
        "followup",
        "review",
        "patient_profile",
        "audit_event",
        "outbound_intent",
        "inbound_receipt",
    ],
)
def test_cross_doctor_reads_and_claims_disclose_nothing(store: StoreBase, kind: str) -> None:
    models = {
        "mission": mission(),
        "followup": followup(),
        "review": review(),
        "patient_profile": profile(),
        "audit_event": event("synthetic-command"),
        "outbound_intent": intent(),
        "inbound_receipt": inbound(),
    }
    record = to_record(models[kind], SCOPE)
    if kind == "inbound_receipt":
        assert store.accept_inbound(inbound().transport_key, record).status == "created"
    else:
        assert isinstance(store.commit(request(record, command_id="synthetic-command")), Accepted)
    assert store.get(OTHER, kind, record.id) is None
    assert store.get(SCOPE, kind, record.id) == record
    foreign_key = ScopedKey(scope=OTHER, pk=record.pk, sk=record.sk)
    assert store.claim_work(foreign_key, 1, "foreign-worker", NOW, timedelta(minutes=1)) is None
    assert store.get(SCOPE, kind, record.id) == record
    assert store.reconcile_partition(OTHER).examined == 0


def test_cross_scope_commit_and_principal_and_unsupported_epoch_are_forbidden(
    store: StoreBase,
) -> None:
    foreign = to_record(mission(doctor_id=OTHER.doctor_id), OTHER)
    assert isinstance(store.commit(request(foreign)), Forbidden)
    batch = request(to_record(mission(), SCOPE))
    cases: list[dict[str, object]] = [
        {"principal": ACTOR.model_dump() | {"doctor_id": OTHER.doctor_id}},
        {"principal": ACTOR.model_dump() | {"verified_roles": ()}},
        {"expected_auth_epoch": 1},
    ]
    for changed in cases:
        unsafe = CommitRequest.model_validate(
            batch.model_dump()
            | {
                "command": batch.command.model_dump() | changed,
            }
        )
        assert isinstance(store.commit(unsafe), Forbidden)
    assert store.get_mission(SCOPE, "synthetic-mission") is None


def test_paginated_patient_review_and_event_lists_are_scoped_and_stable(store: StoreBase) -> None:
    for i in (2, 0, 1):
        scope = PatientScope(doctor_id=SCOPE.doctor_id, patient_id=f"synthetic-patient-{i}")
        p = to_record(profile(scope, normalized_name="same synthetic name"), scope)
        batch = request(p)
        batch = CommitRequest.model_validate(
            batch.model_dump()
            | {
                "command": batch.command.model_dump() | {"scope": scope},
            }
        )
        assert isinstance(store.commit(batch), Accepted)
        # Domain source key is derived by the domain factory, not a mutable label.
        from domain_fixtures import POLICY, review_payload

        from sanad.domain import create_review

        r = create_review(review_payload(source_id=f"synthetic-source-{i}"), NOW, POLICY).aggregate
        accepted(store, to_record(r, SCOPE))
        audit = to_record(event(f"event-command-{i}", id=f"synthetic-event-{i}"), SCOPE)
        assert isinstance(store.commit(request(audit, command_id=f"event-command-{i}")), Accepted)
    for listing, ids in [
        (store.list_patients, [f"synthetic-patient-{i}" for i in range(3)]),
        (store.list_reviews, None),
    ]:
        cursor = None
        rows: list[StoredRecord] = []
        while True:
            page, cursor = listing(TENANT, cursor, 1)
            rows.extend(page)
            if cursor is None:
                break
        assert len(rows) == 3
        if ids is not None:
            assert [r.id for r in rows] == ids
        assert listing(TenantScope(doctor_id=OTHER.doctor_id))[0] == ()
        assert listing(SCOPE)[0] == ()  # A patient scope cannot widen into a tenant board.
    first, cursor = store.list_events(SCOPE, limit=1)
    assert first[0].id == "synthetic-event-0" and cursor is not None
    assert store.list_events(OTHER, cursor, 1) == ((), None)
    second, _ = store.list_events(SCOPE, cursor, 1)
    assert second[0].id == "synthetic-event-1"


def test_inbound_duplicate_preserves_pending_and_processing_content(store: StoreBase) -> None:
    receipt = inbound()
    record = to_record(receipt, SCOPE)
    first = store.accept_inbound(receipt.transport_key, record)
    assert first.status == "created" and first.state == "pending"
    altered = to_record(inbound(payload_ref="synthetic-replacement"), SCOPE)
    duplicate = store.accept_inbound(receipt.transport_key, altered)
    assert duplicate.status == "existing" and duplicate.record == record
    claim = store.claim_work(record.scoped_key(SCOPE), 1, "worker-one", NOW, timedelta(seconds=30))
    assert claim is not None and claim.generation == 1 and claim.version == 2
    duplicate = store.accept_inbound(receipt.transport_key, altered)
    assert duplicate.state == "processing"
    assert duplicate.record is not None
    assert from_record(duplicate.record, InboundReceipt).payload_ref == receipt.payload_ref
    foreign = to_record(inbound(OTHER), OTHER)
    assert store.accept_inbound(receipt.transport_key, foreign).status == "forbidden"


def test_claim_takeover_and_atomic_receipt_completion_fence(store: StoreBase, clock: Any) -> None:
    receipt = inbound()
    record = to_record(receipt, SCOPE)
    store.accept_inbound(receipt.transport_key, record)
    first = store.claim_work(record.scoped_key(SCOPE), 1, "one", NOW, timedelta(seconds=10))
    assert first is not None
    assert store.claim_work(record.scoped_key(SCOPE), 2, "two", NOW, timedelta(seconds=10)) is None
    clock.now = NOW + timedelta(seconds=10)
    second = store.claim_work(record.scoped_key(SCOPE), 2, "two", clock.now, timedelta(seconds=10))
    assert second is not None and second.generation == 2
    audit = to_record(event("complete-command"), SCOPE)
    stale = request(
        to_record(mission(), SCOPE),
        command_id="complete-command",
        events=(audit,),
        expected=(to_record(mission(), SCOPE).ref, audit.ref),
        receipt_completion=ReceiptCompletion(claim=first, result_event_ids=(audit.id,)),
    )
    assert isinstance(store.commit(stale), StaleVersion)
    assert store.get_mission(SCOPE, "synthetic-mission") is None
    fresh = CommitRequest.model_validate(
        stale.model_dump()
        | {
            "receipt_completion": ReceiptCompletion(claim=second, result_event_ids=(audit.id,)),
        }
    )
    assert isinstance(store.commit(fresh), Accepted)
    saved = store.accept_inbound(receipt.transport_key, record)
    assert saved.state == "completed" and saved.record is not None
    assert saved.record.body["result_event_ids"] == [audit.id]
    assert saved.record.due_sort is None


def test_patient_lease_session_contact_and_stale_release(store: StoreBase, clock: Any) -> None:
    accepted(store, to_record(profile(), SCOPE), to_record(intent(), SCOPE))
    first = store.acquire_patient(SCOPE, "worker-one", NOW, timedelta(seconds=10))
    assert first is not None
    assert store.acquire_patient(SCOPE, "worker-two", NOW, timedelta(seconds=10)) is None
    assert store.acquire_patient(OTHER, "other", NOW, timedelta(seconds=10)) is None
    assert store.get_patient_profile(OTHER) is None
    assert (
        store.reserve_contact(OTHER, "synthetic-slot", "synthetic-intent", first) == "already_taken"
    )
    assert store.reserve_contact(SCOPE, "synthetic-slot", "synthetic-intent", first) == "reserved"
    assert (
        store.reserve_contact(SCOPE, "synthetic-slot", "synthetic-intent", first) == "already_taken"
    )
    snapshot = SessionSnapshot(
        id="synthetic-conversation",
        scope=SCOPE,
        role="concierge",
        blob={"summary": "synthetic memory"},
        safety_epoch=0,
        fence_generation=1,
        session_version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    assert store.commit_session(SCOPE, snapshot.id, 0, first, snapshot=snapshot)
    assert store.load_session(SCOPE, snapshot.id) == snapshot
    assert store.load_session(OTHER, snapshot.id) is None
    assert not store.commit_session(SCOPE, snapshot.id, 0, first, snapshot=snapshot)
    clock.now = NOW + timedelta(seconds=10)
    second = store.acquire_patient(SCOPE, "worker-two", clock.now, timedelta(seconds=10))
    assert second is not None and second.generation == 2
    stale_snapshot = SessionSnapshot.model_validate(
        snapshot.model_dump()
        | {
            "version": 2,
            "session_version": 2,
            "updated_at": clock.now,
        }
    )
    assert not store.commit_session(SCOPE, snapshot.id, 1, first, snapshot=stale_snapshot)
    assert not store.commit_session(OTHER, snapshot.id, 1, second, snapshot=stale_snapshot)
    store.release_patient(first)
    assert store.get_patient_profile(SCOPE).lease_owner == "worker-two"  # type: ignore[union-attr]
    valid_snapshot = SessionSnapshot.model_validate(
        stale_snapshot.model_dump() | {"fence_generation": 2}
    )
    assert store.commit_session(SCOPE, snapshot.id, 1, second, snapshot=valid_snapshot)
    store.release_patient(second)
    third = store.acquire_patient(SCOPE, "worker-three", clock.now, timedelta(seconds=10))
    assert third is not None and third.generation == 3


def test_due_pages_and_stale_index_require_fresh_base_read(
    store: StoreBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.domain import WorkClock

    for i in range(2):
        accepted(
            store,
            to_record(
                mission(
                    id=f"synthetic-{i}",
                    work_clock=WorkClock(next_action_at=NOW, work_lane="mission"),
                ),
                SCOPE,
            ),
        )
    cap = capability()
    first, cursor = store.query_due("mission", "0", NOW, limit=1, capability=cap)
    assert len(first) == 1 and cursor is not None
    second, _ = store.query_due("mission", "0", NOW, cursor, 1, capability=cap)
    assert len(second) == 1 and second[0].id != first[0].id
    assert store.query_due("mission", "0", NOW) == ((), None)
    assert store.query_due("mission", "0", NOW, capability=capability(OTHER))[0] == ()
    stale_items, stale_cursor = store._query(
        "mission#0", index="GSI_DUE", through=keys.instant(NOW) + "#\uffff", limit=1
    )
    original = store.get(SCOPE, "mission", first[0].id)
    assert original is not None
    advanced = mission(
        id=original.id,
        version=2,
        work_clock=WorkClock(next_action_at=NOW + timedelta(days=1), work_lane="mission"),
    )
    accepted(store, to_record(advanced, SCOPE))
    real_query = store._query

    def lagged_query(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("index") == "GSI_DUE":
            return stale_items, stale_cursor
        return real_query(*args, **kwargs)

    monkeypatch.setattr(store, "_query", lagged_query)
    hints, _ = store.query_due("mission", "0", NOW, limit=1, capability=cap)
    assert hints[0].next_action_at == NOW  # The index still claims old work is due.
    fresh = store.get(SCOPE, hints[0].entity_type, hints[0].id)
    assert fresh is not None and fresh.version == 2
    assert from_record(fresh, Mission).work_clock.next_action_at > NOW  # type: ignore[union-attr]
    assert (
        store.claim_work(
            hints[0].record_key, original.version, "stale-worker", NOW, timedelta(minutes=1)
        )
        is None
    )


def test_source_review_uniqueness_and_resolution_visibility(store: StoreBase) -> None:
    from domain_fixtures import POLICY

    from sanad.domain import ResolveReview, ReviewAction, ReviewObligation, transition_review

    original = review()
    first, created = store.create_or_get_review(ReviewCreation(scope=SCOPE, review=original), NOW)
    assert first is not None and created
    same, created = store.create_or_get_review(ReviewCreation(scope=SCOPE, review=original), NOW)
    assert not created and same == first
    assert store.create_or_get_review(ReviewCreation(scope=OTHER, review=original), NOW) == (
        None,
        False,
    )
    assert store.get_review(SCOPE, original.id) == original
    resolved = transition_review(
        original,
        ResolveReview(
            event_id="synthetic-resolution",
            action=ReviewAction.review,
            expected_source_version=original.source_version,
            actor_id=ACTOR.subject,
            reason="synthetic reviewed",
        ),
        NOW,
        POLICY,
    )
    assert hasattr(resolved, "aggregate")
    accepted(store, to_record(resolved.aggregate, SCOPE))
    assert store.list_reviews(TENANT)[0] == ()
    same, created = store.create_or_get_review(ReviewCreation(scope=SCOPE, review=original), NOW)
    assert not created and same is not None
    assert from_record(same, ReviewObligation).state == "resolved"


def test_delivery_start_completion_and_duplicate_provider_result(store: StoreBase) -> None:
    source = to_record(mission(), SCOPE)
    outgoing = to_record(intent(source_versions=(source.ref,)), SCOPE)
    accepted(store, source, outgoing)
    expected = (outgoing.ref, source.ref)
    assert store.start_delivery(outgoing.id, expected, "foreign", NOW, scope=OTHER) is None
    attempt = store.start_delivery(outgoing.id, expected, "dispatcher", NOW, scope=SCOPE)
    assert isinstance(attempt, DeliveryAttempt)
    assert store.start_delivery(outgoing.id, expected, "second", NOW, scope=SCOPE) is None
    assert store.get(OTHER, "delivery_attempt", attempt.id) is None
    assert (
        store.complete_delivery(
            attempt.id, "provider_accepted", "synthetic-provider-id", scope=OTHER
        )
        is None
    )
    completed = store.complete_delivery(
        attempt.id, "provider_accepted", "synthetic-provider-id", scope=SCOPE
    )
    assert completed is not None and completed.body["status"] == "provider_accepted"
    assert completed.due_sort is None
    assert "acknowledged_at" not in completed.body
    assert (
        store.complete_delivery(
            attempt.id, "provider_accepted", "synthetic-provider-id", scope=SCOPE
        )
        == completed
    )
    assert store.complete_delivery(attempt.id, "definite_failure", None, scope=SCOPE) is None


@pytest.mark.parametrize("expire_via", ["restart", "late_completion"])
def test_expired_started_delivery_is_uncertain_never_requeued(
    store: StoreBase, clock: Any, expire_via: str
) -> None:
    outgoing = to_record(intent(), SCOPE)
    accepted(store, outgoing)
    attempt = store.start_delivery(outgoing.id, (outgoing.ref,), "first", NOW, scope=SCOPE)
    assert attempt is not None
    clock.now = NOW + timedelta(seconds=30)
    if expire_via == "restart":
        assert (
            store.start_delivery(outgoing.id, (outgoing.ref,), "second", clock.now, scope=SCOPE)
            is None
        )
    else:
        store.complete_delivery(attempt.id, "provider_accepted", "synthetic-late-id", scope=SCOPE)
    saved = store.get(SCOPE, "outbound_intent", outgoing.id)
    assert saved is not None and saved.body["status"] == "uncertain" and saved.due_sort is not None
    assert saved.body["accepted_at"] is None
    ended = store.get(SCOPE, "delivery_attempt", attempt.id)
    assert ended is not None and ended.body["outcome"] == "uncertain"
    assert store.start_delivery(outgoing.id, (saved.ref,), "third", clock.now, scope=SCOPE) is None


def test_stale_source_suppresses_delivery_and_failure_keeps_clock(store: StoreBase) -> None:
    source = to_record(mission(), SCOPE)
    outgoing = to_record(intent(source_versions=(source.ref,)), SCOPE)
    accepted(store, source, outgoing)
    accepted(store, to_record(mission(version=2), SCOPE))
    assert (
        store.start_delivery(outgoing.id, (outgoing.ref, source.ref), "worker", NOW, scope=SCOPE)
        is None
    )
    saved = store.get(SCOPE, "outbound_intent", outgoing.id)
    assert saved is not None and saved.body["status"] == "suppressed" and saved.due_sort is None
    another = to_record(intent(id="another", logical_key="another", source_versions=()), SCOPE)
    accepted(store, another)
    attempt = store.start_delivery(another.id, (another.ref,), "worker", NOW, scope=SCOPE)
    assert attempt is not None
    failed = store.complete_delivery(attempt.id, "definite_failure", None, scope=SCOPE)
    assert failed is not None and failed.body["status"] == "failed" and failed.due_sort is not None


@pytest.mark.parametrize("reason", ["item_count", "item_bytes", "transaction_bytes"])
def test_oversized_transactions_are_typed_and_never_call_backend(
    store: StoreBase, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if reason == "item_count":
        records = [to_record(mission(id=f"synthetic-{i}"), SCOPE) for i in range(100)]
    elif reason == "item_bytes":
        records = [to_record(mission(title="x" * (410 * 1024)), SCOPE)]
    else:
        records = [
            to_record(mission(id=f"synthetic-{i}", title="x" * (240 * 1024)), SCOPE)
            for i in range(18)
        ]

    def must_not_write(*args: Any, **kwargs: Any) -> bool:
        pytest.fail("oversized transaction reached backend")

    monkeypatch.setattr(store, "_atomic", must_not_write)
    monkeypatch.setattr(store, "_read", must_not_write)
    result = store.commit(request(*records))
    assert isinstance(result, TooLarge) and result.reason == reason


def test_global_marker_collision_is_atomic_and_non_disclosing(store: StoreBase) -> None:
    marker_key = keys.subject("synthetic-bot", "90071992547409931234567890")
    original = to_record(mission(), SCOPE)
    marker = MarkerRecord(
        scope=SCOPE,
        pk=marker_key.pk,
        sk=marker_key.sk,
        target=original.scoped_key(SCOPE),
        created_at=NOW,
    )
    assert isinstance(store.commit(request(original, markers=(marker,))), Accepted)
    second = to_record(mission(id="synthetic-second"), SCOPE)
    collision = MarkerRecord(
        scope=SCOPE,
        pk=marker_key.pk,
        sk=marker_key.sk,
        target=second.scoped_key(SCOPE),
        created_at=NOW,
    )
    assert isinstance(store.commit(request(second, markers=(collision,))), StaleVersion)
    assert store.get_mission(SCOPE, second.id) is None
    assert store.get(SCOPE, "subject", marker_key.pk) is None


def test_outbound_logical_key_collision_rolls_back(store: StoreBase) -> None:
    outgoing = to_record(intent(), SCOPE)
    accepted(store, outgoing)
    collision = to_record(intent(id="synthetic-duplicate"), SCOPE)
    result = store.commit(request(to_record(mission(), SCOPE), collision))
    assert isinstance(result, StaleVersion)
    assert store.get_mission(SCOPE, "synthetic-mission") is None
    assert store.get(SCOPE, "outbound_intent", collision.id) is None


def test_reconcile_repairs_only_projections_and_reports_missing_canonical_clock(
    store: StoreBase,
) -> None:
    original = to_record(mission(), SCOPE)
    accepted(store, original)
    damaged = record_item(original)
    damaged.pop("due_lane_shard")
    damaged["due_sort"] = "synthetic-inconsistent"
    assert store._update(damaged, 1)
    report = store.reconcile_partition(SCOPE, limit=100)
    assert report.inconsistent == report.repaired == (original.key,)
    assert store.get(SCOPE, "mission", original.id) == original
    assert store.reconcile_partition(SCOPE).repaired == ()
    damaged = record_item(original)
    import json

    body = original.body | {"work_clock": None}
    damaged["body"] = json.dumps(body)
    assert store._update(damaged, 1)
    report = store.reconcile_partition(SCOPE)
    assert report.unrepairable == (original.key,) and report.repaired == ()
