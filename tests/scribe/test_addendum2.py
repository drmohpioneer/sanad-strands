"""Regressions for the measured Scribe failures and code-owned coverage."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from providers.fixtures import ScriptedModel, candidate
from providers.test_agents import agent

from sanad.agents.factory import Proposal, propose
from sanad.agents.schema import describe_schema
from sanad.scribe.card import arabic_datetime
from sanad.scribe.extract import (
    CORRECTION_PROMPT,
    DictationCandidate,
    candidate_issues,
    derive_intent,
    extracted_numbers,
)


def test_schema_ignores_model_bookkeeping_and_unknown_keys_at_every_level() -> None:
    value = DictationCandidate.model_validate(
        {
            "intent": "orders",
            "numbers_used": ["2"],
            "patient_id": "foreign",
            "patient": {"name_as_spoken": "أحمد", "patient_id": "foreign"},
            "orders": [{"action": "start", "drug": "أملوديبين", "dose": "5 مج", "id": "foreign"}],
            "facts": [{"category": "allergy", "text": "بنسلين", "approved": True}],
            "missions": [{"kind": "TEST", "text": "تحليل سكر", "deadline": "999"}],
        }
    )
    description = describe_schema(DictationCandidate)
    assert all(name not in description for name in ("intent", "numbers_used", "_dropped_numbers"))
    assert "foreign" not in value.model_dump_json() and "999" not in value.model_dump_json()
    assert not candidate_issues(value, "أحمد أملوديبين 5 مج وبنسلين وتحليل سكر بعد أسبوعين")
    assert "scribe-correction-v9" in CORRECTION_PROMPT
    assert "correct the previous card" in CORRECTION_PROMPT.lower()


@pytest.mark.parametrize("has_match", [False, True])
@pytest.mark.parametrize(
    "raw,without_match,with_match",
    [
        ({}, "unclear", "unclear"),
        ({"patient": {"name_as_spoken": "أحمد"}}, "find_patient", "find_patient"),
        ({"orders": [{"action": "stop", "drug": "أملوديبين"}]}, "create_patient", "update_record"),
        ({"facts": [{"category": "history", "text": "عملية"}]}, "create_patient", "update_record"),
        ({"missions": [{"kind": "TEST", "text": "تحليل"}]}, "create_patient", "update_record"),
        ({"alerts": ["لو تعب بلغني"]}, "create_patient", "update_record"),
    ],
)
def test_intent_uses_clinical_content_and_server_lookup(
    raw: dict[str, object], without_match: str, with_match: str, has_match: bool
) -> None:
    value = DictationCandidate.model_validate(raw | {"intent": "create_patient"})
    assert derive_intent(value, has_match=has_match) == (with_match if has_match else without_match)


@pytest.mark.parametrize(
    "family,malformed",
    [
        ("orders", {"orders": [{"action": "start", "drug": "أملوديبين", "dose": "5 مج"}]}),
        ("orders", {"action": "start", "drug": "أملوديبين", "dose": {"value": "5"}}),
        ("orders", [{"action": "start", "drug": "أملوديبين", "dose": "5 مج"}]),
        ("missions", {"kind": "MONITOR", "text": "الضغط بعد 5 أيام"}),
        ("facts", {"category": "history", "text": {"invalid": "من 5 أيام"}}),
        ("alerts", {"text": "البوتاسيوم فوق 5"}),
    ],
)
def test_malformed_numeric_item_is_dropped_without_losing_valid_siblings(
    family: str, malformed: object
) -> None:
    raw: dict[str, Any] = {
        "facts": [{"category": "history", "text": "من 5 أيام"}],
        family: [malformed],
    }
    if family == "facts":
        raw["facts"].append({"category": "history", "text": "من 5 أيام"})
    model = ScriptedModel(candidate(raw))
    result = asyncio.run(
        propose("scribe", DictationCandidate, "من 5 أيام", agent=agent(model), want_spans=False)
    )
    assert isinstance(result, Proposal) and len(result.value.facts) == 1
    assert not result.value.orders and not result.value.missions and not result.value.alerts
    # Even a number repeated in a valid sibling must disclose the dropped instruction.
    assert extracted_numbers(result.value) == ("5",)
    issues = candidate_issues(result.value, "من 5 أيام")
    assert len(issues) == 1 and issues[0].code == "unassigned_number"
    assert issues[0].numbers == ("5",) and not issues[0].blocked
    assert len(model.script.calls) == 1


@pytest.mark.parametrize("malformed", [None, "وقف الدوا", {}, {"action": "wrong", "drug": "دواء"}])
def test_malformed_nonnumeric_item_stays_empty_without_placeholder_question(
    malformed: object,
) -> None:
    value = DictationCandidate.model_validate({"orders": [malformed]})
    assert value.orders == () and value.ambiguities == (
        ("wrong دواء",)
        if isinstance(malformed, dict) and malformed.get("action") == "wrong"
        else ()
    )
    assert any(q.code == "clarification" for q in candidate_issues(value, "وقف الدوا"))
    assert (
        DictationCandidate.model_validate_json(value.model_dump_json()).ambiguities
        == value.ambiguities
    )


def test_coverage_checks_all_clinical_fields_and_runs_alongside_blocked_items() -> None:
    value = DictationCandidate.model_validate(
        {
            "patient": {"name_as_spoken": "أحمد", "age": "60"},
            "orders": [
                {
                    "action": "start",
                    "drug": "أملوديبين",
                    "dose": "99 مج",
                    "frequency": "كل 8 ساعات",
                    "duration": "7 أيام",
                }
            ],
            "missions": [
                {"kind": "TEST", "text": "تحليل بعد 4 ساعات", "timing_expression": "بعد 4 ساعات"}
            ],
            "facts": [{"category": "history", "text": "من 3 أيام"}],
            "alerts": ["البوتاسيوم فوق 5.5"],
            "ambiguities": ["سمعت 90"],
            "numbers_used": ["90", "2"],
        }
    )
    issues = candidate_issues(
        value, "أحمد 60 سنة من 3 أيام 5 مج كل 8 ساعات 7 أيام تحليل بعد 4 ساعات و5.5 و90"
    )
    assert extracted_numbers(value) == ("60", "99", "8", "7", "4", "3", "5.5")
    assert [(i.code, i.numbers) for i in issues if i.code == "unsupported_number"] == [
        ("unsupported_number", ("99",))
    ]
    assert [i.numbers for i in issues if i.code == "unassigned_number"] == [
        ("5",),
        ("90",),
    ]
    assert all(not i.blocked for i in issues if i.code == "unassigned_number")


def test_spoken_word_numbers_are_not_converted_into_supported_digits() -> None:
    source = "أملوديبين 5 مج تلات مرات وتحليل بعد أسبوعين"
    raw: dict[str, Any] = {
        "orders": [
            {"action": "start", "drug": "أملوديبين", "dose": "5 مج", "frequency": "تلات مرات"}
        ],
        "missions": [{"kind": "TEST", "text": "تحليل", "timing_expression": "بعد أسبوعين"}],
        "numbers_used": ["3", "2"],
    }
    value = DictationCandidate.model_validate(raw)
    assert not candidate_issues(value, source)
    raw["orders"][0]["frequency"] = "3 مرات"
    changed = DictationCandidate.model_validate(raw)
    assert any(
        i.code == "unsupported_number" and i.numbers == ("3",)
        for i in candidate_issues(changed, source)
    )


@pytest.mark.parametrize(
    "instant,timezone,expected",
    [
        ("2026-09-20T12:00:00+00:00", "Africa/Cairo", "الأحد 20 سبتمبر، 3 العصر"),
        ("2026-09-06T21:05:30+00:00", "Africa/Cairo", "الاثنين 7 سبتمبر، 12:05 بعد منتصف الليل"),
        ("2026-09-06T09:00:00+00:00", "Africa/Cairo", "الأحد 6 سبتمبر، 12 الظهر"),
        ("2026-09-06T12:07:00+00:00", "UTC", "الأحد 6 سبتمبر، 12:07 الظهر"),
        ("2026-10-30T12:00:00+00:00", "Africa/Cairo", "الجمعة 30 أكتوبر، 2 الظهر"),
        ("2027-01-01T04:00:00+00:00", "Africa/Cairo", "الجمعة 1 يناير 2027، 6 الصبح"),
    ],
)
def test_arabic_dates_use_real_weekday_timezone_precision_and_year(
    instant: str, timezone: str, expected: str
) -> None:
    assert (
        arabic_datetime(
            datetime.fromisoformat(instant), timezone, reference=datetime(2026, 9, 6, tzinfo=UTC)
        )
        == expected
    )
