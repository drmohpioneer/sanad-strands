from datetime import timedelta

import pytest
from harness import FakeClock
from providers.fixtures import (
    FakeS3,
    FakeTelegramFiles,
    ScriptedConverter,
    ScriptedModel,
    ScriptedSpeech,
    candidate,
)
from pydantic import JsonValue

from sanad.domain import DRAFT_POLICY_2026_09, Principal
from sanad.media.retrieve import MediaRetriever
from sanad.media.speech import SpeechAdapter
from sanad.media.telegram import MediaFailure
from sanad.models.io import ModelUnavailable
from sanad.ops.sweep import sweep_due
from sanad.scribe.card import render_card
from sanad.scribe.patients import panel
from sanad.steward.types import StewardPolicy
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.keys import IntakeScope
from sanad.store.records import InboundReceipt, MediaWork, from_record
from store.account_fixtures import APPLICANT, update
from store.login_fixtures import browser_login
from store.scribe_fixtures import ScribeWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    world = ScribeWorld.create(store, clock)
    world.approve()
    return world


def voice(id: int = 10) -> dict[str, JsonValue]:
    body = update(APPLICANT, "", id)
    message = body["message"]
    assert isinstance(message, dict)
    message.pop("text")
    message["voice"] = {"file_id": "synthetic-voice", "file_unique_id": "synthetic", "duration": 15}
    return body


def providers(
    world: ScribeWorld,
    reply: str | ModelUnavailable,
    download: bytes | MediaFailure = b"OggSsynthetic",
) -> tuple[ScriptedSpeech, FakeTelegramFiles, FakeS3]:
    speech, files, s3 = ScriptedSpeech(reply), FakeTelegramFiles(download), FakeS3()
    converter = ScriptedConverter()

    def media(receipt: InboundReceipt, actor: Principal) -> MediaRetriever:
        return MediaRetriever(
            world.runtime.steward,
            s3,
            files,
            converter,
            IntakeScope(doctor_id=world.doctor.id, intake_id=keys.digest(receipt.id)),
            actor,
            lambda: world.claims.doctor(actor) is not None,
            StewardPolicy(DRAFT_POLICY_2026_09),
        )

    world.scribe.media_factory = media
    world.scribe.speech_factory = lambda source: SpeechAdapter(speech, converter, source)
    return speech, files, s3


def work(world: ScribeWorld, id: int = 10) -> MediaWork:
    receipt = world.receipt(id)
    scope = IntakeScope(doctor_id=world.doctor.id, intake_id=keys.digest(receipt.id))
    row = world.store.get(scope, "media_work", keys.digest(receipt.id))
    assert row is not None
    return from_record(row, MediaWork)


def test_voice_disputed_alert_is_blocked_then_explicit_edit_accepts(world: ScribeWorld) -> None:
    speech, files, s3 = providers(world, "أحمد رضا بلّغني لو السكر فوق 100\nNUMBERS: 200 سكر")
    value: dict[str, object] = {
        "intent": "update_record",
        "patient": {"name_as_spoken": "أحمد رضا"},
        "alerts": ["السكر فوق 100"],
        "numbers_used": ["100"],
    }
    model = ScriptedModel(candidate(value), candidate(value))
    world.scribe.model_factory = lambda registry, role: model
    world.post(voice())
    assert world.receipt(10).state == "completed" and len(speech.calls) == 1
    assert len(files.calls) == 1 and len(model.script.calls) == 2
    proposal = world.proposal
    assert proposal.blocked("alert:0") and set(proposal.disputed_numbers) == {"100", "200"}
    assert "محتاج تأكيد" in render_card(proposal)[0]
    media = work(world)
    assert media.state == "completed" and media.work_clock is None and media.association_ref
    assert proposal.source_transcript_ref == media.transcript_ref
    assert proposal.source_provenance[0].source_span is None
    assert proposal.source_provenance[0].source_observation_id == proposal.source_receipt_id
    assert proposal.source_provenance[0].prompt_version == "scribe-v8"
    assert proposal.source_provenance[0].model_id == "us.amazon.nova-lite-v1:0"
    assert media.transcript_ref and s3.get(media.scope, media.transcript_ref, 100000).startswith(
        b"{"
    )
    assert not panel(world.store, world.doctor.scope)
    world.tap("✏️ تعديل")
    value["alerts"], value["numbers_used"] = ["السكر فوق 200"], ["200"]
    changed = world.dictate("السكر فوق 200", value, id=11)
    assert not changed.blocked("alert:0")
    world.tap(id=21)
    patient = panel(world.store, world.doctor.scope)[0]
    order = world.store.list_records(patient.scope, "care_order_version")[0][0]
    instruction = order.body["structured_instruction"]
    assert isinstance(instruction, dict) and instruction["threshold"] == "200"
    world.clock.advance(timedelta(days=2))
    sweep_due(world.runtime, world.store)
    assert not world.store.list_records(media.scope, "review")[0]


@pytest.mark.parametrize("failure", ["expired_handle", "speech_unavailable"])
def test_voice_failure_completes_with_one_resend_and_timed_review(
    world: ScribeWorld, failure: str
) -> None:
    speech, files, _ = providers(
        world,
        ModelUnavailable(reason="unavailable"),
        MediaFailure(reason="expired_handle") if failure == "expired_handle" else b"OggSsynthetic",
    )
    model = ScriptedModel()
    world.scribe.model_factory = lambda registry, role: model
    world.post(voice())
    assert world.receipt(10).state == "completed"
    assert world.scribe.repo.pending(world.doctor.scope) is None and not model.script.calls
    assert len(speech.calls) == (0 if failure == "expired_handle" else 1) and len(files.calls) == 1
    media = work(world)
    assert media.state == "needs_attention" and media.review_obligation_id
    intents = world.store.list_records(media.scope, "outbound_intent")[0]
    assert len(intents) == 1 and intents[0].body["template_id"] == "doctor_voice_unreadable"
    assert not world.cards()
    sweep_due(world.runtime, world.store)
    world.clock.advance(timedelta(days=2))
    sweep_due(world.runtime, world.store)
    assert len(world.store.list_records(media.scope, "outbound_intent")[0]) == 1


def test_edit_voice_uses_same_correction_and_photo_keeps_pending(world: ScribeWorld) -> None:
    world.post(update(APPLICANT, "/new أحمد رضا", 10))
    proposal = world.proposal
    world.tap("✏️ تعديل")
    photo = update(APPLICANT, "", 11)
    message = photo["message"]
    assert isinstance(message, dict)
    message.pop("text")
    message["photo"] = [
        {"file_id": "synthetic-photo", "file_unique_id": "synthetic", "width": 10, "height": 10}
    ]
    world.post(photo)
    assert world.proposal.id == proposal.id and world.proposal.editing
    providers(world, "اسمه أحمد سعيد\nNUMBERS: none")
    value = {"intent": "create_patient", "patient": {"name_as_spoken": "أحمد سعيد"}}
    model = ScriptedModel(candidate(value), candidate(value))
    world.scribe.model_factory = lambda registry, role: model
    world.post(voice(12))
    assert world.proposal.prompt_version == "scribe-correction-v8"
    assert world.proposal.candidate.patient.name_as_spoken == "أحمد سعيد"
    assert "previous_candidate" in str(model.script.calls[0])


def test_web_lists_only_session_doctor_and_foreign_id_is_404(world: ScribeWorld) -> None:
    own = world.named_stub("أحمد رضا")
    world.approve("40004")
    foreign = world.named_stub("أحمد سعيد", subject="40004")
    client = world.client()
    assert client.get("/api/patients").status_code == 401
    assert browser_login(client, world.login_path()).status_code == 303
    listed = client.get("/api/patients")
    assert listed.status_code == 200
    assert [row["patient_id"] for row in listed.json()] == [own.id]
    assert client.get("/api/patients/" + foreign.id).status_code == 404
    assert client.get("/api/patients/absent").status_code == 404
    proposal = world.dictate(
        "أحمد رضا عنده حساسية بنسلين",
        {
            "intent": "update_record",
            "patient": {"name_as_spoken": "أحمد رضا"},
            "facts": [{"category": "allergy", "text": "بنسلين"}],
        },
        id=11,
    )
    detail = client.get("/api/patients/" + own.id).json()
    assert detail["pending_proposal"]["proposal_id"] == proposal.id and not detail["facts"]
    world.tap()
    detail = client.get("/api/patients/" + own.id).json()
    assert detail["pending_proposal"] is None and len(detail["facts"]) == 1
    from sanad.accounts.commands import SuspendDoctor

    doctor = world.doctor
    world.runtime.accounts.suspend(
        SuspendDoctor(
            command_id="suspend",
            actor=world.actor(),
            doctor_id=doctor.id,
            expected_doctor_version=doctor.version,
            reason_code="synthetic",
        )
    )
    assert client.get("/api/patients").status_code == 401
