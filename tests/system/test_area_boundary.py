"""D.1: area answers must authorize a meaningful places query."""

from collections.abc import Callable

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate
from store.account_fixtures import PATIENT, update
from store.resolver_fixtures import QUESTION, add, get, setup

from sanad.resolver.attempts import area_in
from sanad.resolver.places import PlacesResult
from sanad.store._base import StoreBase


@pytest.mark.parametrize("answer", ["...", "؟", "!!", "", "Nasr City", "في مدينة نصر"])
def test_t28_area_answer_records_search_boundary(
    store: StoreBase, clock: FakeClock, answer: str
) -> None:
    w, capture = setup(store, clock)
    original = add(w)
    delegate = capture.provider()

    class RecordingPlaces:
        calls: list[str] = []

        async def search(self, area: str, *, authorized: Callable[[], bool]) -> PlacesResult:
            self.calls.append(area)
            return await delegate.search(area, authorized=authorized)

    provider = RecordingPlaces()
    w.concierge.places_provider = provider
    w.send("The lab is too expensive", {"step": "ask_patient", "question": QUESTION})
    assert provider.calls == []
    model = ScriptedModel(
        candidate(
            {"step": "find_places", "question": "What practical difficulty do you need help with?"}
        )
    )
    w.concierge.model_factory = lambda registry, role: model
    assert w.post(update(PATIENT, answer, 1001)).status_code == 200
    current = get(w)
    if answer in {"Nasr City", "في مدينة نصر"}:
        assert provider.calls == ["Nasr City" if answer == "Nasr City" else "مدينة نصر"]
        assert current.barrier_attempts[-1].places
    else:
        assert provider.calls == []
        assert area_in(answer, answering=True) is None
        assert capture.requests == []
        assert all(attempt.area is None for attempt in current.barrier_attempts)
        assert current.state == "blocked"
    assert current.due_at == original.due_at
