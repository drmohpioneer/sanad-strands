"""Crashes across complete application instances, using only retained durable state."""

from datetime import timedelta
from typing import Any, cast

import pytest
from harness import FakeClock, SimulatedCrash, crash_after
from providers.fixtures import (
    FakeTelegramFiles,
    ScriptedConverter,
    ScriptedModel,
    ScriptedVision,
    png,
)
from store import evidence_fixtures as evidence
from store.account_fixtures import update
from store.contact_fixtures import required
from store.test_browser_upload import UploadWorld
from store.test_browser_upload import uploads as uploads

from sanad.contact import question_digest
from sanad.domain import Principal
from sanad.media.retrieve import MediaRetriever
from sanad.media.storage import S3MediaStore
from sanad.media.upload import STAGING_TTL, UploadIngress, patient_source
from sanad.media.vision import VisionAdapter
from sanad.ops.sweep import sweep_due
from sanad.store._base import StoreBase
from sanad.store.records import Evidence, InboundReceipt, MediaWork, from_record
from system.subjects import DOCTOR_A, PATIENT_A, SubjectWorld
from system.test_journey import assert_kinds, mission_kind, tick


def restart(w: SubjectWorld) -> SubjectWorld:
    return SubjectWorld.for_subjects(
        w.store, w.clock, doctor=w.doctor_subject, patient=w.patient_subject, scope=w.patient_scope
    )


def setup(store: StoreBase, clock: FakeClock) -> SubjectWorld:
    w = SubjectWorld.for_subjects(store, clock, doctor=DOCTOR_A, patient=PATIENT_A)
    w.approve(DOCTOR_A, language="en")
    w.enroll_named("Synthetic Recovery Patient", id=100)
    return w


@pytest.mark.parametrize("boundary", ["accept_inbound", "claim_work", "commit", "before_commit"])
def test_t11_t35_receipt_and_question_survive_fresh_runtime(
    store: StoreBase,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    w = setup(store, clock)
    body = update(PATIENT_A, "talk to the doctor about my appointment", 1000)
    if boundary == "before_commit":
        original = store.commit

        def before(request: Any) -> Any:
            if any(r.entity_type == "mission" for r in request.puts):
                raise SimulatedCrash("before domain commit")
            return original(request)

        with monkeypatch.context() as patch:
            patch.setattr(store, "commit", before)
            with pytest.raises(SimulatedCrash):
                w.post(body)
        assert not w.rows("mission")
    else:
        with crash_after(store, boundary), pytest.raises(SimulatedCrash):
            w.post(body)
    assert w.receipt(1000).payload is not None
    clock.advance(timedelta(minutes=11))
    recovered = restart(w)
    tick(recovered)
    assert recovered.receipt(1000).state == "completed"
    assert_kinds(recovered, "QUESTION")
    question = mission_kind(recovered, "QUESTION")
    assert question.state == "open"
    assert (
        len([r for r in recovered.rows("review") if r.body["review_kind"] == "question_answer"])
        == 1
    )
    saved = {kind: recovered.rows(kind) for kind in ("mission", "review", "audit_event")}
    assert recovered.post(body).status_code == 200
    tick(recovered)
    assert saved == {kind: recovered.rows(kind) for kind in saved}
    replies = [
        r
        for r in recovered.rows("outbound_intent")
        if r.body.get("template_id") == "patient_question_forwarded"
    ]
    assert len(replies) == 1 and replies[0].body["status"] == "provider_accepted"


def test_digest_restart_between_arm_and_fire_preserves_single_notice(
    store: StoreBase, clock: FakeClock
) -> None:
    w = setup(store, clock)
    w.send("talk to the doctor about my appointment", id=1000)
    q = mission_kind(w, "QUESTION")
    clock.now = q.due_at
    tick(w)
    schedule = required(question_digest.load(store, w.doctor.scope))
    at = required(schedule.next_action_at)
    recovered = restart(w)
    clock.now = at - timedelta(seconds=1)
    tick(recovered)
    assert not recovered.transport.calls
    clock.now = at
    tick(recovered)
    assert len(recovered.transport.calls) == 1
    payload = str(recovered.transport.calls[0].payload)
    assert "Synthetic Recovery Patient" in payload and "/answer 1" in payload
    row = question_digest.due(store, recovered.doctor.scope)[0][2]
    assert row.first_notice_at == at and row.state == "open"
    again = restart(recovered)
    tick(again)
    assert not again.transport.calls
    assert required(required(question_digest.load(store, again.doctor.scope)).next_action_at) > at


@pytest.mark.parametrize("boundary", ["reserved", "stored", "accepted"])
def test_t35_upload_recovery_uses_new_ingress_and_retained_storage(
    uploads: UploadWorld, boundary: str
) -> None:
    def crash(name: str) -> None:
        if name == boundary:
            raise RuntimeError("Synthetic upload crash")

    expected_mission = evidence.mission(uploads.world, order_refs=())
    vision = ScriptedVision(evidence.lab(), evidence.lab())
    storage = cast(S3MediaStore, uploads.ingress.storage)
    files = FakeTelegramFiles(png())

    def media(receipt: InboundReceipt, actor: Principal) -> MediaRetriever:
        w = uploads.world
        binding = required(w.store.authorize(w.runtime.settings.bot_id, actor.subject).binding)
        return MediaRetriever(
            w.runtime.steward,
            storage,
            patient_source(receipt, actor, w.store, storage, files),
            ScriptedConverter(),
            w.patient_scope,
            actor,
            lambda: w.concierge.valid(actor, binding),
            w.runtime.steward.policy_provider(w.patient_scope),
        )

    uploads.world.concierge.media_factory = media
    uploads.world.concierge.vision_factory = lambda source: VisionAdapter(
        vision, source, uploads.world.runtime.safety_policy
    )
    uploads.ingress.checkpoint = crash
    assert (
        uploads.client.post(
            "/api/patient/uploads", content=png(), headers=uploads.headers()
        ).status_code
        == 409
    )
    stage = uploads.stages()[0]
    # New service instance: neither callbacks nor process memory survive.
    fresh = UploadIngress(uploads.world.runtime, uploads.world.login, uploads.ingress.storage)
    uploads.world.clock.advance(STAGING_TTL)
    result = sweep_due(
        uploads.world.runtime,
        uploads.world.store,
        upload_handler=fresh.recover,
        elapsed_clock=lambda: 0,
    )
    assert not result["errors"]
    current = uploads.stages()[0]
    if boundary == "reserved":
        assert current.state == "discarded" and not uploads.s3.objects
        assert uploads.world.store.get(stage.scope, "inbound_receipt", stage.receipt.id) is None
    else:
        assert current.state == "attached"
        row = required(uploads.world.store.get(stage.scope, "inbound_receipt", stage.receipt.id))
        receipt = from_record(row, InboundReceipt)
        assert receipt.source_subject == uploads.session.subject
        if receipt.state != "completed":
            recovery = required(receipt.work_clock)
            assert recovery.work_lane == "ingress"
            uploads.world.clock.now = max(uploads.world.clock(), recovery.next_action_at)
        # The staging lane runs after ingress; a new sweep must consume its saved receipt.
        for _ in range(2):
            outcome = sweep_due(
                uploads.world.runtime,
                uploads.world.store,
                upload_handler=fresh.recover,
                elapsed_clock=lambda: 0,
            )
            assert not outcome["errors"]
        final = from_record(
            required(uploads.world.store.get(stage.scope, "inbound_receipt", stage.receipt.id)),
            InboundReceipt,
        )
        assert final.state == "completed" and final.work_clock is None
        work = from_record(uploads.world.rows("media_work")[0], MediaWork)
        assert work.state == "completed" and work.work_clock is None
        accepted = [
            from_record(r, Evidence)
            for r in uploads.world.rows("evidence")
            if r.body["association_state"] == "accepted"
        ]
        assert len(accepted) == 1
        assert accepted[0].observation_id == stage.receipt.id
        assert accepted[0].mission_id == expected_mission.id
        mission = required(uploads.world.store.get_mission(stage.scope, expected_mission.id))
        assert mission.state == "fulfilled" and mission.due_at == expected_mission.due_at
        assert len(vision.calls) == 2 and not files.calls
        saved = {kind: uploads.world.rows(kind) for kind in ("mission", "evidence", "review")}
        fresh.recover(to_record_stage(current))
        sweep_due(
            uploads.world.runtime,
            uploads.world.store,
            upload_handler=fresh.recover,
            elapsed_clock=lambda: 0,
        )
        assert saved == {kind: uploads.world.rows(kind) for kind in saved}
        assert len(vision.calls) == 2


def to_record_stage(stage: Any) -> Any:
    from sanad.store.records import to_record

    return to_record(stage, stage.scope)


def test_t53_provider_timeout_keeps_ticket_and_urgent_route_on_restart(
    store: StoreBase,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = setup(store, clock)
    model = ScriptedModel(TimeoutError())
    w.concierge.model_factory = lambda registry, role: model
    assert (
        w.post(
            update(PATIENT_A, "What should I do about an unsupported condition?", 1000)
        ).status_code
        == 200
    )
    assert w.receipt(1000).state == "completed"
    assert_kinds(w, "QUESTION")
    assert len(model.script.calls) == 1
    assert any(
        r.body.get("template_id") == "patient_safe_fallback" for r in w.rows("outbound_intent")
    )
    recovered = restart(w)
    assert (
        recovered.post(update(PATIENT_A, "I have chest pain and cannot breathe", 1001)).status_code
        == 200
    )
    assert len(recovered.rows("incident")) == 1
    assert any(
        r.body["notification_purpose"] == "patient_safety_response"
        and r.body["status"] == "provider_accepted"
        for r in recovered.rows("outbound_intent")
    )


def test_t43_extension_after_due_query_and_paginated_recovery(
    store: StoreBase,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain_fixtures import mission
    from store.fixtures import SCOPE
    from store.processing_fixtures import World

    from sanad.domain import Mission, WorkClock
    from sanad.steward.types import records
    from sanad.store.records import from_record, to_record

    w = World.create(store, clock)
    for index in range(4):
        w.put(
            mission(
                id=f"paged-{index}",
                due_at=clock(),
                escalation_at=clock(),
                work_clock=WorkClock(next_action_at=clock(), work_lane="mission"),
            )
        )
    queried = store.query_due
    injected = False
    expected_extended: Mission | None = None
    deferred_until = clock() + timedelta(days=60)

    def extend_after_query(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected, expected_extended
        result = queried(*args, **kwargs)
        if not injected and result[0]:
            injected = True
            # A separately confirmed current record wins over the already fetched index hint.
            old = required(store.get_mission(SCOPE, result[0][0].id))
            expected_extended = old.model_copy(
                update={
                    "version": old.version + 1,
                    "due_at": deferred_until,
                    "escalation_at": deferred_until,
                    "updated_at": clock(),
                    "deadline_generation": old.deadline_generation + 1,
                    "work_clock": WorkClock(next_action_at=deferred_until, work_lane="mission"),
                }
            )
            w.put(expected_extended)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(store, "query_due", extend_after_query)
        first = w.run_sweep("mission", max_items=2, page_size=1)
    assert injected and first.budget_exhausted
    # New sweep objects, bounded pages, same durable store.
    from sanad.steward.sweep import Sweeper

    recovered = Sweeper(w.steward, w.inbound, w.dispatcher, w.sweep.capability)
    w.sweep = recovered
    w.run_sweep("mission", page_size=1)
    current = [from_record(r, Mission) for r in records(store, SCOPE, "mission")]
    assert sum(m.state == "overdue" for m in current) == 3
    extended = next(m for m in current if m.due_at == deferred_until)
    assert extended.state == "open"
    assert expected_extended is not None
    assert store.get(SCOPE, "mission", expected_extended.id) == to_record(expected_extended, SCOPE)
    before = list(records(store, SCOPE, "outbound_intent"))
    w.run_sweep("mission", page_size=1)
    assert list(records(store, SCOPE, "outbound_intent")) == before
