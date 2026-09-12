"""The exact adversarial corpus also crosses the DynamoDB serialization boundary."""

from typing import Any

import pytest
from harness import FakeClock
from scribe.test_grounding_invariant import CORPUS, exercise_corpus

from sanad.domain.language import Language
from sanad.store._base import StoreBase
from store.scribe_fixtures import ScribeWorld


@pytest.mark.parametrize("row", CORPUS, ids=lambda row: row["id"])
@pytest.mark.parametrize("language", ["en", "ar"])
@pytest.mark.usefixtures("legacy_dictation_schema")
def test_grounded_corpus_survives_store_and_confirmation(
    store: StoreBase, clock: FakeClock, row: dict[str, Any], language: Language
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language=language)
    exercise_corpus(row, language, world)
