"""The exact F23 first attempt, retry invariance and offered-name answers."""

from copy import deepcopy
from typing import Any

import pytest
from providers.fixtures import ScriptedModel, candidate
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld

from sanad.scribe.extract import DictationCandidate, PatientCandidate
from sanad.scribe.merge import merge_candidates
from scribe.test_grounding_invariant import world_for
from scribe.test_walkthrough_20_6d import FIRST


def value(name: str) -> dict[str, Any]:
    return {
        "patient": {"name_as_spoken": name},
        "orders": [
            {
                "action": "start",
                "drug": "Exforge",
                "dose": "5/160",
                "frequency": "once daily",
                "action_quote": "put him on",
            },
            {"action": "stop", "drug": "Aspirin", "action_quote": "we can hold"},
        ],
        "missions": [
            {"kind": "TEST", "text": "request CBC and potassium"},
            {
                "kind": "MONITOR",
                "text": "check his pressure twice a day for three days",
                "timing_expression": "twice a day for three days",
            },
        ],
    }


def pair(w: ScribeWorld, text: str, names: tuple[str, str], id: int) -> None:
    model = ScriptedModel(*(candidate(value(name)) for name in names))
    w.scribe.model_factory = lambda *_: model
    assert w.post(update(APPLICANT, text, id)).status_code == 200
    assert w.receipt(id).state == "completed"
    assert len(model.script.calls) == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_exact_first_attempt_and_unchanged_retry(reverse: bool) -> None:
    w = world_for("en")
    patient = w.named_stub("Ahmed Test")
    names = ("Ahmed Test", "Ahmed") if not reverse else ("Ahmed", "Ahmed Test")
    pair(w, FIRST, names, 8000)
    first = w.proposal
    assert first.selected_patient_id == patient.id
    assert not first.issues
    assert w.button("✅ Confirm")
    assert first.candidate.patient.name_as_spoken == "Ahmed Test"
    w.tap("❌ Cancel", id=8001)
    pair(w, FIRST, (names[1], names[0]), 8002)
    assert w.proposal.candidate == first.candidate
    assert not w.proposal.issues
    assert w.button("✅ Confirm")


@pytest.mark.parametrize("name", ["Ahmed Test", "Ahmed Other"])
@pytest.mark.parametrize("sentence", [False, True])
def test_each_offered_name_settles_without_model_authority(name: str, sentence: bool) -> None:
    w = world_for("en")
    patients = {n: w.named_stub(n) for n in ("Ahmed Test", "Ahmed Other")}
    pair(w, FIRST, ("Ahmed Test", "Ahmed Other"), 8010)
    assert any(i.item == "patient" and i.code == "extraction_conflict" for i in w.proposal.issues)
    with pytest.raises(AssertionError, match="card button missing"):
        w.button("✅ Confirm")
    old_orders = deepcopy(w.proposal.candidate.orders)
    # Even repeated conflicting model readings cannot override the explicit answer.
    pair(
        w, f"The patient name is {name}." if sentence else name, ("Ahmed Test", "Ahmed Other"), 8011
    )
    p = w.proposal
    assert p.selected_patient_id == patients[name].id
    assert p.candidate.patient.name_as_spoken == name
    assert p.candidate.orders == old_orders
    assert not p.issues, p.issues
    assert w.button("✅ Confirm")


@pytest.mark.parametrize(
    "short,long",
    [("Ahmed", "Ahmed Test"), ("Test", "Ahmed Test"), ("Ahmed Ali", "Ahmed Mohamed Ali")],
)
def test_only_panel_supported_whole_name_subsets_coalesce(short: str, long: str) -> None:
    a = DictationCandidate(patient=PatientCandidate(name_as_spoken=short))
    b = DictationCandidate(patient=PatientCandidate(name_as_spoken=long))
    merged = merge_candidates(a, b, long, panel_names=(long,))
    assert merged and not merged.issues and merged.candidate.patient.name_as_spoken == long
    absent = merge_candidates(a, b, long, panel_names=("Different Person",))
    assert absent and any(i.item == "patient" for i in absent.issues)


@pytest.mark.parametrize("reply", ["Not Ahmed Other", "Ahmed Test or Ahmed Other", "Ahmed Third"])
def test_ambiguous_or_unoffered_reply_keeps_name_question(reply: str) -> None:
    from sanad.scribe.corrections import patient_answer

    w = world_for("en")
    w.named_stub("Ahmed Test")
    w.named_stub("Ahmed Other")
    pair(w, FIRST, ("Ahmed Test", "Ahmed Other"), 8020)
    assert patient_answer(w.proposal, reply) is None
