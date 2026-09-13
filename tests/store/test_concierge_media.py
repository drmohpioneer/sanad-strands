"""Real receipt/media/SpeechAdapter recovery with private synthetic bytes."""

from datetime import timedelta
from typing import cast

import pytest
from harness import FakeClock
from providers.fixtures import (
    FakeS3,
    FakeTelegramFiles,
    ScriptedConverter,
    ScriptedModel,
    ScriptedSpeech,
    png,
)
from pydantic import JsonValue

from sanad.domain import Principal
from sanad.media.retrieve import MediaRetriever
from sanad.media.speech import SpeechAdapter
from sanad.media.telegram import MediaFailure
from sanad.models.io import ModelUnavailable
from sanad.ops.sweep import sweep_due
from sanad.scribe.records import ClinicalFact
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.records import InboundReceipt, MediaWork, from_record
from store.account_fixtures import PATIENT, update
from store.concierge_fixtures import PatientWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> PatientWorld:
    w = cast(PatientWorld, PatientWorld.create(store, clock))
    w.enroll()
    return w


def media_message(kind: str = "voice", id: int = 1100, caption: str = "") -> dict[str, JsonValue]:
    body = update(PATIENT, "", id)
    message = body["message"]
    assert isinstance(message, dict)
    message.pop("text")
    file: dict[str, JsonValue] = {"file_id": "synthetic-media", "file_unique_id": "synthetic"}
    message[kind] = [file] if kind == "photo" else file
    if caption:
        message["caption"] = caption
    return body


def providers(
    world: PatientWorld,
    reply: str | ModelUnavailable,
    download: bytes | MediaFailure = b"OggSsynthetic",
) -> tuple[ScriptedSpeech, FakeTelegramFiles, FakeS3]:
    speech, files, s3 = ScriptedSpeech(reply), FakeTelegramFiles(download), FakeS3()
    converter = ScriptedConverter()

    def media(receipt: InboundReceipt, actor: Principal) -> MediaRetriever:
        auth = world.store.authorize(world.runtime.settings.bot_id, actor.subject)
        assert auth.binding
        binding = auth.binding
        return MediaRetriever(
            world.runtime.steward,
            s3,
            files,
            converter,
            world.patient_scope,
            actor,
            lambda: world.concierge.valid(actor, binding),
            world.runtime.steward.policy_provider(world.patient_scope),
        )

    world.concierge.media_factory = media
    world.concierge.speech_factory = lambda source: SpeechAdapter(speech, converter, source)
    world.concierge.model_factory = lambda registry, role: ScriptedModel()
    return speech, files, s3


def work(world: PatientWorld, id: int = 1100) -> MediaWork:
    receipt = world.receipt(id)
    row = world.store.get(world.patient_scope, "media_work", keys.digest(receipt.id))
    assert row
    return from_record(row, MediaWork)


def test_voice_start_via_real_adapter(world: PatientWorld) -> None:
    speech, files, s3 = providers(world, "بدأت الدوا\nNUMBERS: none")
    world.post(media_message())
    assert world.receipt(1100).state == "completed"
    assert len(speech.calls) == 1 and len(files.calls) == 1
    assert any(r.body["state"] == "fulfilled" for r in world.rows("mission"))
    saved = work(world)
    assert saved.state == "completed" and saved.transcript_ref and saved.association_ref
    fact = next(r for r in world.rows("clinical_fact") if r.body["category"] == "patient_report")
    provenance = from_record(fact, ClinicalFact).provenance
    assert provenance.source_span
    assert provenance.prompt_version == "egyptian-verbatim-numbers-v4"
    assert any(i.template_id == "patient_start_recorded" for i in world.patient_intents())


@pytest.mark.parametrize("failure", ["expired", "speech"])
def test_voice_failure_one_resend_and_review(world: PatientWorld, failure: str) -> None:
    speech, files, _ = providers(
        world,
        ModelUnavailable(reason="unavailable"),
        MediaFailure(reason="expired_handle") if failure == "expired" else b"OggSsynthetic",
    )
    world.post(media_message())
    assert world.receipt(1100).state == "completed"
    saved = work(world)
    assert saved.state == "needs_attention" and saved.review_obligation_id
    assert (
        len([i for i in world.patient_intents() if i.template_id == "patient_voice_unreadable"])
        == 1
    )
    assert len(speech.calls) == int(failure == "speech") and len(files.calls) == 1
    world.clock.advance(timedelta(days=4))
    sweep_due(world.runtime, world.store)
    assert (
        len([i for i in world.patient_intents() if i.template_id == "patient_voice_unreadable"])
        == 1
    )


def test_voice_transcript_checkpoint_replay_no_second_transcription(world: PatientWorld) -> None:
    speech, files, _ = providers(world, "بدأت الدوا\nNUMBERS: none")

    def crash(stage: str) -> None:
        if stage == "transcript_persisted":
            raise RuntimeError("synthetic crash")

    world.concierge.checkpoint = crash
    world.post(media_message())
    assert world.receipt(1100).state == "pending" and work(world).transcript_ref
    world.concierge.checkpoint = lambda stage: None
    world.clock.advance(timedelta(minutes=11))
    sweep_due(world.runtime, world.store)
    assert world.receipt(1100).state == "completed" and work(world).state == "completed"
    assert len(speech.calls) == 1 and len(files.calls) == 1
    assert (
        len([i for i in world.patient_intents() if i.template_id == "patient_start_recorded"]) == 1
    )


def test_voice_disputed_numbers_do_not_become_reading(world: PatientWorld) -> None:
    providers(world, "سكر 240\nNUMBERS: 140 سكر")
    world.post(media_message())
    assert world.receipt(1100).state == "completed"
    assert not any(r.body["category"] == "patient_report" for r in world.rows("clinical_fact"))
    assert any(i.template_id == "patient_voice_unreadable" for i in world.patient_intents())


def test_voice_danger_bypasses_model(world: PatientWorld) -> None:
    providers(world, "عندي ألم في صدري\nNUMBERS: none")
    world.post(media_message())
    assert world.rows("incident")
    assert any(i.template_id == "patient_emergency" for i in world.patient_intents())


@pytest.mark.parametrize("kind", ["photo", "document"])
def test_photo_document_durable_pending_then_scoped_media_recovery(
    world: PatientWorld, kind: str
) -> None:
    _, files, _ = providers(world, "unused", png())
    world.post(media_message(kind))
    assert world.receipt(1100).state == "completed"
    assert work(world).state == "pending" and work(world).work_clock
    assert any(
        i.template_id == "patient_evidence_received_pending" for i in world.patient_intents()
    )
    assert not any(r.body["category"] == "patient_report" for r in world.rows("clinical_fact"))
    sweep_due(world.runtime, world.store)
    saved = work(world)
    assert saved.source_blob_ref and saved.stage == "extract" and len(files.calls) == 1
    for minutes in (1, 5, 15):
        sweep_due(world.runtime, world.store)
        retry = work(world)
        assert retry.state == "pending" and retry.work_clock
        assert retry.work_clock.next_action_at == world.clock() + timedelta(minutes=minutes)
        world.clock.advance(timedelta(minutes=minutes))
    sweep_due(world.runtime, world.store)
    assert work(world).review_obligation_id


def test_photo_danger_caption_first(world: PatientWorld) -> None:
    _, files, _ = providers(world, "unused", png())
    world.post(media_message("photo", caption="عندي ألم في صدري"))
    assert world.rows("incident") and not files.calls
