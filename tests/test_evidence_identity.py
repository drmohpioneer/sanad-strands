"""Literal identity and unit expectations for binding addenda 2 and 3."""

import pytest
from domain_fixtures import NOW
from evidence_cases import evidence, objective, read

from sanad.evidence.classify import document_identity, identity_outcome
from sanad.evidence.evaluate import evaluate
from sanad.evidence.grading import meaningful_disagreements
from sanad.media.vision import Disagreement, PrintedIdentityHint


@pytest.mark.parametrize(
    "first,second,name,expected",
    [
        ("Synthetic Patient", "Synthetic Patient", "Synthetic Patient", "match"),
        ("Dr. Ahmed Saleh", "Ahmed Saleh", "Ahmed Saleh", "match"),
        ("Ahmed Saleh", "Unrelated Person", "Ahmed Saleh", "match"),
        (None, "Ahmed Saleh", "Ahmed Saleh", "match"),
        ("Ahmed Saleh", "Ahmed Saleh", "أحمد صالح / Ahmed Saleh", "match"),
        ("John Smith", "John Smith", "Ahmed Saleh", "mismatch"),
        ("John Smith", "Jon Smith", "Ahmed Saleh", "mismatch"),
        ("John Smith", "Jane Doe", "Ahmed Saleh", "unverifiable"),
        ("John Smith", None, "Ahmed Saleh", "unverifiable"),
        ("", "", "Ahmed Saleh", "unverifiable"),
        (None, None, "Ahmed Saleh", "unverifiable"),
        ("أحمد س.", "أحمد س.", "أحمد س.", "unverifiable"),
        ("iزاء و.", "العواد الدوب", "Ahmed Saleh", "unverifiable"),
        ("John Smith", "John سمث", "Ahmed Saleh", "unverifiable"),
        ("AI Nour Lab", "Al Nour Lab", "Ahmed Saleh", "unverifiable"),
        ("Saleh Clinic", "Saleh Clinic", "Ahmed Saleh", "unverifiable"),
        ("[unreadable]", "[unreadable]", "Ahmed Saleh", "unverifiable"),
        ("123", "123", "Ahmed Saleh", "unverifiable"),
        ("José Silva", "José Silva", "Ahmed Saleh", "mismatch"),
        ("Ιωάννης", "Ιωάννης", "Ahmed Saleh", "unverifiable"),
    ],
)
def test_three_valued_identity(first, second, name, expected) -> None:  # type: ignore[no-untyped-def]
    assert identity_outcome(first, second, name) == expected


def test_explicit_document_header_is_not_a_patient_name() -> None:
    result = read(
        printed_identity_hint=PrintedIdentityHint(text="Al Nour"),
        notes=("Clinic name: Al Nour",),
    )
    assert document_identity(result, "Synthetic Patient") == "unverifiable"


def test_empty_and_missing_unit_do_not_disagree_but_neither_covers() -> None:
    difference = Disagreement(field="items.0.unit", first="", second=None)
    assert meaningful_disagreements((difference,)) == ()
    assert meaningful_disagreements(
        (Disagreement(field="items.0.unit", first="mmol/L", second=None),)
    )
    for unit in ("", None):
        result = evaluate(objective(), evidence(unit=unit, disagreements=(difference,)), (), NOW)
        assert not result.satisfied and result.missing == ("K", "Creatinine")
