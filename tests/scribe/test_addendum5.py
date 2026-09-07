"""Measured embedded strengths, verified proposals and duplicate-history removal."""

import logging

import pytest

from sanad.scribe.extract import (
    CORRECTION_PROMPT,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    DictationCandidate,
    candidate_issues,
)
from sanad.scribe.names import prepare_names, resolve, split_drug_dose


@pytest.mark.parametrize(
    "drug,name,dose",
    [
        ("إكس فورش إتش سي تي 560 12.5", "Exforge HCT", "560 12.5"),
        ("كونكور 5", "Concor", "5"),
        ("كونكور 5mg", "Concor", "5mg"),
        ("كونكور ٥ مج", "Concor", "5 مج"),
        ("كونكور 5 مج،", "Concor", "5 مج،"),
        ("Exforge HCT 5/160/12.5 mg", "Exforge HCT", "5/160/12.5 mg"),
        ("كونكور mg", "Concor", "mg"),
    ],
)
def test_embedded_strength_becomes_dose_before_exact_resolution(
    drug: str, name: str, dose: str
) -> None:
    value = DictationCandidate.model_validate({"orders": [{"action": "continue", "drug": drug}]})
    prepared, issues = prepare_names(value, drug)
    assert prepared.orders[0].drug == name
    assert prepared.orders[0].dose == dose
    assert not any(i.code == "drug_unclear" for i in issues)
    if dose == "560 12.5":
        assert len(issues) == 1 and issues[0].blocked
        assert issues[0].question == 'سمعت "560 12.5" لـ Exforge HCT، قصدك 5/160/12.5؟'
    assert not any(i.code == "unsupported_number" for i in candidate_issues(prepared, drug))
    assert prepare_names(prepared, drug)[0] == prepared


@pytest.mark.parametrize("drug", ["B12", "X4 HCT", "Forxiga", "دواء مجهول", "5 mg"])
def test_cleaning_preserves_internal_digits_and_requires_a_name(drug: str) -> None:
    assert split_drug_dose(drug) == (drug, "")


def test_existing_dose_wins_and_cleaning_cannot_hide_an_unsupported_number() -> None:
    value = DictationCandidate.model_validate(
        {"orders": [{"action": "continue", "drug": "كونكور 99 mg", "dose": "5 مج"}]}
    )
    prepared, issues = prepare_names(value, "كونكور 5 مج")
    assert prepared.orders[0].drug == "Concor" and prepared.orders[0].dose == "5 مج"
    assert any(i.code == "unsupported_number" and i.numbers == ("99",) for i in issues)


@pytest.mark.parametrize("dose", [None, "", "   "])
def test_empty_dose_inherits_literal_fragment(dose: str | None) -> None:
    value = DictationCandidate.model_validate(
        {"orders": [{"action": "continue", "drug": "كونكور 5", "dose": dose}]}
    )
    prepared, _ = prepare_names(value, "كونكور 5")
    assert prepared.orders[0].dose == "5"
    assert any(
        i.code == "disputed_number" and i.item == "order:0"
        for i in candidate_issues(prepared, "كونكور 5", ("5",))
    )


def test_model_name_is_verified_and_conflicting_or_unverified_names_still_ask() -> None:
    assert resolve("اسم غير مدرج تماما", "اسم غير مدرج تماما", "Concor").latin == "Concor"
    assert resolve("اسم غير مدرج تماما", "اسم غير مدرج تماما", "Inventedbrand").latin is None
    assert resolve("اسم غير مدرج تماما", "Inventedbrand", "Inventedbrand").latin == "Inventedbrand"
    assert resolve("كونكور", "كونكور", "Forxiga").conflict
    assert resolve("اسم غير مدرج تماما", "", "Concor", "dapagliflozin").conflict
    value = DictationCandidate.model_validate(
        {"orders": [{"action": "continue", "drug": "كونكور 5", "name_latin": "Inventedbrand"}]}
    )
    prepared, issues = prepare_names(value, "كونكور 5")
    assert prepared.orders[0].drug == "كونكور" and prepared.orders[0].dose == "5"
    assert any(i.code == "drug_unclear" and i.question for i in issues)


def test_duplicate_history_uses_cleaned_spoken_name_and_logs_one_redacted_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    value = DictationCandidate.model_validate(
        {
            "orders": [{"action": "continue", "drug": "كُـونكور 5"}],
            "facts": [
                {"category": "medication_history", "text": "كان بياخد كونكور"},
                {"category": "medication_history", "text": "كونكور 5 مج"},
                {"category": "medication_history", "text": "كان بياخد اسبوسيد"},
                {"category": "condition", "text": "ضغط"},
                {"category": "history", "text": "ذكر كونكور"},
            ],
        }
    )
    with caplog.at_level(logging.INFO, logger="sanad.scribe.names"):
        prepared, _ = prepare_names(value, "كُـونكور 5")
    assert prepared.facts == value.facts[2:]
    assert [r.getMessage() for r in caplog.records] == ["scribe_medication_history_dropped count=2"]
    assert "كونكور" not in caplog.text and "اسبوسيد" not in caplog.text
    assert len(value.facts) == 5 and value.orders[0].drug == "كُـونكور 5"
    assert not any(i.code == "unassigned_number" for i in candidate_issues(prepared, "كونكور 5"))


def test_v6_requests_model_knowledge_separately_from_the_spoken_drug() -> None:
    assert PROMPT_VERSION == "scribe-v7"
    for prompt in (SYSTEM_PROMPT, CORRECTION_PROMPT):
        assert "drug keeps the spoken form" in prompt
        assert "also fill name_latin" in prompt
        assert "from your own knowledge" in prompt
        assert "code verifies name_latin" in prompt
        assert "Preserve Egyptian Arabic and English drug names as spoken" not in prompt
        assert "{" not in prompt and "5/160/12.5" not in prompt


@pytest.mark.parametrize(
    "source,code", [("كونكور 5", "unsupported_number"), ("كونكور 5 زمان 99", "unassigned_number")]
)
def test_history_removal_cannot_hide_unplaced_or_unsupported_digits(source: str, code: str) -> None:
    value = DictationCandidate.model_validate(
        {
            "orders": [{"action": "continue", "drug": "كونكور 5"}],
            "facts": [{"category": "medication_history", "text": "كونكور 99"}],
        }
    )
    prepared, _ = prepare_names(value, source)
    assert prepared.facts == ()
    assert any(i.code == code and i.numbers == ("99",) for i in candidate_issues(prepared, source))


def test_long_numeric_text_followed_by_prose_is_not_a_dose_suffix() -> None:
    drug = "اسم " + "5" * 1000 + " كلام"
    assert split_drug_dose(drug) == (drug, "")
