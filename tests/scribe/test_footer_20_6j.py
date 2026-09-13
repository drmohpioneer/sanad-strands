"""All frozen issue sets share the actual keyboard predicate, in both languages."""

from types import SimpleNamespace
from typing import cast

import pytest
from domain_fixtures import NOW
from store.processing_fixtures import DOCTOR

from sanad.channels.telegram import wording
from sanad.scribe.card import render_card
from sanad.scribe.proposal import Proposal, card_actions, card_footer
from sanad.scribe.turn import ScribeTurn
from scribe.test_view_11j import ORACLE


@pytest.mark.parametrize("row", ORACLE, ids=[r["id"] for r in ORACLE])
@pytest.mark.parametrize("language", ["en", "ar"])
def test_every_frozen_issue_set_matches_keyboard(row: dict[str, object], language: str) -> None:
    p = Proposal.model_validate(row["proposal"])
    harness = SimpleNamespace(
        claims=SimpleNamespace(doctor=lambda _: SimpleNamespace(language=language)),
        repo=SimpleNamespace(clock=lambda: NOW),
        photos=SimpleNamespace(buttons=lambda *args: ((), [])),
    )
    _, keyboard = ScribeTurn.buttons(cast(ScribeTurn, harness), p, DOCTOR, "synthetic-confirm")
    assert isinstance(keyboard, dict) and isinstance(keyboard["inline_keyboard"], list)
    labels = []
    for button_row in keyboard["inline_keyboard"]:
        assert isinstance(button_row, list)
        for button in button_row:
            assert isinstance(button, dict) and isinstance(button["text"], str)
            labels.append(button["text"])
    assert " | ".join(labels) == card_footer(p, language)
    assert card_footer(p, language) in "\n".join(render_card(p, language)).splitlines()
    assert (wording.button("confirm", language) in labels) == (("confirm", None) in card_actions(p))
