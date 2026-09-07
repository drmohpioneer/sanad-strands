"""English transcription, primary-only clinical content and numeric-safe normalization."""

import asyncio
import re

import pytest
from providers.fixtures import SOURCE, ScriptedConverter, ScriptedSpeech

from sanad.media.speech import (
    ENGLISH_VERBATIM_PROMPT,
    SpeechAdapter,
    Transcript,
    normalize_transcript,
)
from sanad.scribe.extract import (
    DictationCandidate,
    EnglishDictationCandidate,
    candidate_issues,
    scribe_prompt,
)
from sanad.scribe.merge import merge_candidates
from sanad.scribe.monitoring import task_instruction
from sanad.scribe.resolver import hint_names


@pytest.mark.parametrize(
    "spoken,written",
    [
        ("10 over 160 slash 25", "10/160/25"),
        ("120 by 80, EF 45 percent", "120/80, EF 45%"),
        ("12 point 5", "12.5"),
        ("over there by noon, point to it", "over there by noon, point to it"),
        ("five over ten", "five over ten"),
        ("1 Over 2 BY 3 slash 4", "1/2/3/4"),
    ],
)
def test_english_separator_normalization_never_adds_or_removes_digits(
    spoken: str, written: str
) -> None:
    actual = normalize_transcript(spoken, "en")
    assert actual == written
    assert re.findall(r"\d", actual) == re.findall(r"\d", spoken)
    assert normalize_transcript(spoken, "ar") == spoken


def test_english_speech_prompt_provenance_and_numbers_use_the_same_hint() -> None:
    caller = ScriptedSpeech("Exforge HCT 10 over 160 over 25. EF 45 percent. NUMBERS: 10 160 25 45")
    result = asyncio.run(
        SpeechAdapter(caller, ScriptedConverter(), SOURCE).transcribe(b"OggS", "ogg")
    )
    assert isinstance(result, Transcript)
    assert result.text == "Exforge HCT 10/160/25. EF 45%."
    assert result.numbers == result.heard_numbers == ("10", "160", "25", "45")
    assert not result.disputed_numbers
    assert (
        result.prompt_version
        == result.provenance[0].prompt_version
        == "english-verbatim-numbers-v4"
    )
    assert caller.calls[0][1][-1] == {"text": ENGLISH_VERBATIM_PROMPT}
    assert "NUMBERS:" in ENGLISH_VERBATIM_PROMPT
    english = scribe_prompt(hint_names())
    arabic = scribe_prompt(hint_names(), language="ar")
    assert "Language: en" in english and "spoken English" in english
    assert "Language: ar" in arabic
    assert "clinical_en" in str(
        EnglishDictationCandidate.model_json_schema()["$defs"]["EnglishFactCandidate"]
    )
    assert (
        "clinical_en"
        not in DictationCandidate.model_json_schema()["$defs"]["FactCandidate"]["properties"]
    )


def test_secondary_history_missions_categories_and_questions_cannot_add_content() -> None:
    primary = DictationCandidate.model_validate(
        {
            "facts": [{"category": "condition", "text": "diabetes"}],
            "missions": [{"kind": "TEST", "text": "CBC"}],
        }
    )
    secondary = DictationCandidate.model_validate(
        {
            "facts": [
                {"category": "finding", "text": "diabetes"},
                {"category": "history", "text": "EF 99%"},
            ],
            "missions": [{"kind": "TEST", "text": "CBC"}, {"kind": "TASK", "text": "walk"}],
            "ambiguities": ["Which new disease?"],
        }
    )
    result = merge_candidates(primary, secondary, "diabetes, tests CBC")
    assert result and result.candidate.facts == primary.facts
    assert result.candidate.missions == primary.missions
    assert not result.issues and not result.candidate.ambiguities
    assert not result.candidate._dropped_numbers


def test_primary_subset_and_same_number_echo_facts_fold_without_category_questions() -> None:
    primary = DictationCandidate.model_validate(
        {
            "facts": [
                {"category": "finding", "text": "EF 45%"},
                {"category": "finding", "text": "Echo ejection fraction 45%"},
                {"category": "finding", "text": "T-wave inversion"},
                {"category": "finding", "text": "T-wave inversion in lateral leads"},
                {"category": "condition", "text": "diabetic"},
            ]
        }
    )
    result = merge_candidates(
        primary,
        primary,
        "EF 45% Echo ejection fraction 45% T-wave inversion in lateral leads diabetic",
    )
    assert result and [f.text for f in result.candidate.facts] == [
        "Echo ejection fraction 45%",
        "T-wave inversion in lateral leads",
        "diabetic",
    ]
    assert not result.issues


@pytest.mark.parametrize("empty", [None, "", "null", "none", " NULL "])
def test_sentinel_timing_is_absent_without_empty_alternative(empty: object) -> None:
    primary = DictationCandidate.model_validate(
        {
            "orders": [{"action": "start", "drug": "Forxiga", "dose": empty, "timing": empty}],
            "missions": [{"kind": "TEST", "text": "CBC", "timing_expression": empty}],
        }
    )
    other = DictationCandidate.model_validate(
        {
            "orders": [{"action": "start", "drug": "Forxiga", "timing": "tomorrow"}],
            "missions": [{"kind": "TEST", "text": "CBC", "timing_expression": "tomorrow"}],
        }
    )
    result = merge_candidates(primary, other, "Forxiga; tests CBC")
    assert result and result.candidate.orders[0].dose is None
    assert result.candidate.missions[0].timing_expression is None
    assert not result.issues
    assert [i.code for i in candidate_issues(result.candidate, "Forxiga; tests CBC")] == [
        "dose_missing"
    ]


def test_arabic_monitoring_words_do_not_become_invented_digits() -> None:
    assert (
        task_instruction("قياس ضغط الدم ثلاث مرات في اليوم لمدة خمس ايام")
        == "Blood pressure chart, three times a day for five days"
    )
