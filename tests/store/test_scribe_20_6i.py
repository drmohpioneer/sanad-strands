"""F23: real /new confirmation, delayed panel index and either reader order."""

from copy import deepcopy
from datetime import timedelta

import pytest
from providers.fixtures import ScriptedModel, candidate
from scribe.test_walkthrough_20_6d import FIRST
from scribe.test_walkthrough_20_6h import value

from sanad.scribe.card import render_card
from sanad.store._base import StoreBase
from store.account_fixtures import APPLICANT, update
from store.conftest import Clock
from store.scribe_fixtures import ScribeWorld


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("retry_finds_test", [False, True])
def test_first_dictation_after_new_with_lagging_panel(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    reverse: bool,
    retry_finds_test: bool,
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    world.post(update(APPLICANT, "/new Ahmed Test", 8000))
    world.tap("✅ Confirm", id=8001)
    state = store.get(world.doctor.scope, "scribe_state", "current")
    assert state and state.recent_patient_id
    patient_id = state.recent_patient_id
    monkeypatch.setattr(store, "list_patients", lambda *args, **kwargs: ((), None))
    clock.now += timedelta(seconds=5)
    complete = value("Ahmed Test")
    incomplete = deepcopy(complete)
    incomplete["patient"] = {"name_as_spoken": "Ahmed"}
    incomplete["missions"] = [m for m in incomplete["missions"] if m["kind"] != "TEST"]
    incomplete["ambiguities"] = ["Ahmed", "CBC and potassium were not found as drugs"]
    readings = [incomplete, complete] if not reverse else [complete, incomplete]
    readings.append(complete if retry_finds_test else incomplete)
    model = ScriptedModel(*(candidate(reading) for reading in readings))
    world.scribe.model_factory = lambda *_: model
    world.post(update(APPLICANT, FIRST, 8002))
    proposal = world.proposal
    assert len(model.script.calls) == 3
    assert proposal.selected_patient_id == patient_id
    assert not proposal.issues, (proposal.issues, render_card(proposal))
    assert world.button("✅ Confirm")
    world.tap("✅ Confirm", id=8003)
    from sanad.domain import PatientScope

    rows, _ = store.list_records(
        PatientScope(doctor_id=world.doctor.id, patient_id=patient_id), "mission"
    )
    assert sorted(str(r.body["kind"]) for r in rows) == [
        "MEDICATION",
        "MEDICATION",
        "MONITOR",
        "TEST",
    ]
    monitor = next(r for r in rows if r.body["kind"] == "MONITOR")
    details = monitor.body["details"]
    assert isinstance(details, dict) and isinstance(details["slots"], list)
    assert len(details["slots"]) == 6
