"""Removal citation, two-reader agreement, and authenticated target-only links."""

import re

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate, response
from store.account_fixtures import APPLICANT, update
from store.conftest import clock as clock
from store.conftest import ddb_server as ddb_server
from store.conftest import pytest_generate_tests as pytest_generate_tests
from store.conftest import store as store
from store.login_fixtures import browser_login
from store.scribe_fixtures import ScribeWorld
from store.test_removal import remove

from sanad.scribe.extract import DictationCandidate
from sanad.scribe.grounding import removal_request
from sanad.scribe.patients import panel
from sanad.store._base import StoreBase


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    result = ScribeWorld.create(store, clock)
    result.approve(language="en")
    return result


@pytest.mark.parametrize(
    ("text", "quote", "outcome"),
    [
        ("remove Ahmed Test", "remove Ahmed Test", "verified"),
        ("don't remove Ahmed Test", "remove Ahmed Test", "negated"),
        ("he said remove Ahmed Test", "remove Ahmed Test", "unverified"),
        ("yesterday remove Ahmed Test", "remove Ahmed Test", "unverified"),
        ("Ahmed Test is well", "remove Ahmed Test", "unverified"),
    ],
)
def test_removal_citation(text: str, quote: str, outcome: str) -> None:
    assert removal_request(quote, text, "Ahmed Test") == outcome


def test_optional_quote_preserves_legacy_serialization() -> None:
    value = DictationCandidate()
    assert "removal_quote" not in value.model_dump()
    assert "removal_quote" not in value.model_dump_json()
    quoted = value.model_copy(update={"removal_quote": "remove Ahmed Test"})
    assert (
        DictationCandidate.model_validate_json(quoted.model_dump_json()).removal_quote
        == quoted.removal_quote
    )


@pytest.mark.parametrize("mode", ["agree", "disagree", "failed", "ambiguous", "mixed"])
def test_removal_readers_and_targeted_exchange(world: ScribeWorld, mode: str) -> None:
    w = world
    patient = w.named_stub("Ahmed Test")
    if mode == "ambiguous":
        w.named_stub("Ahmed Test")
    value: dict[str, object] = {
        "patient": {"name_as_spoken": "Ahmed Test"},
        "removal_quote": "remove Ahmed Test",
    }
    if mode == "mixed":
        value["facts"] = [{"text": "BP 120/80"}]
    second = dict(value)
    if mode == "disagree":
        second.pop("removal_quote")
    script = (
        ScriptedModel(candidate(value), response("invalid"), response("invalid"))
        if mode == "failed"
        else ScriptedModel(candidate(value), candidate(second))
    )
    w.scribe.model_factory = lambda registry, role: script
    assert (
        w.post(
            update(APPLICANT, "remove Ahmed Test" + (" BP 120/80" if mode == "mixed" else ""), 505)
        ).status_code
        == 200
    )
    assert len(script.script.calls) == (3 if mode == "failed" else 2)
    assert w.scribe.repo.pending(w.doctor.scope) is None
    profile = w.store.get_patient_profile(patient.scope)
    assert profile and profile.removed_at is None
    links = [i for i in w.intents() if i.template_id == "scribe_removal_link"]
    if mode != "agree":
        assert not links
        return
    assert len(links) == 1
    link = links[0]
    assert w.dispatch(link).status == "provider_accepted"
    assert link.payload
    match = re.search(r"https://sanad.example(/d/[^\s)]+)", str(link.payload["text"]))
    assert match
    with w.client() as client:
        login_response = browser_login(client, match[1])
        assert login_response.status_code == 303
        assert login_response.headers["location"] == "/a/patients/" + patient.id + "#remove"
        assert client.post(match[1], data={"csrf": "replay"}).status_code in {403, 410}


def test_removed_name_requires_explicit_creation(world: ScribeWorld) -> None:
    w = world
    patient = w.named_stub("Ahmed Test")
    remove(w, patient.id)
    p = w.dictate("Ahmed Test", {"patient": {"name_as_spoken": "Ahmed Test"}})
    assert not p.creating_patient and not p.selected_patient_id
    assert len(panel(w.store, w.doctor.scope)) == 1
    text = "\n".join(str(i.payload.get("text", "")) for i in w.cards() if i.payload)
    assert "This name belongs to a removed patient. Create a new patient with this name?" in text
    w.tap("Create new patient", id=507)
    assert w.proposal.creating_patient
    w.tap("✅ Confirm", id=508)
    assert len(panel(w.store, w.doctor.scope)) == 2


def test_stale_selection_returns_fresh_card(world: ScribeWorld) -> None:
    w = world
    patient = w.named_stub("Ahmed Test")
    other = w.named_stub("Ahmed Tarek")
    proposal = w.dictate("Ahmed", {"patient": {"name_as_spoken": "Ahmed"}})
    raw = w.button("Ahmed Test", proposal)
    remove(w, patient.id)
    w.tap(raw=raw, id=590)
    fresh = w.proposal
    assert fresh.version > proposal.version
    assert fresh.selected_patient_id is None
    assert [c.patient_id for c in fresh.choices] == [other.id]
    assert w.transport.callback_calls[-1].text == "This patient was removed."


def test_targeted_removal_link_expires_at_ten_minutes(world: ScribeWorld) -> None:
    from datetime import timedelta

    from sanad.auth.commands import IssueDoctorLogin

    w = world
    patient = w.named_stub("Ahmed Test")
    assert (
        w.login.issue(
            IssueDoctorLogin(
                command_id="expiring-removal",
                actor=w.owner,
                removal_patient_id=patient.id,
            )
        ).status
        == "accepted"
    )
    link = next(i for i in w.intents() if i.template_id == "scribe_removal_link")
    assert link.payload
    match = re.search(r"https://sanad.example(/d/[^\s)]+)", str(link.payload["text"]))
    assert match
    w.clock.advance(timedelta(minutes=10))
    with w.client() as client:
        assert browser_login(client, match[1]).status_code == 403
    profile = w.store.get_patient_profile(patient.scope)
    assert profile and profile.removed_at is None


def test_pending_card_refuses_removed_patient(world: ScribeWorld) -> None:
    w = world
    patient = w.named_stub("Ahmed Test")
    proposal = w.dictate(
        "Ahmed Test. Hypertension.",
        {
            "patient": {"name_as_spoken": "Ahmed Test"},
            "facts": [{"category": "condition", "text": "Hypertension"}],
        },
    )
    assert proposal.selected_patient_id == patient.id
    token = w.button("✅ Confirm", proposal)
    remove(w, patient.id)
    before = w.store.list_records(patient.scope, "clinical_fact")[0]
    w.tap(raw=token, id=591)
    assert w.store.list_records(patient.scope, "clinical_fact")[0] == before
    assert w.scribe.repo.pending(w.doctor.scope)
    assert w.transport.callback_calls[-1].text == "This patient was removed. Nothing was saved."
