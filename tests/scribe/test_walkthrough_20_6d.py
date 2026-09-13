"""F01/F02 forged dictations: the persisted card must match accepted records."""

from typing import Any

import pytest
from store.account_fixtures import APPLICANT, update

from sanad.domain.language import Language
from sanad.scribe.card import render_card
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.records import CareOrderVersion
from sanad.store.records import from_record
from scribe.test_grounding_invariant import dictate, world_for

FIRST = (
    "Ahmed Test, put him on Exforge 5/160 once daily, we can hold the aspirin for now, "
    "request CBC and potassium, and check his pressure twice a day for three days."
)
SECOND = "Ahmed Test, change Exforge to 10/160 once daily, and add Forxiga 10 in the morning"


@pytest.mark.parametrize("expression", ["for 3 days", "twice a day for three days"])
def test_f01_f02_card_and_confirmation(expression: str) -> None:
    world = world_for("en")
    patient = world.named_stub("Ahmed Test")
    value: dict[str, Any] = {
        "patient": {"name_as_spoken": "Ahmed Test"},
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
                "timing_expression": expression,
            },
        ],
    }
    first = dictate(world, FIRST, value, id=1789171146)
    card = "\n".join(render_card(first))
    assert not first.issues, (card, first.issues)
    assert "Exforge 5/160, once daily (start)" in card
    assert "Aspirin (stop; doctor instructed, not on file before)" in card
    assert "TEST: CBC, K" in card and "MONITOR: blood pressure" in card
    assert "Needs confirmation:" not in card and "✅ Confirm" in card
    assert "2 times a day for 3 days" in card
    assert not world.store.list_records(patient.scope, "mission")[0]
    world.tap("✅ Confirm", id=1789171147)
    assert world.receipt(1789171147).state == "completed"
    rows = world.store.list_records(patient.scope, "mission")[0]
    assert sorted(str(r.body["kind"]) for r in rows) == [
        "MEDICATION",
        "MEDICATION",
        "MONITOR",
        "TEST",
    ]
    test = next(r for r in rows if r.body["kind"] == "TEST")
    assert isinstance(test.body["details"], dict)
    assert test.body["details"]["analytes"] == ["CBC", "K"]
    monitor = next(r for r in rows if r.body["kind"] == "MONITOR")
    assert isinstance(monitor.body["details"], dict)
    slots = monitor.body["details"]["slots"]
    assert isinstance(slots, list) and len(slots) == 6
    first_orders = [
        from_record(r, CareOrderVersion)
        for r in world.store.list_records(patient.scope, "care_order_version")[0]
    ]
    assert all(isinstance(v.structured_instruction, OrderCandidate) for v in first_orders)
    assert {
        (
            v.structured_instruction.drug,
            v.structured_instruction.dose,
            v.structured_instruction.action,
        )
        for v in first_orders
        if isinstance(v.structured_instruction, OrderCandidate)
    } == {("Exforge", "5/160", "start"), ("Aspirin", None, "stop")}
    assert all(v.provenance.source_observation_id == first.source_receipt_id for v in first_orders)
    second = dictate(
        world,
        SECOND,
        {
            "patient": {"name_as_spoken": "Ahmed Test"},
            "orders": [
                {
                    "action": "change",
                    "drug": "Exforge",
                    "dose": "10/160",
                    "frequency": "once daily",
                    "previous_drug": "Exforge",
                    "action_quote": "change",
                },
                {
                    "action": "start",
                    "drug": "Forxiga",
                    "dose": "10",
                    "frequency": "in the morning",
                    "timing": "in the morning",
                    "action_quote": "add",
                },
            ],
        },
        id=1789171150,
    )
    card = "\n".join(render_card(second))
    assert not second.issues, (card, second.issues)
    assert "Exforge 5/160 → Exforge 10/160, once daily (change)" in card
    assert "Forxiga 10, in the morning (start)" in card
    world.tap("✅ Confirm", id=1789171151)
    assert world.receipt(1789171151).state == "completed"
    orders = world.store.list_records(patient.scope, "care_order_head")[0]
    assert len(orders) == 3
    versions = world.store.list_records(patient.scope, "care_order_version")[0]
    assert len(versions) == 4
    final = [from_record(r, CareOrderVersion) for r in versions]
    new = [v for v in final if v.provenance.source_observation_id == second.source_receipt_id]
    assert {
        (
            v.structured_instruction.drug,
            v.structured_instruction.dose,
            v.structured_instruction.action,
        )
        for v in new
        if isinstance(v.structured_instruction, OrderCandidate)
    } == {("Exforge", "10/160", "change"), ("Forxiga", "10", "start")}
    assert (
        next(
            v
            for v in new
            if isinstance(v.structured_instruction, OrderCandidate)
            and v.structured_instruction.drug == "Exforge"
        ).supersedes_version
        == 1
    )
    assert "Not recorded:" not in str(world.transport.calls[-1].payload)
    world.post(update(APPLICANT, "/corrections", 1789171165))
    assert any(
        patient.id in str(c.payload) and "4 correctable records" in str(c.payload)
        for c in world.transport.calls
    )


@pytest.mark.parametrize("language", ["en", "ar"])
def test_blocked_order_only_appears_under_confirmation(language: Language) -> None:
    world = world_for(language)
    patient = world.named_stub("Ahmed Test")
    proposal = dictate(
        world,
        "Ahmed Test. Start Forxiga. Request CBC.",
        {
            "patient": {"name_as_spoken": "Ahmed Test"},
            "orders": [{"action": "start", "drug": "Forxiga", "action_quote": "Start"}],
            "missions": [{"kind": "TEST", "text": "CBC"}],
        },
    )
    assert proposal.blocked("order:0") and not proposal.blocked("mission:0")
    card = "\n".join(render_card(proposal))
    plain, needs = card.split("Needs confirmation:" if language == "en" else "محتاج تأكيد:")
    assert "Forxiga" not in plain and "TEST: CBC" in plain
    assert "Forxiga" in needs and ("dose" if language == "en" else "جرعة") in needs
    world.tap("✅ Confirm" if language == "en" else "✅ تمام", id=21)
    assert not world.store.list_records(patient.scope, "care_order_version")[0]
    missions = world.store.list_records(patient.scope, "mission")[0]
    assert len(missions) == 1 and missions[0].body["kind"] == "TEST"


@pytest.mark.parametrize("verb", ["check", "measure", "monitor"])
def test_test_clause_stops_at_scheduled_monitoring_verb(verb: str) -> None:
    from sanad.scribe.resolver import unresolved_test_fragments

    source = f"request CBC and potassium, and {verb} blood pressure twice a day for three days"
    assert unresolved_test_fragments(("CBC", "K"), source) == ()
    # A bare test name after check must not be erased as a monitoring request.
    assert unresolved_test_fragments(("CBC",), "request CBC and check potassium")
