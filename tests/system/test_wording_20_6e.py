"""The verified sender supplies display text, never identity or account authority."""

import pytest
from harness import FakeClock
from pydantic import JsonValue
from store.account_fixtures import APPLICANT, PATIENT, update
from store.concierge_fixtures import PatientWorld
from store.login_fixtures import browser_login

from sanad.auth.service import revise
from sanad.channels.telegram.wording import render
from sanad.store._base import StoreBase
from sanad.web.api_record import displayed_order


def test_profile_name_approval_and_legacy_prompt(store: StoreBase, clock: FakeClock) -> None:
    w = PatientWorld.create(store, clock)
    assert isinstance(w, PatientWorld)
    message = update(APPLICANT, "/start", 9000)
    body = message["message"]
    assert isinstance(body, dict)
    sender = body["from"]
    assert isinstance(sender, dict)
    sender.update(first_name="Mona", last_name="Doctor")
    assert w.post(message).status_code == 200
    w.approve()
    assert w.doctor.name == "Mona Doctor"
    approved = next(i for i in w.intents() if i.template_id == "doctor_approved")
    assert "Mona Doctor" in str(approved.payload)
    p = w.bound()
    w.patient_scope = p.scope
    with w.client() as client:
        assert browser_login(client, w.login_path(PATIENT)).status_code == 303
        assert client.get("/api/patient/plan").json()["doctor_name"] == "Mona Doctor"
    w.seed(revise(w.doctor, clock(), name=""))
    for i in range(2):
        assert w.post(update(APPLICANT, "/help", 9010 + i)).status_code == 200
    prompts = [i for i in w.intents() if i.template_id == "doctor_name_needed"]
    assert len(prompts) == 1
    assert w.post(update(APPLICANT, "/name Mona Doctor", 9012)).status_code == 200
    assert w.receipt(9012).state == "completed" and w.doctor.name == "Mona Doctor"
    assert w.post(update(APPLICANT, "/name Another person", 9013)).status_code == 200
    assert w.doctor.name == "Mona Doctor"


@pytest.mark.parametrize("verb", ["request", "order", "ask for", "send for"])
def test_test_introducers_are_never_names(verb: str) -> None:
    from sanad.scribe.resolver import unresolved_test_fragments

    assert unresolved_test_fragments(("CBC", "K"), f"{verb} CBC and potassium") == ()


def test_help_and_displayed_frequency() -> None:
    help_text = render("doctor_help", "en")
    for command in (
        "login",
        "logout",
        "digest",
        "questions",
        "answer",
        "send",
        "reuse",
        "inbox",
        "corrections",
        "qr",
        "new",
    ):
        assert "/" + command in help_text
    raw: dict[str, JsonValue] = {
        "structured_instruction": {
            "drug": "Forxiga",
            "frequency": "in the morning",
            "timing": "in the morning",
        }
    }
    projected = displayed_order(raw)
    assert projected["structured_instruction"] == {
        "drug": "Forxiga",
        "frequency": "in the morning",
        "timing": None,
    }
    instruction = raw["structured_instruction"]
    assert isinstance(instruction, dict) and instruction["timing"] == "in the morning"


def test_doctor_uploaded_lab_retains_sender_source(store: StoreBase, clock: FakeClock) -> None:
    from pathlib import Path

    from providers.fixtures import document
    from store.photo_fixtures import photo, providers

    w = PatientWorld.create(store, clock)
    assert isinstance(w, PatientWorld)
    w.approve()
    w.seed(revise(w.doctor, clock(), language="en"))
    patient = w.named_stub("Synthetic Patient")
    reading = document(
        printed_name="Synthetic Patient",
        items=[{"name": "Potassium", "value": "4.1", "unit": "mmol/L"}],
    )
    providers(w, reading, reading, data=Path("tests/data/09b/lab_synthetic.png").read_bytes())
    assert w.post(photo("Synthetic Patient", id=9200)).status_code == 200
    w.tap("✅ Confirm", id=9201)
    assert w.scribe.repo.pending(w.doctor.scope) is None
    with w.client() as client:
        assert browser_login(client, w.login_path(APPLICANT)).status_code == 303
        response = client.get("/api/patients/" + patient.id)
        assert response.status_code == 200
        media = response.json()["media"]
        assert len(media) == 1 and media[0]["uploaded_by_you"] is True
        assert media[0]["date"]
