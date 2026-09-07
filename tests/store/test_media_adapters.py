import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from providers.fixtures import (
    FakeS3,
    FakeTelegramFiles,
    ScriptedConverter,
    ScriptedModel,
    candidate,
    png,
)
from providers.test_agents import Answer, binding

from sanad.agents.factory import Proposal, ProposalFailure, make_agent
from sanad.agents.sessions import FencedSessionManager
from sanad.media.retrieve import MediaRetriever, StoredMedia
from sanad.media.telegram import MediaFailure
from sanad.safety.models import OutputContext
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.records import MediaWork, from_record, to_record
from store.conftest import Clock
from store.fixtures import OTHER, SCOPE, inbound
from store.processing_fixtures import PATIENT, World


@pytest.mark.parametrize("expired", [False, True])
def test_unassigned_intake_keeps_doctor_scope_without_patient_identity(
    store: StoreBase,
    clock: Clock,
    expired: bool,
) -> None:
    from sanad.store.keys import IntakeScope
    from sanad.store.records import InboundReceipt
    from store.processing_fixtures import DOCTOR

    world = World.create(store, clock)
    scope = IntakeScope(doctor_id=SCOPE.doctor_id, intake_id="synthetic-intake")
    receipt = InboundReceipt.model_validate(
        inbound().model_dump()
        | {
            "scope": scope,
            "principal": DOCTOR,
            "source_subject": DOCTOR.subject,
            "provider_media_handle": "synthetic-handle",
            "kind": "photo",
        }
    )
    assert (
        store.accept_inbound(receipt.transport_key, to_record(receipt, scope)).status == "created"
    )
    retriever = MediaRetriever(
        world.steward,
        FakeS3(),
        FakeTelegramFiles(MediaFailure(reason="expired_handle") if expired else png()),
        ScriptedConverter(),
        scope,
        DOCTOR,
        lambda: True,
        world.policy,
    )
    result = retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt.id)
    if expired:
        assert isinstance(result, MediaFailure) and result.durable
        review = store.get_review(scope, result.review_obligation_id or "")
        assert review is not None and review.patient_id is None
        intent = store.get(scope, "outbound_intent", result.resend_intent_id or "")
        assert intent is not None and intent.patient_id is None
    else:
        assert (
            isinstance(result, StoredMedia)
            and "/intake/synthetic-intake/" in result.source_blob_ref
        )
    assert store.get(SCOPE, "media_work", keys.digest(receipt.id)) is None


def test_s3_outage_is_typed_and_recoverable(
    store: StoreBase, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World.create(store, clock)
    retriever, receipt_id = setup(world)
    s3 = FakeS3()
    retriever.media_store = s3
    original = s3.fake.put_object

    def unavailable(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("private provider URL")

    monkeypatch.setattr(s3.fake, "put_object", unavailable)
    result = retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id)
    assert isinstance(result, MediaFailure) and result.reason == "storage_unavailable"
    assert "private" not in repr(result)
    assert work(world, receipt_id).state == "processing"
    monkeypatch.setattr(s3.fake, "put_object", original)
    clock.now += world.policy.operations.claim_ttl + timedelta(seconds=1)
    assert isinstance(
        retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id), StoredMedia
    )


def setup(world: World, *, failure: MediaFailure | None = None) -> tuple[MediaRetriever, str]:
    receipt = inbound(
        provider_media_handle="synthetic-handle",
        kind="photo",
        principal=PATIENT,
        source_subject=PATIENT.subject,
    )
    assert (
        world.store.accept_inbound(receipt.transport_key, to_record(receipt, SCOPE)).status
        == "created"
    )
    return MediaRetriever(
        world.steward,
        FakeS3(),
        FakeTelegramFiles(failure or png()),
        ScriptedConverter(),
        SCOPE,
        PATIENT,
        lambda: True,
        world.policy,
    ), receipt.id


def work(world: World, receipt_id: str) -> MediaWork:
    row = world.store.get(SCOPE, "media_work", keys.digest(receipt_id))
    assert row is not None
    return from_record(row, MediaWork)


def test_media_fetch_normalize_then_pending_extraction(store: StoreBase, clock: Clock) -> None:
    world = World.create(store, clock)
    retriever, receipt_id = setup(world)
    result = retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id)
    assert isinstance(result, StoredMedia)
    saved = work(world, receipt_id)
    assert saved.state == "pending" and saved.stage == "extract" and saved.work_clock is not None
    assert saved.source_blob_ref != saved.normalized_blob_ref
    assert saved.work_clock.next_action_at > clock()
    assert store.get(OTHER, "media_work", saved.id) is None
    assert not store.list_records(SCOPE, "care_order")[0][0].body.get("model_id")
    calls = len(retriever.telegram.calls)  # type: ignore[attr-defined]
    assert retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id) == result
    assert len(retriever.telegram.calls) == calls  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "boundary",
    [
        "enqueued",
        "claimed_fetch",
        "downloaded",
        "source_stored",
        "committed_normalize",
        "claimed_normalize",
        "normalization_loaded",
        "normalized_stored",
        "committed_extract",
    ],
)
def test_media_recovers_crash_at_each_checkpoint(
    store: StoreBase, clock: Clock, boundary: str
) -> None:
    world = World.create(store, clock)
    retriever, receipt_id = setup(world)

    class Crash(Exception):
        pass

    def checkpoint(name: str) -> None:
        if name == boundary:
            raise Crash()

    retriever.checkpoint = checkpoint
    with pytest.raises(Crash):
        retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id)
    before = work(world, receipt_id)
    assert before.state != "completed"
    retriever.checkpoint = lambda name: None
    clock.now += world.policy.operations.claim_ttl + timedelta(seconds=1)
    result = retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id)
    assert isinstance(result, StoredMedia) and work(world, receipt_id).stage == "extract"
    assert len(store.list_records(SCOPE, "media_work")[0]) == 1


def test_expired_handle_failure_atomic_review_and_resend(store: StoreBase, clock: Clock) -> None:
    world = World.create(store, clock)
    retriever, receipt_id = setup(world, failure=MediaFailure(reason="expired_handle"))
    result = retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id)
    assert isinstance(result, MediaFailure) and result.durable
    saved = work(world, receipt_id)
    assert saved.state == "needs_attention" and saved.work_clock is not None
    assert result.review_obligation_id == saved.review_obligation_id
    review = store.get_review(SCOPE, saved.review_obligation_id or "")
    assert review is not None and review.review_kind.value == "media_failure"
    intent = store.get(SCOPE, "outbound_intent", saved.resend_intent_id or "")
    assert intent is not None and intent.body["status"] == "queued"
    assert retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id) == result
    assert len(store.list_records(SCOPE, "review")[0]) == 1


def test_stale_claim_cannot_commit_download_and_new_worker_recovers(
    store: StoreBase, clock: Clock
) -> None:
    world = World.create(store, clock)
    retriever, receipt_id = setup(world)

    def steal(name: str) -> None:
        if name != "downloaded":
            return
        clock.now += world.policy.operations.claim_ttl + timedelta(seconds=1)
        current = work(world, receipt_id)
        row = to_record(current, SCOPE)
        assert (
            store.claim_work(
                row.scoped_key(SCOPE),
                current.version,
                "replacement",
                clock(),
                world.policy.operations.claim_ttl,
            )
            is not None
        )

    retriever.checkpoint = steal
    result = retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id)
    assert isinstance(result, MediaFailure) and result.reason == "stale_work"
    assert work(world, receipt_id).source_blob_ref is None
    retriever.checkpoint = lambda name: None
    clock.now += world.policy.operations.claim_ttl + timedelta(seconds=1)
    assert isinstance(
        retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id), StoredMedia
    )


def test_retrieval_scope_and_handle_cannot_be_replaced(store: StoreBase, clock: Clock) -> None:
    world = World.create(store, clock)
    retriever, receipt_id = setup(world)
    bad = retriever.fetch_telegram_file("foreign", receipt_id=receipt_id)
    assert isinstance(bad, MediaFailure) and bad.reason == "handle_mismatch"
    retriever.scope = OTHER
    bad = retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id)
    assert isinstance(bad, MediaFailure) and bad.reason == "scope_unavailable"
    assert not store.list_records(SCOPE, "media_work")[0]


def test_scoped_sweep_refetches_expired_claim_and_keeps_review_timed(
    store: StoreBase, clock: Clock
) -> None:
    world = World.create(store, clock)
    retriever, receipt_id = setup(world)

    def crash(name: str) -> None:
        if name == "downloaded":
            raise RuntimeError("synthetic crash")

    retriever.checkpoint = crash
    with pytest.raises(RuntimeError):
        retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id)
    retriever.checkpoint = lambda name: None
    clock.now += world.policy.operations.claim_ttl + timedelta(seconds=1)
    assert retriever.sweep() == 1
    assert work(world, receipt_id).stage == "extract"
    clock.now += world.policy.timing.result_review_interval + timedelta(seconds=1)
    assert retriever.sweep() == 1
    saved = work(world, receipt_id)
    assert saved.state == "needs_attention"
    clock.now += world.policy.timing.result_review_interval + timedelta(seconds=1)
    assert retriever.sweep() == 1
    assert work(world, receipt_id).review_obligation_id == saved.review_obligation_id
    assert len(store.list_records(SCOPE, "review")[0]) == 1


def test_audio_normalization_stores_mp3_and_duration(store: StoreBase, clock: Clock) -> None:
    world = World.create(store, clock)
    retriever, receipt_id = setup(world)
    retriever.telegram = FakeTelegramFiles(b"OggSsynthetic")
    result = retriever.fetch_telegram_file("synthetic-handle", receipt_id=receipt_id)
    assert isinstance(result, StoredMedia) and result.duration == 15
    assert result.normalized_blob_ref != result.source_blob_ref
    assert retriever.media_store.get(SCOPE, result.normalized_blob_ref, 100) == b"ID3synthetic"


def session(world: World) -> FencedSessionManager:
    lease = world.store.acquire_patient(SCOPE, "agent", world.clock(), timedelta(seconds=30))
    assert lease is not None
    return FencedSessionManager(
        world.store,
        SCOPE,
        "concierge:synthetic-bot:epoch-0",
        "concierge",
        lease,
        world.clock,
        safety_epoch=world.profile.safety_epoch,
    )


def test_session_continuity_through_real_agent_and_bounded_snapshot(
    store: StoreBase, clock: Clock
) -> None:
    world = World.create(store, clock)
    manager = session(world)
    first = make_agent(
        "concierge",
        scope=binding(output_context=OutputContext(mode="plan_explanation", language="ar")),
        tools=[],
        system_prompt="test",
        session_key=manager.key,
        session=manager,
        model_factory=lambda registry, selected: ScriptedModel(candidate({"text": "مرحبا"})),
    )
    result = asyncio.run(first.propose(Answer, "synthetic first turn", patient_fields=("text",)))
    assert isinstance(result, Proposal)
    store.release_patient(manager.fence)
    second_manager = session(world)
    second = make_agent(
        "concierge",
        scope=binding(output_context=OutputContext(mode="plan_explanation", language="ar")),
        tools=[],
        system_prompt="test",
        session_key=manager.key,
        session=second_manager,
        model_factory=lambda registry, selected: ScriptedModel(candidate({"text": "أهلاً"})),
    )
    assert second.sdk.messages[0]["content"][0]["text"] == "synthetic first turn"
    assert len(second_manager.turns) == 2
    store.release_patient(second_manager.fence)


def test_session_fence_stolen_mid_model_discards_turn(store: StoreBase, clock: Clock) -> None:
    world = World.create(store, clock)
    manager = session(world)

    def during_call(kwargs: Any) -> Any:
        clock.now += timedelta(seconds=31)
        assert store.acquire_patient(SCOPE, "replacement", clock(), timedelta(seconds=30))
        return candidate({"text": "أهلاً"})

    instance = make_agent(
        "concierge",
        scope=binding(output_context=OutputContext(mode="plan_explanation", language="ar")),
        tools=[],
        system_prompt="test",
        session_key=manager.key,
        session=manager,
        model_factory=lambda registry, selected: ScriptedModel(during_call),
    )
    result = asyncio.run(instance.propose(Answer, "synthetic", patient_fields=("text",)))
    assert isinstance(result, ProposalFailure) and result.reason == "stale_session"
    assert store.load_session(SCOPE, manager.key) is None


def test_urgent_incident_during_model_prevents_session_reassurance(
    store: StoreBase, clock: Clock
) -> None:
    world = World.create(store, clock)
    manager = session(world)
    world.urgent.raise_incident(SCOPE, "synthetic-source", {}, "danger", clock())
    assert not manager.commit("old turn", "old output")
    assert store.load_session(SCOPE, manager.key) is None


def test_doctor_suspension_during_tool_read_discards_proposal(
    store: StoreBase, clock: Clock
) -> None:
    world = World.create(store, clock)
    live = [True]
    context = replace(binding(), authority_check=lambda: live[0])

    def during_call(kwargs: Any) -> Any:
        live[0] = False
        return candidate({"text": "obsolete"})

    instance = make_agent(
        "scribe",
        scope=context,
        tools=[],
        system_prompt="test",
        session_key="test",
        model_factory=lambda registry, selected: ScriptedModel(during_call),
    )
    result = asyncio.run(instance.propose(Answer, "synthetic"))
    assert isinstance(result, ProposalFailure)
    assert result.reason in {"scope_unavailable", "guard_refused"}
    assert world.profile.safety_epoch == 0
