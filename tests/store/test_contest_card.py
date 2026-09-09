"""The card a doctor actually receives, for a doctor whose record says Arabic.

A review reproduced the reported bug against a checkout that had already routed the
render call and the saved proposal through the resolver: `card_intents` reads the
raw preference back out of `accounts.language()` and overrides both. A source rail
over one module could not see it. This asserts the delivered text instead.
"""

import pytest

from sanad.store._base import StoreBase
from store.scribe_fixtures import ScribeWorld


@pytest.fixture(autouse=True)
def contest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")


def test_an_arabic_doctor_receives_an_english_card(store: StoreBase, clock: object) -> None:
    world = ScribeWorld.create(store, clock)  # type: ignore[arg-type]
    world.approve(language="ar")
    proposal = world.dictate(
        "New patient Synthetic Person, 40 years old. Add Concor 5.",
        {
            "patient": {"name_as_spoken": "Synthetic Person", "age": "40"},
            "orders": [{"action": "start", "drug": "Concor", "dose": "5"}],
        },
    )
    assert proposal.language == "en"
    sent = "\n".join(
        str(intent.payload.get("text", "")) for intent in world.cards() if intent.payload
    )
    assert sent, "no card was queued"
    # The buttons are emoji, so ASCII is not the test; Arabic letters are.
    arabic = [c for c in sent if "\u0600" <= c <= "\u06ff"]
    assert not arabic, f"an Arabic-stored doctor received Arabic text: {sent!r}"


def test_a_patient_message_is_english_too(store: StoreBase, clock: object) -> None:
    """Decision 023 covers patient chat, not only the doctor surface.

    The doctor path was fixed at the accounts accessor; the patient path reads
    `patient.language` in a dozen places, so this asserts the delivered text
    rather than any one of them.
    """
    from sanad.contact.templates import render as contact_render
    from sanad.evidence.templates import render as evidence_render

    assert "الضغط" not in contact_render("patient_monitor_prompt", "ar", metric="blood pressure")
    for text in (
        contact_render("patient_monitor_prompt", "ar", metric="blood pressure"),
        evidence_render("unverified", "ar"),
    ):
        arabic = [c for c in text if "؀" <= c <= "ۿ"]
        assert not arabic, f"an Arabic-stored patient received Arabic text: {text!r}"
