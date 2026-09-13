"""Persisted semantics of the owner's exact confirmed dictations."""

from datetime import timedelta

import pytest
from domain_fixtures import NOW
from harness import FakeClock
from store.scribe_fixtures import ScribeWorld

from sanad.scribe.proposal import Proposal
from sanad.store._base import StoreBase
from system.test_clock_paths_20_6e import AdvancingClock


@pytest.fixture
def clock() -> AdvancingClock:
    return AdvancingClock(NOW)


@pytest.mark.parametrize("busy", [False, True])
def test_exact_change_confirm(store: StoreBase, clock: FakeClock, busy: bool) -> None:
    w = ScribeWorld.create(store, clock)
    w.approve(language="en")
    patient = w.named_stub("Ahmed Test")
    w.dictate(
        "Ahmed Test, put him on Exforge 5/160 once daily",
        {
            "patient": {"name_as_spoken": "Ahmed Test"},
            "orders": [
                {
                    "action": "start",
                    "drug": "Exforge",
                    "dose": "5/160",
                    "frequency": "once daily",
                    "action_quote": "put him on",
                }
            ],
        },
    )
    w.tap("✅ Confirm")
    assert (
        w.claims.confirm(w.confirm_command(w.consent(w.claim(w.invite(patient))))).status
        == "accepted"
    )
    proposal = w.dictate(
        "Ahmed Test, change Exforge to 10/160 once daily, and add Forxiga 10 in the morning.",
        {
            "patient": {"name_as_spoken": "Ahmed Test"},
            "orders": [
                {
                    "action": "change",
                    "drug": "Exforge",
                    "dose": "10/160",
                    "frequency": "once daily",
                    "action_quote": "change",
                },
                {
                    "action": "start",
                    "drug": "Forxiga",
                    "dose": "10",
                    "timing": "in the morning",
                    "action_quote": "add",
                },
            ],
        },
        id=30,
    )
    clock.advance(timedelta(seconds=5))
    raw = w.button("✅ Confirm")
    if busy:
        lease = store.acquire_patient(
            patient.scope, "parallel-media", clock(), timedelta(minutes=5)
        )
        assert lease
        w.tap(raw=raw, id=31)
        pending = w.scribe.repo.load(w.doctor.scope, "scribe_proposal", proposal.id, Proposal)
        assert pending and pending.status == "pending"
        assert w.receipt(31).state == "pending" and w.receipt(31).processing_claim is None
        store.release_patient(lease)
    w.tap(raw=raw, id=31)
    saved = w.scribe.repo.load(w.doctor.scope, "scribe_proposal", proposal.id, Proposal)
    assert saved and saved.status == "confirmed", (saved, proposal.issues)
    versions = store.list_records(patient.scope, "care_order_version")[0]
    assert len(versions) == 3
    instructions = [
        r.body["structured_instruction"]
        for r in versions
        if isinstance(r.body["structured_instruction"], dict)
    ]
    assert any(i["drug"] == "Exforge" and i["dose"] == "10/160" for i in instructions)
    assert any(i["drug"] == "Forxiga" and i["dose"] == "10" for i in instructions)
