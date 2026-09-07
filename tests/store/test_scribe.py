from datetime import timedelta

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate
from scribe.dictations import OWNER_SYNTHETIC, SYNTHETIC_TABLE, TABLE, DictationExample

from sanad.domain import PatientScope
from sanad.scribe.card import render_card
from sanad.scribe.patients import panel
from sanad.scribe.proposal import Proposal
from sanad.scribe.records import ClinicalFact
from sanad.store._base import StoreBase
from sanad.store.records import from_record
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    world = ScribeWorld.create(store, clock)
    world.approve()
    return world


@pytest.mark.parametrize("example", TABLE, ids=[f"dictation-{i + 1}" for i in range(len(TABLE))])
def test_handwritten_cards_through_real_receipt(
    world: ScribeWorld, example: DictationExample
) -> None:
    if example is OWNER_SYNTHETIC:
        from store.test_scribe_voice_web import providers, voice

        providers(world, example.input + " NUMBERS: 53 560 12.5 5")
        model = ScriptedModel(candidate(example.candidate.model_dump()))
        world.scribe.model_factory = lambda registry, role: model
        world.post(voice())
        proposal = world.proposal
        assert proposal.disputed_numbers == ("45",)
    else:
        model = ScriptedModel(
            *(candidate(example.candidate.model_dump()) for _ in range(example.extraction_calls))
        )
        world.scribe.model_factory = lambda registry, role: model
        world.post(update(APPLICANT, example.input, 10))
        assert world.receipt(10).state == "completed"
        assert len(model.script.calls) == example.extraction_calls
        if example.extraction_calls == 2:
            assert model.script.calls[0] == model.script.calls[1]
        proposal = world.proposal
    assert render_card(proposal) == (example.card,)
    assert panel(world.store, world.doctor.scope) == ()
    card = next(i for i in world.cards() if i.template_id == "scribe_card")
    assert card.payload and card.payload["text"] == example.card
    assert world.dispatch(card).status == "provider_accepted"


def test_create_commit_clinical_batch_and_day3(world: ScribeWorld) -> None:
    sample = SYNTHETIC_TABLE[0]
    proposed = world.dictate(sample.input, sample.candidate.model_dump())
    world.tap()
    row = world.store.get(world.doctor.scope, "scribe_proposal", proposed.id)
    assert row and from_record(row, Proposal).status == "confirmed"
    patients = panel(world.store, world.doctor.scope)
    assert len(patients) == 1
    patient = patients[0]
    assert patient.display_name == "أحمد رضا"
    scope = PatientScope(doctor_id=world.doctor.id, patient_id=patient.id)
    facts, _ = world.store.list_records(scope, "clinical_fact")
    assert len(facts) == 1
    assert (
        from_record(facts[0], ClinicalFact).provenance.source_observation_id
        == proposed.source_receipt_id
    )
    missions, _ = world.store.list_records(scope, "mission")
    followups, _ = world.store.list_records(scope, "followup")
    assert {m.body["kind"] for m in missions} == {"MEDICATION", "TEST"}
    assert all(m.body["state"] == "awaiting_link" for m in missions)
    medication = next(m for m in missions if m.body["kind"] == "MEDICATION")
    details = medication.body["details"]
    assert isinstance(details, dict) and isinstance(details["order_ref"], dict)
    ref = details["order_ref"]
    assert ref["entity_type"] == "care_order_version"
    assert world.store.get(scope, "care_order_version", str(ref["id"])) is not None
    assert len(followups) == 1 and followups[0].body["state"] == "awaiting_anchor"
    assert len(world.store.list_records(scope, "care_order_head")[0]) == 2
    assert len(world.store.list_records(scope, "care_order_version")[0]) == 2
    assert len(world.store.list_records(scope, "care_plan")[0]) == 1
    assert any(i.template_id == "scribe_invitation" for i in world.intents())


def test_expired_refused_without_creating_patient(world: ScribeWorld) -> None:
    sample = SYNTHETIC_TABLE[0]
    proposed = world.dictate(sample.input, sample.candidate.model_dump())
    raw = world.button("✅ تمام")
    world.clock.advance(timedelta(minutes=31))
    world.tap(raw=raw)
    row = world.store.get(world.doctor.scope, "scribe_proposal", proposed.id)
    assert row and row.body["status"] == "expired"
    assert panel(world.store, world.doctor.scope) == ()
