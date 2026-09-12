"""18b checkpoint 1: real browser ingress, durable recovery and adapter semantics."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from harness import FakeClock
from providers.fixtures import (
    FakeS3Client,
    FakeTelegramFiles,
    ScriptedConverter,
    ScriptedVision,
    png,
)

from sanad.channels.telegram.router import route_receipt
from sanad.domain import PatientScope, Principal
from sanad.media.retrieve import MediaRetriever
from sanad.media.source import FileBytes, MediaFailure
from sanad.media.storage import S3MediaStore
from sanad.media.upload import STAGING_TTL, StagedUpload, UploadIngress, patient_source
from sanad.media.vision import VisionAdapter
from sanad.ops.sweep import sweep_due
from sanad.safety import screen_text
from sanad.store._base import StoreBase, Write
from sanad.store.keys import ScopedKey
from sanad.store.memory import MemoryStore
from sanad.store.records import InboundReceipt, UploadStage, WebSession, from_record, to_record
from sanad.web.routes import upload_router
from store import evidence_fixtures as evidence
from store.account_fixtures import PATIENT
from store.concierge_fixtures import PatientWorld
from store.login_fixtures import ORIGIN, browser_login
from store.test_concierge_media import media_message

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


class UploadS3(FakeS3Client):
    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.objects.pop(kwargs["Key"], None)
        return {}


@dataclass
class UploadWorld:
    world: PatientWorld
    ingress: UploadIngress
    s3: UploadS3
    client: TestClient
    session: WebSession

    def headers(self, caption: str = "") -> dict[str, str]:
        return {
            "origin": ORIGIN,
            "sec-fetch-site": "same-origin",
            "x-csrf-token": self.client.cookies["sanad_csrf"],
            "content-type": "image/png",
            "x-upload-caption": base64.b64encode(caption.encode()).decode(),
        }

    def stages(self) -> list[UploadStage]:
        return [from_record(r, UploadStage) for r in self.world.rows("upload_stage")]


def mount(world: PatientWorld, client: TestClient, session: WebSession) -> UploadWorld:
    s3 = UploadS3()
    ingress = UploadIngress(world.runtime, world.login, S3MediaStore("synthetic-private", s3))
    world.app.include_router(upload_router(ingress, world.app.state.web_settings))
    return UploadWorld(world, ingress, s3, client, session)


@pytest.fixture
def uploads(store: StoreBase, clock: FakeClock) -> Iterator[UploadWorld]:
    world = evidence.world(store, clock)
    with world.client() as client:
        assert browser_login(client, world.login_path(PATIENT)).status_code == 303
        session = world.login.session(client.cookies["sanad_session"])
        assert session
        yield mount(world, client, WebSession.model_validate(session.model_dump()))


def test_received_receipt_is_scoped_screened_and_recoverable(uploads: UploadWorld) -> None:
    seen: list[str] = []

    def fail_submit(key: Any) -> None:
        assert uploads.world.store.get(key.scope, "inbound_receipt", key.pk)
        seen.append(key.pk)
        raise RuntimeError("synthetic worker unavailable")

    uploads.ingress.submit = fail_submit
    response = uploads.client.post("/api/patient/uploads", content=png(), headers=uploads.headers())
    assert response.status_code == 202 and response.json()["status"] == "received"
    stage = uploads.stages()[0]
    row = uploads.world.store.get(stage.scope, "inbound_receipt", response.json()["receipt_id"])
    assert row
    receipt = from_record(row, InboundReceipt)
    assert stage.state == "attached" and stage.work_clock is None
    assert receipt.channel == receipt.transport == "browser"
    assert receipt.source_subject == uploads.session.subject
    assert receipt.source_chat == uploads.world.profile.recipient_ref
    assert receipt.scope == uploads.world.patient_scope
    assert receipt.principal == uploads.world.actor(PATIENT)
    assert receipt.safety_screen_state == "screened" and receipt.safety_result
    assert receipt.work_clock and receipt.work_clock.work_lane == "ingress"
    assert seen == [receipt.id]
    assert uploads.world.runtime.counters["upload_handoff_deferred"] == 1
    assert not uploads.world.rows("evidence_head")
    assert len(uploads.s3.objects) == 1
    assert all(w["ServerSideEncryption"] == "AES256" and "ACL" not in w for w in uploads.s3.writes)


@pytest.mark.parametrize(
    "bad",
    [
        "anonymous",
        "doctor",
        "csrf",
        "csrf_cookie",
        "origin",
        "fetch_site",
        "revoked",
        "idle",
        "absolute",
        "epoch",
        "consent",
        "binding",
    ],
)
def test_upload_authority_refused_before_body_or_storage(uploads: UploadWorld, bad: str) -> None:
    headers = uploads.headers()
    world = uploads.world
    if bad == "anonymous":
        uploads.client.cookies.clear()
    elif bad == "doctor":
        assert browser_login(uploads.client, world.login_path(id=299)).status_code == 303
    elif bad == "csrf":
        headers["x-csrf-token"] = "forged"
    elif bad == "csrf_cookie":
        uploads.client.cookies.set("sanad_csrf", "forged")
    elif bad == "origin":
        headers["origin"] = "https://foreign.example"
    elif bad == "fetch_site":
        headers["sec-fetch-site"] = "cross-site"
    elif bad == "revoked":
        world.login.revoke(uploads.session)
    elif bad in {"idle", "absolute"}:
        world.clock.advance(timedelta(minutes=30) if bad == "idle" else timedelta(hours=12))
    elif bad == "epoch":
        from sanad.auth.service import revise

        world.seed(revise(world.doctor, world.clock(), auth_epoch=world.doctor.auth_epoch + 1))
    else:
        from sanad.auth.service import revise
        from sanad.store.records import Consent, PatientBinding

        if bad == "binding":
            row = world.store.get(
                world.patient_scope, "patient_binding", uploads.session.binding_id or ""
            )
            assert row
            world.seed(revise(from_record(row, PatientBinding), world.clock(), status="frozen"))
        else:
            row = world.rows("consent")[0]
            world.seed(revise(from_record(row, Consent), world.clock(), withdrawn_at=world.clock()))

    def body() -> Iterator[bytes]:
        raise AssertionError("unauthorized request body was read")
        yield b""

    response = uploads.client.post("/api/patient/uploads", content=body(), headers=headers)
    assert response.status_code in {401, 403}
    assert not uploads.s3.writes and not uploads.stages()


@pytest.mark.parametrize(
    "case",
    ["mismatch", "oversized", "truncated", "dimension", "pixels", "multipart", "foreign_scope"],
)
def test_image_validation_and_danger_caption_survive_rejection(
    uploads: UploadWorld, case: str
) -> None:
    headers = uploads.headers("I have chest pain and cannot breathe")
    data = png()
    path = "/api/patient/uploads"
    if case == "mismatch":
        headers["content-type"] = "image/jpeg"
    elif case == "oversized":
        data = b"x" * (8 * 1024 * 1024 + 1)
        headers["content-length"] = "1"
    elif case == "truncated":
        data = png()[:35]
    elif case in {"dimension", "pixels"}:
        data = png(8001, 1) if case == "dimension" else png(5000, 4001)
    elif case == "multipart":
        headers["content-type"] = "multipart/form-data; boundary=not-parsed"
    else:
        path += "?patient_id=foreign&doctor_id=foreign"
    uploads.ingress.submit = None
    response = uploads.client.post(path, content=data, headers=headers)
    assert response.status_code in {400, 413}
    assert not uploads.s3.writes and not uploads.stages()
    assert len(uploads.world.rows("incident")) == 1
    intents = uploads.world.rows("outbound_intent")
    assert any(r.body.get("template_id") == "patient_emergency" for r in intents)
    assert any(r.body.get("notification_purpose") == "DANGER" for r in intents)


def test_revocation_during_stream_stores_no_image(uploads: UploadWorld) -> None:
    def body() -> Iterator[bytes]:
        yield png()[:20]
        uploads.world.login.revoke(uploads.session)
        yield png()[20:]

    response = uploads.client.post(
        "/api/patient/uploads", content=body(), headers=uploads.headers()
    )
    assert response.status_code == 403
    assert not uploads.s3.writes and not uploads.stages()


@pytest.mark.parametrize("boundary", ["reserved", "stored", "accepted"])
def test_crash_recovery_attaches_or_disposes_without_orphan_claim(
    uploads: UploadWorld, boundary: str
) -> None:
    def crash(name: str) -> None:
        if name == boundary:
            raise RuntimeError("synthetic crash")

    uploads.ingress.checkpoint = crash
    response = uploads.client.post("/api/patient/uploads", content=png(), headers=uploads.headers())
    assert response.status_code == 503
    stage = uploads.stages()[0]
    uploads.ingress.checkpoint = lambda name: None
    uploads.world.clock.advance(STAGING_TTL)
    sweep_due(uploads.world.runtime, uploads.world.store, upload_handler=uploads.ingress.recover)
    stage = uploads.stages()[0]
    if boundary == "reserved":
        assert stage.state == "discarded" and stage.work_clock
        assert not uploads.s3.objects
        assert uploads.world.store.get(stage.scope, "inbound_receipt", stage.receipt.id) is None
    else:
        assert stage.state == "attached"
        row = uploads.world.store.get(stage.scope, "inbound_receipt", stage.receipt.id)
        assert row
        assert row.body["state"] in {"pending", "completed"}
    assert stage.receipt.processing_claim is None


def test_single_attachment_allows_repeated_fetch_and_refuses_foreign_handles(
    uploads: UploadWorld,
) -> None:
    response = uploads.client.post("/api/patient/uploads", content=png(), headers=uploads.headers())
    assert response.status_code == 202
    stage = uploads.stages()[0]
    source = StagedUpload(
        uploads.world.store, uploads.ingress.storage, stage.scope, stage.subject, stage.receipt.id
    )
    for _ in range(2):
        assert source.fetch("upload:" + stage.id) == FileBytes(data=png())
    assert isinstance(source.fetch("telegram-file"), MediaFailure)
    assert isinstance(
        StagedUpload(
            uploads.world.store, uploads.ingress.storage, stage.scope, "foreign", stage.receipt.id
        ).fetch("upload:" + stage.id),
        MediaFailure,
    )
    assert isinstance(
        StagedUpload(
            uploads.world.store,
            uploads.ingress.storage,
            PatientScope(doctor_id=stage.scope.doctor_id, patient_id="foreign"),
            stage.subject,
            stage.receipt.id,
        ).fetch("upload:" + stage.id),
        MediaFailure,
    )
    assert isinstance(
        StagedUpload(
            uploads.world.store, uploads.ingress.storage, stage.scope, stage.subject, "foreign"
        ).fetch("upload:" + stage.id),
        MediaFailure,
    )
    assert (
        uploads.world.store.accept_inbound(
            stage.receipt.transport_key, to_record(stage.receipt, stage.scope)
        ).status
        == "forbidden"
    )
    assert (
        uploads.world.store.accept_inbound(
            stage.receipt.transport_key, to_record(stage.receipt, stage.scope), upload_id=stage.id
        ).status
        == "existing"
    )
    other = uploads.ingress.receipt(
        uploads.session, "", screen_text("", policy=uploads.world.runtime.safety_policy), "0" * 32
    )
    assert (
        uploads.world.store.accept_inbound(
            other.transport_key, to_record(other, other.scope), upload_id=stage.id
        ).status
        == "conflict"
    )


def test_unclaimed_stage_is_disposed_after_revocation_and_late_writer_is_cleaned(
    uploads: UploadWorld,
) -> None:
    def crash(name: str) -> None:
        if name == "stored":
            raise RuntimeError("crash")

    uploads.ingress.checkpoint = crash
    assert (
        uploads.client.post(
            "/api/patient/uploads", content=png(), headers=uploads.headers()
        ).status_code
        == 503
    )
    stage = uploads.stages()[0]
    uploads.ingress.checkpoint = lambda name: None
    uploads.world.login.revoke(uploads.session)
    uploads.world.clock.advance(STAGING_TTL)
    uploads.ingress.recover(to_record(stage, stage.scope))
    assert uploads.stages()[0].state == "discarded" and not uploads.s3.objects
    uploads.ingress.storage.put_upload(
        stage.scope, stage.id, stage.content_digest, png(), "image/png"
    )
    uploads.world.clock.advance(timedelta(days=1))
    uploads.ingress.recover(to_record(uploads.stages()[0], stage.scope))
    assert not uploads.s3.objects


def test_browser_and_telegram_image_have_equal_business_event_semantics(
    store: StoreBase, clock: FakeClock
) -> None:
    """Two identical stores: explicit receipt/media/evidence ID mapping, no dedupe oracle."""
    from collections import Counter
    from copy import deepcopy

    from sanad.store import keys

    seed = MemoryStore(clock=clock)
    initial = evidence.world(seed, clock)
    evidence.mission(initial)
    with initial.client() as browser:
        assert browser_login(browser, initial.login_path(PATIENT)).status_code == 303
        cookies = dict(browser.cookies)
        session = initial.login.session(cookies["sanad_session"])
        assert session
    # Include the exact same identities, consent, order/mission, receipt history and cookies.
    baseline = deepcopy(seed._items)
    targets = [store, MemoryStore(clock=clock)]
    results: list[dict[str, Any]] = []
    receipt_ids: list[str] = []
    for adapter, target in zip(("browser", "telegram"), targets, strict=True):
        for item in baseline.values():
            assert target._atomic([Write(deepcopy(item), None)], [])
        world = cast(PatientWorld, PatientWorld.create(target, clock))
        world.patient_scope = initial.patient_scope
        world.concierge.synthetic = True
        with world.client() as client:
            client.cookies.update(cookies)
            upload = mount(world, client, WebSession.model_validate(session.model_dump()))
            vision = ScriptedVision(evidence.lab(), evidence.lab())
            telegram = FakeTelegramFiles(png())
            media = cast(S3MediaStore, upload.ingress.storage)

            def factory(
                receipt: InboundReceipt,
                actor: Principal,
                target: StoreBase = target,
                world: PatientWorld = world,
                media: S3MediaStore = media,
                telegram: FakeTelegramFiles = telegram,
            ) -> MediaRetriever:
                binding = target.authorize(world.runtime.settings.bot_id, actor.subject).binding
                assert binding
                return MediaRetriever(
                    world.runtime.steward,
                    media,
                    patient_source(receipt, actor, target, media, telegram),
                    ScriptedConverter(),
                    world.patient_scope,
                    actor,
                    lambda: world.concierge.valid(actor, binding),
                    world.runtime.steward.policy_provider(world.patient_scope),
                )

            world.concierge.media_factory = factory
            world.concierge.vision_factory = lambda source, vision=vision, world=world: (
                VisionAdapter(vision, source, world.runtime.safety_policy)
            )
            before_events = {r.id for r in world.rows("audit_event")}
            before_intents = {r.id for r in world.rows("outbound_intent")}
            if adapter == "browser":

                def submit(key: ScopedKey, world: PatientWorld = world) -> None:
                    route_receipt(world.runtime, key)

                upload.ingress.submit = submit
                response = client.post(
                    "/api/patient/uploads", content=png(), headers=upload.headers()
                )
                assert response.status_code == 202
                stage = upload.stages()[0]
                receipt = stage.receipt
            else:
                assert world.post(media_message("photo", 1100)).status_code == 200
                receipt = world.receipt(1100)
            receipt_ids.append(receipt.id)
            assert not vision.calls, "HTTP ingress must not call the readers"
            assert len(world.rows("media_work")) == 1
            sweep_due(world.runtime, target, upload_handler=upload.ingress.recover)
            assert len(vision.calls) == 2
            assert len(telegram.calls) == (0 if adapter == "browser" else 1)
            accepted = evidence.current(world)
            assert accepted.association_state == "accepted"
            assert world.rows("mission")[0].body["state"] == "fulfilled"
            assert evidence.work(world).state == "completed"
            assert (
                media.get(
                    world.patient_scope, evidence.work(world).source_blob_ref or "", 8 * 1024 * 1024
                )
                == png()
            )
            # Generated IDs map to semantic roles; provenance keeps the true adapter receipt.
            mapping = {
                receipt.id: "OBSERVATION",
                keys.digest(receipt.id): "MEDIA",
                keys.digest("evidence:" + receipt.id): "EVIDENCE",
            }
            assert accepted.observation_id == receipt.id

            def mapped(value: Any, mapping: dict[str, str] = mapping) -> Any:
                if isinstance(value, str):
                    for original, label in sorted(mapping.items(), key=lambda pair: -len(pair[0])):
                        value = value.replace(original, label)
                    return value
                if isinstance(value, list | tuple):
                    return [mapped(x) for x in value]
                if isinstance(value, dict):
                    return {k: mapped(v) for k, v in value.items()}
                return value

            events: list[dict[str, Any]] = [
                dict(r.body) for r in world.rows("audit_event") if r.id not in before_events
            ]
            reviews = [
                r.body for r in world.rows("review") if r.body["review_kind"] == "result_review"
            ]
            notices = [r.body for r in world.rows("outbound_intent") if r.id not in before_intents]
            mission = world.rows("mission")[0].body
            results.append(
                mapped(
                    {
                        "events": Counter(str(e["event_type"]) for e in events),
                        "event_effects": sorted(
                            [
                                (
                                    e["event_type"],
                                    e["actor"]["actor_kind"],
                                    sorted(
                                        (r["entity_type"], r["version"])
                                        for r in e["aggregate_refs"]
                                    ),
                                )
                                for e in events
                            ]
                        ),
                        "evidence": accepted.model_dump(
                            mode="json",
                            include={
                                "category",
                                "printed_date",
                                "association_state",
                                "extracted_values",
                                "flags",
                                "required_predicate_results",
                                "observation_id",
                                "mission_id",
                                "provenance",
                            },
                        ),
                        "readers": [
                            r.model_dump(mode="json", exclude={"metadata"})
                            for r in accepted.readers
                        ],
                        "mission": {
                            k: mission[k]
                            for k in (
                                "state",
                                "fulfillment_validity",
                                "fulfilled_at",
                                "objective_received_at",
                                "timeliness",
                                "version",
                                "work_clock",
                            )
                        },
                        "reviews": [
                            {
                                k: r[k]
                                for k in (
                                    "review_kind",
                                    "state",
                                    "source_type",
                                    "source_version",
                                    "review_at",
                                    "work_clock",
                                )
                            }
                            for r in reviews
                        ],
                        "notices": sorted(
                            [
                                {
                                    k: r.get(k)
                                    for k in (
                                        "audience",
                                        "notification_purpose",
                                        "template_id",
                                        "payload",
                                        "state",
                                        "recipient_ref",
                                    )
                                }
                                for r in notices
                            ],
                            key=lambda n: str(n["template_id"]),
                        ),
                    }
                )
            )
            sweep_due(world.runtime, target, upload_handler=upload.ingress.recover)
            assert len(vision.calls) == 2 and len(world.rows("evidence_head")) == 1
    assert receipt_ids[0] != receipt_ids[1]
    assert results[0] == results[1]
    assert results[0]["reviews"] and results[0]["events"]


def test_staged_source_survives_media_fetch_retry(uploads: UploadWorld) -> None:
    from sanad.store.records import MediaWork

    assert (
        uploads.client.post(
            "/api/patient/uploads", content=png(), headers=uploads.headers()
        ).status_code
        == 202
    )
    stage = uploads.stages()[0]
    source = StagedUpload(
        uploads.world.store, uploads.ingress.storage, stage.scope, stage.subject, stage.receipt.id
    )
    world = uploads.world
    binding = world.store.authorize(world.runtime.settings.bot_id, stage.subject).binding
    assert binding
    actor = world.actor(PATIENT)

    def crash(name: str) -> None:
        if name == "downloaded":
            raise RuntimeError("synthetic crash after fetch")

    retriever = MediaRetriever(
        world.runtime.steward,
        cast(S3MediaStore, uploads.ingress.storage),
        source,
        ScriptedConverter(),
        stage.scope,
        actor,
        lambda: world.concierge.valid(actor, binding),
        world.runtime.steward.policy_provider(stage.scope),
        checkpoint=crash,
    )
    with pytest.raises(RuntimeError):
        retriever.fetch_media("upload:" + stage.id, receipt_id=stage.receipt.id)
    work = from_record(world.rows("media_work")[0], MediaWork)
    assert work.processing_claim
    world.clock.advance(STAGING_TTL)
    retriever.checkpoint = lambda name: None
    result = retriever.fetch_media("upload:" + stage.id, receipt_id=stage.receipt.id)
    assert not isinstance(result, MediaFailure)
    assert result.size == len(png())


def test_reserved_then_revoked_before_s3_stores_no_byte(uploads: UploadWorld) -> None:
    def revoke(name: str) -> None:
        if name == "reserved":
            uploads.world.login.revoke(uploads.session)

    uploads.ingress.checkpoint = revoke
    result = uploads.client.post("/api/patient/uploads", content=png(), headers=uploads.headers())
    assert result.status_code == 403
    assert not uploads.s3.objects
    assert uploads.stages()[0].state == "reserved"


def test_disconnect_keeps_danger_before_ordinary_lock(uploads: UploadWorld) -> None:
    from starlette.requests import ClientDisconnect

    world = uploads.world
    lease = world.store.acquire_patient(
        world.patient_scope, "slow-ordinary-turn", world.clock(), timedelta(minutes=5)
    )
    assert lease

    def body() -> Iterator[bytes]:
        yield b"partial"
        raise ClientDisconnect()

    response = uploads.client.post(
        "/api/patient/uploads",
        content=body(),
        headers=uploads.headers("I have chest pain and cannot breathe"),
    )
    assert response.status_code == 400
    assert len(world.rows("incident")) == 1
    assert not uploads.s3.objects
    world.store.release_patient(lease)


def test_two_attachment_workers_have_one_receipt(uploads: UploadWorld) -> None:
    from concurrent.futures import ThreadPoolExecutor

    def crash(name: str) -> None:
        if name == "stored":
            raise RuntimeError("synthetic crash")

    uploads.ingress.checkpoint = crash
    assert (
        uploads.client.post(
            "/api/patient/uploads", content=png(), headers=uploads.headers()
        ).status_code
        == 503
    )
    stage = uploads.stages()[0]

    def attach() -> str:
        return uploads.world.store.accept_inbound(
            stage.receipt.transport_key, to_record(stage.receipt, stage.scope), upload_id=stage.id
        ).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attach(), range(2)))
    assert results.count("created") == 1
    assert set(results) <= {"created", "existing", "conflict"}
    assert uploads.stages()[0].state == "attached"
    assert attach() == "existing"


def test_adapter_composition_does_not_move_into_domain_processing() -> None:
    import ast
    from pathlib import Path

    roots = [Path("src/sanad") / name for name in ("evidence", "concierge", "monitor", "safety")]
    for root in roots:
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text())
            assert not any(
                isinstance(node, ast.Attribute)
                and node.attr in {"channel", "transport"}
                and isinstance(node.value, ast.Name)
                and node.value.id == "receipt"
                for node in ast.walk(tree)
            ), str(path)
    tree = ast.parse(Path("src/sanad/media/retrieve.py").read_text())
    assert not any(
        isinstance(node, ast.Attribute) and node.attr in {"channel", "transport"}
        for node in ast.walk(tree)
    )
    # Known residuals are asserted explicitly, not concealed by the narrower guarantee.
    assert 'receipt.transport == "telegram"' in Path("src/sanad/steward/inbound.py").read_text()
    assert 'model.transport == "telegram"' in Path("src/sanad/store/_base.py").read_text()


def test_app_injects_upload_storage_and_saved_key_submission(uploads: UploadWorld) -> None:
    from sanad.api.app import create_app

    saved: list[ScopedKey] = []
    world = uploads.world
    app = create_app(
        store=world.store,
        clock=world.clock,
        telegram_settings=world.runtime.settings,
        transport=world.transport,
        web_settings=world.app.state.web_settings,
        upload_storage=uploads.ingress.storage,
        consent_policy=world.claims.consent_policy,
        receipt_submit=saved.append,
    )
    assert app.state.uploads.storage is uploads.ingress.storage
    with type(uploads.client)(app, base_url=ORIGIN) as client:
        client.cookies.update(dict(uploads.client.cookies))
        response = client.post("/api/patient/uploads", content=png(), headers=uploads.headers())
        assert response.status_code == 202
        assert len(saved) == 1
        row = world.store.get(saved[0].scope, "inbound_receipt", saved[0].pk)
        assert row and row.body["state"] == "pending"


def test_recovery_preserves_danger_when_no_s3_body_was_written(uploads: UploadWorld) -> None:
    def crash(name: str) -> None:
        if name == "reserved":
            raise RuntimeError("synthetic crash")

    uploads.ingress.checkpoint = crash
    assert (
        uploads.client.post(
            "/api/patient/uploads",
            content=png(),
            headers=uploads.headers("I have chest pain and cannot breathe"),
        ).status_code
        == 503
    )
    uploads.world.clock.advance(STAGING_TTL)
    stage = uploads.stages()[0]
    uploads.ingress.recover(to_record(stage, stage.scope))
    assert uploads.stages()[0].state == "discarded"
    assert len(uploads.world.rows("incident")) == 1
    assert not uploads.s3.objects


def test_stage_metadata_cannot_be_rewritten_through_a_domain_command(uploads: UploadWorld) -> None:
    from sanad.steward.service import system_command
    from sanad.store.records import CommitRequest

    assert (
        uploads.client.post(
            "/api/patient/uploads", content=png(), headers=uploads.headers()
        ).status_code
        == 202
    )
    stage = uploads.stages()[0]
    command = system_command(stage.scope, "forged-stage", {}, uploads.world.clock(), lane="media")
    result = uploads.world.store.commit(
        CommitRequest(command=command, puts=(to_record(stage, stage.scope),))
    )
    assert result.status == "forbidden"


def test_telegram_source_refuses_a_staged_handle_without_transport_io(uploads: UploadWorld) -> None:
    import httpx

    from sanad.channels.telegram.transport import TelegramTransport
    from sanad.media.telegram import TelegramFileClient

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("staged handle reached Telegram")

    with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
        source = TelegramFileClient(TelegramTransport(uploads.world.runtime.settings, client))
        assert source.fetch("upload:" + "0" * 32) == MediaFailure(reason="invalid_handle")


def test_lambda_composition_selects_staged_source_without_provider_calls(
    uploads: UploadWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from sanad.api import lambda_entry
    from sanad.media.telegram import TelegramFileClient

    world = uploads.world
    assert (
        uploads.client.post(
            "/api/patient/uploads", content=png(), headers=uploads.headers()
        ).status_code
        == 202
    )
    stage = uploads.stages()[0]
    values = {
        "gemini_api_key": "synthetic-not-a-credential",
        "bot-token": "4242:synthetic-token-value",
        "webhook-secret": "synthetic-webhook-secret",
        "tick-secret": "synthetic-tick",
        "admin-telegram-id": "10001",
        "public-base-url": ORIGIN,
        "bot-username": "synthetic_bot",
    }
    calls: list[str] = []

    def client(service: str, **kwargs: Any) -> Any:
        calls.append(service)
        if service == "ssm":
            return SimpleNamespace(
                get_parameters=lambda **kw: {
                    "Parameters": [
                        {"Name": "/synthetic/" + k, "Value": v} for k, v in values.items()
                    ]
                }
            )
        if service == "s3":
            return uploads.s3
        if service in {"dynamodb", "lambda"}:
            return SimpleNamespace()
        raise AssertionError("unexpected provider client")

    for name, value in {
        "SANAD_SSM_PREFIX": "/synthetic/",
        "SANAD_TABLE": "synthetic",
        "SANAD_ENV": "test",
        "SANAD_BUCKET": "synthetic-private",
        "AWS_LAMBDA_FUNCTION_NAME": "synthetic",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("boto3.client", client)
    monkeypatch.setattr(lambda_entry, "DynamoStore", lambda *args: world.store)
    monkeypatch.setattr(lambda_entry, "utc_now", world.clock)
    app = lambda_entry.configure("synthetic")
    try:
        retriever = app.state.concierge.media_factory(stage.receipt, world.actor(PATIENT))
        assert isinstance(retriever.source, StagedUpload)
        assert retriever.source.fetch("upload:" + stage.id) == FileBytes(data=png())
        assert app.state.uploads.storage is app.state.media_store
        telegram = world.receipt(200)  # Existing patient's Telegram /login receipt.
        assert isinstance(
            app.state.concierge.media_factory(telegram, world.actor(PATIENT)).source,
            TelegramFileClient,
        )
        assert set(calls) == {"ssm", "dynamodb", "lambda", "s3"}
    finally:
        app.state.telegram.transport.http.close()
        app.state.scribe.rxnorm_client.close()


def test_image_receipt_time_is_when_the_complete_body_arrives(uploads: UploadWorld) -> None:
    started = uploads.world.clock()

    def body() -> Iterator[bytes]:
        yield png()[:20]
        uploads.world.clock.advance(timedelta(seconds=10))
        yield png()[20:]

    response = uploads.client.post(
        "/api/patient/uploads", content=body(), headers=uploads.headers()
    )
    assert response.status_code == 202
    receipt = uploads.stages()[0].receipt
    assert receipt.received_at == started + timedelta(seconds=10)
    assert receipt.work_clock and receipt.work_clock.next_action_at == receipt.received_at
