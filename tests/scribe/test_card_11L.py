"""Card correctness under scripted extraction, including negative evidence cases."""

import hashlib
from typing import Any

import pytest

from sanad.scribe.card import dictation_questions, medication_line
from sanad.scribe.extract import (
    CORRECTION_PROMPT_VERSION,
    PROMPT_VERSION,
    DictationCandidate,
    ProposalIssue,
    scribe_prompt,
)
from sanad.scribe.grounding import inventory, permits
from sanad.scribe.merge import merge_candidates
from scribe.test_grounding_invariant import world_for


@pytest.mark.parametrize(
    "action", ["add", "begin", "put on", "hold", "discontinue", "increase", "decrease", "switch"]
)
def test_unknown_action_is_a_doubt_never_an_inferred_order(action: str) -> None:
    value = DictationCandidate.model_validate(
        {"orders": [{"action": action, "drug": "Forxiga", "dose": "10"}]}
    )
    assert value.orders == ()
    assert value.ambiguities == (f"{action} Forxiga",)
    assert value._malformed_items
    assert value._dropped_numbers == ("10",)


@pytest.mark.parametrize(
    "item",
    [
        {"action": "add", "drug": "Forxiga", "dose": {}},
        {"drug": "Forxiga"},
        {"action": "add", "drug": None},
    ],
)
def test_non_action_failures_keep_the_existing_malformed_path(item: dict[str, Any]) -> None:
    value = DictationCandidate.model_validate({"orders": [item]})
    assert value.orders == ()
    assert value.ambiguities == ()
    assert value._malformed_items


def test_both_readers_doubts_are_deduplicated_in_order() -> None:
    a = DictationCandidate(ambiguities=("first doubt", "shared doubt"))
    b = DictationCandidate.model_validate(
        {
            "ambiguities": ["shared doubt", "last doubt"],
            "orders": [{"action": "add", "drug": "Forxiga"}],
        }
    )
    merged = merge_candidates(a, b, "Add Forxiga")
    assert merged
    assert merged.candidate.ambiguities == (
        "first doubt",
        "shared doubt",
        "last doubt",
        "add Forxiga",
    )
    assert merged.candidate._malformed_items


@pytest.mark.parametrize("correction", [False, True])
def test_one_english_monitor_rule_and_preserved_arabic(correction: bool) -> None:
    assert PROMPT_VERSION == "scribe-v10"
    assert CORRECTION_PROMPT_VERSION == "scribe-correction-v9"
    english = scribe_prompt("Concor", language="en", correction=correction)
    assert english.startswith((CORRECTION_PROMPT_VERSION if correction else PROMPT_VERSION) + ".")
    assert english.count("MONITOR") == 1
    assert "any other measuring, recording or charting request is TASK, never TEST" in english
    assert "Never TEST or MONITOR" not in english
    assert "Allowed action values: start, stop, change, continue" in english
    assert ("Correct the previous card" in english) == correction
    arabic = scribe_prompt("Concor", language="ar", correction=correction)
    if correction:
        assert arabic.startswith(CORRECTION_PROMPT_VERSION + ".")
    preserved = arabic.replace("scribe-correction-v9", "scribe-correction-v8")
    assert hashlib.sha256(preserved.encode()).hexdigest() == (
        "e4b7fb060034bdcad0163ebb40bdbabebf177223d83cefbca6a9a78a59cf1384"
        if correction
        else "2c747cb847568563e48a9cc693a1ffd183f2379fd427eeeeeaa3c44704ca7b72"
    )


@pytest.mark.parametrize("supplied", [True, False])
@pytest.mark.usefixtures("legacy_dictation_schema")
def test_frequency_requires_valid_field_evidence(supplied: bool) -> None:
    world = world_for("en")
    p = world.dictate(
        "New patient Synthetic Person. Start Forxiga 10" + (" twice daily." if supplied else "."),
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [
                {"action": "start", "drug": "Forxiga", "dose": "10", "frequency": "twice daily"}
            ],
        },
    )
    assert p.candidate.orders[0].frequency == ("twice daily" if supplied else None)
    assert ("twice daily" in medication_line(p, 0)) == supplied
    if supplied:
        assert permits(p, "order:0", "frequency")
        p = p.model_copy(
            update={"evidence": tuple(e for e in p.evidence if e.field != "frequency")}
        )
        assert not permits(p, "order:0", "frequency")
        assert "twice daily" not in medication_line(p, 0)
        assert p.blocked("order:0")


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_scoped_inherited_prior_frequency_is_displayed() -> None:
    world = world_for("en")
    world.named_stub("Synthetic Person")
    world.dictate(
        "Synthetic Person. Taking Concor 5 daily.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "continue", "drug": "Concor", "dose": "5", "frequency": "daily"}],
        },
        id=8,
    )
    world.tap("✅ Confirm", id=9)
    p = world.dictate(
        "Synthetic Person. Change Concor to 10.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "change", "drug": "Concor", "dose": "10"}],
        },
    )
    assert "daily" not in p.source_text
    assert p.candidate.orders[0].frequency == "daily"
    assert permits(p, "order:0", "frequency")
    assert any(e.field == "frequency" and e.origin == "stored_prior_order" for e in p.evidence)
    assert "daily" in medication_line(p, 0)


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_patient_alternatives_leave_arabic_question_and_fact_inventory_unchanged() -> None:
    world = world_for("en")
    p = world.dictate(
        "New patient Synthetic Person. Start Forxiga 10.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "start", "drug": "Forxiga", "dose": "10"}],
        },
    )
    second = p.candidate.model_copy(
        update={
            "patient": p.candidate.patient.model_copy(
                update={"name_as_spoken": "Synthetic Pearson"}
            )
        }
    )
    merged = merge_candidates(p.candidate, second, p.source_text)
    assert merged
    issue = next(i for i in merged.issues if i.item == "patient")
    assert issue.question == 'سمعت بيانات المريض بقراءتين مختلفتين؛ تؤكد "Synthetic Person"؟'
    shown = p.model_copy(update={"issues": (issue,)})
    assert dictation_questions(shown) == (
        'I heard the patient as "Synthetic Person" and as "Synthetic Pearson"; which is right?',
    )
    arabic = shown.model_copy(update={"language": "ar"})
    legacy = arabic.model_copy(
        update={"issues": (issue.model_copy(update={"alternatives": None}),)}
    )
    assert dictation_questions(arabic) == dictation_questions(legacy)
    assert inventory(shown) == inventory(p)


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_indexed_unsafe_ambiguity_is_excluded_in_both_languages() -> None:
    world = world_for("en")
    p = world.dictate(
        "New patient Synthetic Person. Start Forxiga 10.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "start", "drug": "Forxiga", "dose": "10"}],
        },
    )
    p = p.model_copy(
        update={
            "candidate": p.candidate.model_copy(
                update={"ambiguities": ("unsafe disputed phrase", "safe disputed phrase")}
            ),
            "issues": (ProposalIssue(item="ambiguity:0", code="unsafe_text"),),
        }
    )
    for language in ("en", "ar"):
        questions = "\n".join(dictation_questions(p.model_copy(update={"language": language})))
        assert "unsafe disputed phrase" not in questions
        assert "safe disputed phrase" in questions


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_retained_frequency_without_evidence_asks_and_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sanad.scribe import grounding

    original = grounding._record

    def missing_frequency(claim: Any, *args: Any, **kwargs: Any) -> Any:
        return None if claim.field == "frequency" else original(claim, *args, **kwargs)

    monkeypatch.setattr(grounding, "_record", missing_frequency)
    world = world_for("en")
    p = world.dictate(
        "New patient Synthetic Person. Start Forxiga 10 twice daily.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [
                {"action": "start", "drug": "Forxiga", "dose": "10", "frequency": "twice daily"}
            ],
        },
    )
    assert p.candidate.orders[0].frequency == "twice daily"
    assert not permits(p, "order:0", "frequency")
    assert "twice daily" not in medication_line(p, 0)
    assert any("frequency" in q for q in dictation_questions(p))
    assert p.blocked("order:0")


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_reply_added_evidence_cannot_be_constructed_against_stale_previous_version() -> None:
    from sanad.scribe.grounding import Claim, _correction_record
    from sanad.scribe.resolver import Context

    world = world_for("en")
    world.named_stub("Synthetic Person")
    previous = world.dictate(
        "Synthetic Person. Add Forxiga.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "add", "drug": "Forxiga"}],
        },
        id=10,
    )
    changed = world.dictate(
        "start Forxiga 10",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "start", "drug": "Forxiga", "dose": "10"}],
        },
        id=11,
    )
    claim = Claim("order:0", "dose", "10")
    handled, evidence = _correction_record(claim, changed, previous, Context())
    assert handled and evidence
    assert _correction_record(
        claim, changed, previous.model_copy(update={"version": previous.version + 1}), Context()
    ) == (True, None)
    assert _correction_record(
        claim, changed, previous.model_copy(update={"id": "different-card"}), Context()
    ) == (True, None)
