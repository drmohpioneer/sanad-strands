"""Absent model containers do not introduce a blocking clarification."""

from typing import Any

from store.test_scribe_11L import confirmation

from sanad.scribe.extract import DictationCandidate, EnglishDictationCandidate, candidate_issues
from sanad.scribe.grounding import _fingerprint
from scribe.test_grounding_invariant import world_for


def assert_english_confirms(nulls: dict[str, Any]) -> None:
    world = world_for("en")
    patient = world.named_stub("Synthetic Person")
    raw = {
        "orders": [
            {"action": "start", "drug": "Forxiga", "dose": "10", "action_quote": "put him on"}
        ],
        **nulls,
    }
    reading = EnglishDictationCandidate.model_validate(raw)
    assert not reading._malformed_items
    assert not reading._dropped_numbers
    assert not reading.ambiguities
    world.dictate("put him on Forxiga 10", raw)
    world.tap("Synthetic Person", id=11)
    proposal = world.proposal
    assert proposal.selected_patient_id == patient.id
    assert not any(issue.code == "clarification" for issue in proposal.issues)
    assert not proposal.blocked("all") and not proposal.blocked("order:0")
    assert proposal.evidence_fingerprint == _fingerprint(proposal)
    assert confirmation(world, proposal) == "accepted"
    assert world.scribe.repo.pending(world.doctor.scope) is None
    assert len(world.store.list_records(patient.scope, "care_order_version")[0]) == 1


def test_null_patient_and_correction_edits_seal_and_confirm() -> None:
    assert_english_confirms({"patient": None, "correction_edits": None})


def test_null_facts_seal_and_confirm() -> None:
    assert_english_confirms({"facts": None})


def test_non_null_non_list_facts_remain_malformed() -> None:
    reading = EnglishDictationCandidate.model_validate({"facts": "x"})
    assert reading.facts == ()
    assert reading._malformed_items
    assert any(issue.code == "clarification" for issue in candidate_issues(reading, "x"))


def test_arabic_and_english_null_containers_equal_absent_defaults() -> None:
    raw = dict.fromkeys(
        ("patient", "facts", "orders", "missions", "alerts", "ambiguities", "correction_edits")
    )
    for schema in (DictationCandidate, EnglishDictationCandidate):
        reading = schema.model_validate(raw)
        assert reading == schema()
        assert not reading._malformed_items
        assert not reading._dropped_numbers
        assert not reading.ambiguities
        assert candidate_issues(reading, "") == candidate_issues(schema(), "")
    assert all(value is None for value in raw.values())
