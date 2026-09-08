"""English transport and language-switch behavior on memory and DynamoDB Local."""

import json
from collections.abc import Iterator
from datetime import timedelta
from importlib import import_module
from typing import Any

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, document
from providers.photo11d_fixtures import paper
from scribe.test_surface_11f import no_arabic

from sanad.channels.telegram import wording
from sanad.domain.language import default_language
from sanad.scribe.card import render_card
from sanad.store._base import StoreBase
from store.account_fixtures import APPLICANT, update
from store.photo_fixtures import SHIFT, photo, prescription, providers
from store.scribe_fixtures import ScribeWorld


@pytest.fixture(autouse=True)
def zero_model_provider_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    client = import_module("botocore.client").BaseClient
    original = client._make_api_call
    calls: list[str] = []

    def guarded(instance: Any, operation: str, *args: Any, **kwargs: Any) -> object:
        if "bedrock" in instance.meta.service_model.service_name:
            calls.append(operation)
            raise AssertionError("11f forbids model provider calls")
        # The store fixture alone permits numeric loopback with dummy credentials.
        return original(instance, operation, *args, **kwargs)

    monkeypatch.setattr(client, "_make_api_call", guarded)
    yield
    assert calls == []


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    value = ScribeWorld.create(store, clock)
    value.approve(language="en")
    value.transport.calls.clear()
    value.transport.callback_calls.clear()
    return value


def transport_is_english(world: ScribeWorld) -> None:
    for call in world.transport.calls:
        if call.recipient_ref == APPLICANT:
            no_arabic(json.dumps(call.payload, ensure_ascii=False))
    for callback in world.transport.callback_calls:
        no_arabic(callback.text)


VALUE: dict[str, object] = {
    "patient": {
        "name_as_spoken": "Synthetic Person",
        "age": "53",
        "sex": "male",
        "identifiers": ["123"],
    },
    "facts": [{"category": "condition", "text": "hypertension"}],
}
SOURCE = "New patient Synthetic Person, age 53, male, identifier 123. History of hypertension."


def test_english_dictation_confirmation_and_failed_photo_reach_transport(
    world: ScribeWorld,
) -> None:
    proposal = world.dictate(SOURCE, VALUE)
    card = "\n".join(render_card(proposal))
    assert "New patient: Synthetic Person" in card
    world.tap("✅ Confirm")
    assert any("Recorded:" in str(call.payload) for call in world.transport.calls)
    failed = document(printed_name=None, unreadable=True, items=[])
    providers(world, failed, failed)
    world.post(photo("Synthetic Person", id=30))
    assert world.receipt(30).state == "completed"
    assert any(
        "I could not read this paper confidently" in str(call.payload)
        for call in world.transport.calls
    )
    transport_is_english(world)
    print("Rendered English doctor turn:\n" + card)
    print("Real model provider calls: 0")


@pytest.mark.parametrize("action", ["confirm", "edit", "reject", "expired", "stale"])
def test_reply_uses_current_doctor_language_and_sent_card_keeps_its_words(
    world: ScribeWorld, action: str
) -> None:
    world.post(update(APPLICANT, "/lang ar", 5))
    proposal = world.dictate(SOURCE, VALUE)
    original = next(i for i in world.cards() if i.template_id == "scribe_card")
    original_text = str((original.payload or {})["text"])
    assert "مريض جديد:" in original_text
    button = world.button({"edit": "✏️ تعديل", "reject": "❌ إلغاء"}.get(action, "✅ تمام"))
    if action == "stale":
        world.tap(raw=button, id=19)
    world.post(update(APPLICANT, "/lang en", 21))
    world.transport.calls.clear()
    world.transport.callback_calls.clear()
    if action == "expired":
        world.clock.advance(timedelta(minutes=31))
    world.tap(raw=button, id=22)
    transport_is_english(world)
    expected = {
        "confirm": "Recorded:",
        "edit": "Send your correction",
        "reject": "Card cancelled.",
        "expired": "The card expired.",
        "stale": "The card changed",
    }[action]
    delivered = (
        "\n".join(str(c.payload) for c in world.transport.calls)
        + "\n"
        + "\n".join(c.text for c in world.transport.callback_calls)
    )
    assert expected in delivered
    saved_card = next(i for i in world.cards() if i.id == original.id)
    assert saved_card.payload and saved_card.payload["text"] == original_text
    saved_proposal = world.scribe.repo.load(
        proposal.scope, "scribe_proposal", proposal.id, type(proposal)
    )
    assert saved_proposal and saved_proposal.language == "ar"


@pytest.mark.parametrize(
    "case",
    [
        "prescription",
        "disagreement",
        "missing_unit",
        "shift",
        "danger",
        "crop",
        "intake",
        "below_gate",
    ],
)
def test_english_photo_cards_buttons_failures_and_captions(world: ScribeWorld, case: str) -> None:
    world.named_stub("Synthetic Person")
    first = second = prescription()
    if case == "disagreement":
        second = prescription("50 mg")
    elif case == "missing_unit":
        first = second = document(
            printed_name=None, items=[{"name": "Potassium", "value": "4.1", "flag": "H"}]
        )
    elif case == "shift":
        first = second = SHIFT
    elif case == "danger":
        first = second = document(printed_name=None)
    elif case == "crop":
        first = second = document(
            document_type="prescription",
            printed_name=None,
            items=[{"name": "Bisoprolol", "dose": "5 mg", "frequency": "اليوم"}],
        )
    elif case == "below_gate":
        second = prescription(drug="Atorvastatin")
    providers(world, first, second, data=paper())
    world.post(photo("" if case == "intake" else "Synthetic Person"))
    assert world.receipt(10).state == "completed"
    assert world.transport.calls
    transport_is_english(world)
    texts = "\n".join(str(call.payload) for call in world.transport.calls)
    expected = {
        "prescription": "(start)",
        "disagreement": "Reading 2: 50 mg",
        "missing_unit": "no unit; printed flag: H",
        "shift": "Values may be shifted",
        "danger": "You have been alerted",
        "crop": "Arabic instruction column",
        "intake": "Not now",
        "below_gate": "I will record nothing from it.",
    }[case]
    assert expected in texts
    if case in {"prescription", "missing_unit"}:
        world.tap("✅ Confirm")
        transport_is_english(world)
        assert any("Recorded:" in str(call.payload) for call in world.transport.calls)


def test_photo_reading_choice_after_language_switch_renders_current_language(
    world: ScribeWorld,
) -> None:
    world.named_stub("Synthetic Person")
    world.post(update(APPLICANT, "/lang ar", 5))
    providers(world, prescription(), prescription("50 mg"))
    world.post(photo("Synthetic Person"))
    original = world.proposal
    raw = world.button("قراءة 2: 50 mg")
    world.post(update(APPLICANT, "/lang en", 11))
    world.transport.calls.clear()
    world.transport.callback_calls.clear()
    world.tap(raw=raw)
    assert world.proposal.id == original.id and world.proposal.language == "ar"
    transport_is_english(world)
    assert any("50 mg" in str(call.payload) for call in world.transport.calls)
    world.tap("✅ Confirm", id=22)
    transport_is_english(world)


@pytest.mark.parametrize(
    "command",
    [
        "/start",
        "/help",
        "/cancel",
        "/find nobody",
        "/qr nobody",
        "/intake",
        "/lang invalid",
        "/unknown",
    ],
)
def test_english_command_and_missing_media_surfaces_are_offline(
    world: ScribeWorld, command: str
) -> None:
    model = ScriptedModel()
    world.scribe.model_factory = lambda *args: model
    world.post(update(APPLICANT, command, 10))
    assert world.receipt(10).state == "completed"
    world.post(photo("Synthetic Person", id=11))
    assert world.receipt(11).state == "completed"
    assert model.script.calls == []
    transport_is_english(world)


def test_unreadable_arabic_name_is_preserved_as_data_in_english_card(world: ScribeWorld) -> None:
    world.named_stub("أحمد Synthetic")
    providers(world, prescription(), prescription())
    world.post(photo("أحمد Synthetic"))
    text = "\n".join(render_card(world.proposal, "en"))
    assert "Patient: أحمد Synthetic" in text
    no_arabic(text.replace("أحمد Synthetic", "Synthetic Person"))


def test_preapproval_default_and_known_doctor_recipient_language(world: ScribeWorld) -> None:
    assert world.runtime.accounts.language("999990") == default_language
    world.post(update("999990", "/start", 40))
    acknowledgment = next(
        i
        for i in world.intents()
        if i.recipient_subject == "999990" and i.template_id == "application_received"
    )
    assert acknowledgment.payload == {
        "text": wording.render("application_received", default_language)
    }
    world.post(update(APPLICANT, "/lang ar", 41))
    assert world.runtime.accounts.language(APPLICANT) == "ar"
    world.post(update(APPLICANT, "/lang en", 42))
    assert world.runtime.accounts.language(APPLICANT) == "en"


def test_queued_crop_keeps_rendered_language_and_rejects_forged_payload(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.scribe.photo_delivery import load_crop

    monkeypatch.setattr("sanad.api.internal.dispatch_inline", lambda *args: 0)
    world.named_stub("Synthetic Person")
    reading = document(
        document_type="prescription",
        printed_name=None,
        items=[{"name": "Bisoprolol", "dose": "5 mg", "frequency": "اليوم"}],
    )
    _, _, media = providers(world, reading, reading, data=paper())
    world.post(photo("Synthetic Person"))
    crop = next(i for i in world.cards() if i.template_id == "scribe_photo_column")
    original = crop.payload
    assert original and "Arabic instruction column" in str(original["text"])
    world.post(update(APPLICANT, "/lang ar", 11))
    assert world.doctor.language == "ar"
    assert load_crop(world.store, media, crop)
    for payload in (
        original | {"text": "Forged caption"},
        original | {"photo_blob_ref": "foreign-blob"},
        original | {"extra": "unreleased field"},
    ):
        with pytest.raises(ValueError, match="photo_reference"):
            load_crop(world.store, media, crop.model_copy(update={"payload": payload}))
    for intent in sorted(world.cards(), key=lambda i: i.conversation_sequence):
        if intent.template_id in {"scribe_card", "scribe_photo_column"}:
            assert world.dispatch(intent).status == "provider_accepted"
    transport_is_english(world)
    saved = next(i for i in world.cards() if i.id == crop.id)
    assert saved.payload == original


def test_unreadable_commit_still_rejects_unreleased_text(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.store.records import (
        CommitRequest,
        CommitResult,
        OutboundIntent,
        from_record,
        to_record,
    )

    commit = world.store.commit
    outcomes: list[str] = []

    def forge(request: CommitRequest) -> CommitResult:
        if request.command.payload.get("type") == "PhotoUnreadable":
            intent = from_record(request.intents[0], OutboundIntent)
            forged = intent.model_copy(update={"payload": {"text": "Nothing needs review."}})
            result = commit(
                request.model_copy(update={"intents": (to_record(forged, forged.scope),)})
            )
            outcomes.append(result.status)
            assert result.status == "forbidden"
        return commit(request)

    monkeypatch.setattr(world.store, "commit", forge)
    providers(world, prescription(), prescription(drug="Atorvastatin"))
    world.post(photo(""))
    assert outcomes == ["forbidden"] and world.receipt(10).state == "completed"
    transport_is_english(world)


def test_photo_demographics_and_explicit_deadlines_keep_data_in_english(world: ScribeWorld) -> None:
    from sanad.domain import DRAFT_POLICY_2026_09
    from sanad.scribe.extract import DictationCandidate, MissionCandidate, PatientCandidate
    from sanad.scribe.timing import candidate_timings

    world.named_stub("Synthetic Person")
    providers(world, prescription(), prescription())
    world.post(photo("Synthetic Person"))
    original = world.proposal
    candidate = DictationCandidate(
        patient=PatientCandidate(age="53", sex="male", identifiers=("123",)),
        orders=original.candidate.orders,
        missions=(
            MissionCandidate(kind="TEST", text="CBC", timing_expression="2026-09-20T12:00:00Z"),
        ),
    )
    timings, issues = candidate_timings(candidate, original.created_at, DRAFT_POLICY_2026_09)
    assert not issues
    proposal = original.model_copy(
        update={
            "candidate": candidate,
            "timings": timings,
            "source_text": original.source_text + " 53 123 CBC",
        }
    )
    text = "\n".join(render_card(proposal, "en"))
    no_arabic(text)
    assert "Age: 53" in text and "Sex: male" in text and "Identifiers: 123" in text
    assert "explicit" in text and "If not done I will notify you" in text
    assert proposal.timings == timings and proposal.candidate == candidate


@pytest.mark.parametrize("case", ["card", "fallback", "intake"])
def test_language_changed_while_reading_is_selected_when_rendered(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from sanad.auth.service import revise
    from store.login_fixtures import replace_model

    world.named_stub("Synthetic Person")
    world.post(update(APPLICANT, "/lang ar", 5))
    world.transport.calls.clear()

    def change_language(name: str) -> None:
        if name == "photo_reads_persisted":
            replace_model(world, revise(world.doctor, world.clock(), language="en"))

    monkeypatch.setattr(world.scribe, "checkpoint", change_language)
    second = prescription(drug="Atorvastatin") if case == "fallback" else prescription()
    providers(world, prescription(), second)
    world.post(photo("" if case == "intake" else "Synthetic Person"))
    assert world.doctor.language == "en" and world.receipt(10).state == "completed"
    assert world.transport.calls
    transport_is_english(world)
