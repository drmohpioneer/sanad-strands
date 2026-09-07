"""Latin ASR variants and compressed doses through the receipt and commit boundary."""

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate

from sanad.domain import PatientScope
from sanad.scribe.card import render_card
from sanad.scribe.patients import panel
from sanad.store._base import StoreBase
from store.scribe_fixtures import ScribeWorld
from store.test_scribe_voice_web import providers, voice


@pytest.mark.parametrize("prefix", ["560", "516"])
def test_voice_asr_spelling_and_compound_question_cannot_commit_an_invented_dose(
    store: StoreBase,
    clock: FakeClock,
    prefix: str,
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve()
    source = (
        f"مريض جديد تجربة واحد ماشي على X-Force HCT {prefix} 12.5 وConcord 5 مج مرة "
        "وضفت Forsige وطلبت Bano Creatine Na K"
    )
    providers(world, source + f" NUMBERS: {prefix}: HCT, 12.5: جرعة, 5: مج")
    value = {
        "patient": {"name_as_spoken": "تجربة واحد"},
        "orders": [
            {"drug": "X-Force HCT", "action": "continue", "dose": prefix + " 12.5"},
            {"drug": "Concord", "action": "continue", "dose": "5 مج", "frequency": "مرة"},
            {"drug": "Forsige", "action": "start"},
        ],
        "missions": [{"kind": "TEST", "text": "Bano Creatine, Na, K"}],
    }
    model = ScriptedModel(candidate(value))
    world.scribe.model_factory = lambda registry, role: model
    world.post(voice())
    proposed = world.proposal
    assert proposed.prompt_version == "scribe-v8" and proposed.disputed_numbers == ()
    assert [o.drug for o in proposed.candidate.orders] == ["Exforge HCT", "Concor", "Forxiga"]
    assert proposed.candidate.missions[0].text == "Bano Creatine, Na, K"
    assert proposed.candidate.missions[0].clinical_en == "BUN, creatinine, Na, K"
    assert not any(f.category == "medication_history" for f in proposed.candidate.facts)
    card = render_card(proposed)[0]
    assert f"Exforge HCT {prefix} 12.5\n" in card
    assert f'سمعت "{prefix} 12.5" لـ Exforge HCT، قصدك 5/160/12.5؟' in card
    assert "Concor 5 mg, مرة" in card and "Forxiga (بداية)" in card
    assert proposed.blocked("order:0") and proposed.blocked("order:2")
    assert not proposed.blocked("order:1")
    system = " ".join(c.get("text", "") for c in model.script.calls[0]["system"])
    from sanad.scribe.resolver import hint_names

    assert "Known names" in system and ", ".join(hint_names().split(", ")[:200]) in system
    world.tap()
    patient = panel(store, world.doctor.scope)[0]
    scope = PatientScope(doctor_id=world.doctor.id, patient_id=patient.id)
    orders, _ = store.list_records(scope, "care_order_head")
    assert [o.body["name"] for o in orders] == ["Concor"]
    assert store.list_records(scope, "followup")[0] == ()
