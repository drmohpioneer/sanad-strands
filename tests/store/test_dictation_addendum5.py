"""Embedded strengths and duplicate history through real receipt/confirmation on both stores."""

import pytest
from harness import FakeClock

from sanad.domain import PatientScope
from sanad.scribe.card import render_card
from sanad.scribe.patients import panel
from sanad.store._base import StoreBase
from store.scribe_fixtures import ScribeWorld


@pytest.mark.parametrize("proposed_name", [None, "Concor", "Forxiga", "Inventedbrand"])
def test_embedded_dose_history_and_name_guard_survive_confirmation(
    store: StoreBase, clock: FakeClock, proposed_name: str | None
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve()
    proposal = world.dictate(
        "مريض جديد تجربة واحد ماشي على كونكور 5 مج مرة",
        {
            "patient": {"name_as_spoken": "تجربة واحد"},
            "orders": [
                {
                    "action": "continue",
                    "drug": "كونكور 5 مج",
                    "name_latin": proposed_name,
                    "frequency": "مرة",
                }
            ],
            "facts": [{"category": "medication_history", "text": "بياخد كونكور"}],
        },
    )
    blocked = proposed_name in {"Forxiga", "Inventedbrand"}
    assert proposal.prompt_version == "scribe-v7"
    assert proposal.candidate.facts == ()
    assert proposal.candidate.orders[0].dose == "5 مج"
    assert proposal.blocked("order:0") == blocked
    text = render_card(proposal)[0]
    assert ("(؟)" in text) == blocked
    if not blocked:
        assert "Concor 5 mg, مرة" in text
        assert "محتاج تأكيد:" not in text
    world.tap()
    patient = panel(store, world.doctor.scope)[0]
    scope = PatientScope(doctor_id=world.doctor.id, patient_id=patient.id)
    heads = store.list_records(scope, "care_order_head")[0]
    assert [r.body["name"] for r in heads] == ([] if blocked else ["Concor"])
    assert store.list_records(scope, "clinical_fact")[0] == ()
    assert store.list_records(scope, "followup")[0] == ()
