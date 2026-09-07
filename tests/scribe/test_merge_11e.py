"""Two readings preserve unique evidence and block each conflicting field."""

from typing import Any

import pytest

from sanad.scribe.extract import DictationCandidate, candidate_issues
from sanad.scribe.merge import merge_candidates


def value(family: str, items: list[Any]) -> DictationCandidate:
    return DictationCandidate.model_validate({family: items})


ROWS: tuple[tuple[str, Any, Any, str], ...] = (
    (
        "orders",
        {"action": "continue", "drug": "كونكور", "dose": "5"},
        {"action": "continue", "drug": "Concord", "dose": "10"},
        "كونكور 5 أو 10",
    ),
    (
        "facts",
        {"category": "history", "text": "EF 45%"},
        {"category": "history", "text": "EF 50%"},
        "EF 45% أو EF 50%",
    ),
    (
        "missions",
        {"kind": "TEST", "text": "BUN, Na"},
        {"kind": "TEST", "text": "BUN, K"},
        "طلبت BUN, Na, K",
    ),
    ("alerts", "potassium above 5", "potassium above 6", "potassium above 5 or 6"),
)


@pytest.mark.parametrize("family,left,right,source", ROWS)
@pytest.mark.parametrize("mode", ["both", "one", "conflict"])
def test_merge_table(family: str, left: Any, right: Any, source: str, mode: str) -> None:
    a = value(family, [left])
    b = value(family, [] if mode == "one" else [left if mode == "both" else right])
    result = merge_candidates(a, b, source)
    assert result is not None
    assert len(getattr(result.candidate, family)) == 1
    item = {"orders": "order", "facts": "fact", "missions": "mission", "alerts": "alert"}[
        family
    ] + ":0"
    assert result.single_source == (
        (item,) if mode == "one" or (family == "facts" and mode == "conflict") else ()
    )
    assert len(result.issues) == (1 if mode == "conflict" and family != "facts" else 0)
    assert all(
        i.blocked and i.item == item and i.code == "extraction_conflict" for i in result.issues
    )


def test_resolved_aliases_equal_analyte_sets_and_spoken_order() -> None:
    a = value("missions", [{"kind": "TEST", "text": "بانو كريات وسوديوم"}])
    b = value("missions", [{"kind": "TEST", "text": "Na, creatinine, BUN"}])
    merged = merge_candidates(a, b, "بانو كريات وسوديوم")
    assert merged and not merged.issues and not merged.single_source
    a = value("facts", [{"category": "history", "text": "angina"}])
    b = value("facts", [{"category": "history", "text": "EF 45%"}])
    result = merge_candidates(a, b, "EF 45% then angina")
    assert result and [f.text for f in result.candidate.facts] == ["angina"]
    assert result.single_source == ("fact:0",)


def test_changed_fields_ask_but_secondary_mission_does_not_replace_primary() -> None:
    a = value("orders", [{"action": "continue", "drug": "كونكور", "dose": "5"}])
    b = DictationCandidate.model_validate(
        {
            "orders": [{"action": "stop", "drug": "كونكور", "dose": "10"}],
            "missions": [{"kind": "TEST", "text": "BUN"}],
        }
    )
    result = merge_candidates(a, b, "كونكور 5 وقف 10 وطلبت BUN")
    assert result and len(result.issues) == 2
    assert result.single_source == ()
    from sanad.scribe.extract import missing_request

    assert missing_request(result.candidate, "طلبت BUN")


def test_failures_and_discarded_unsupported_digits() -> None:
    a = value("orders", [{"action": "continue", "drug": "كونكور", "dose": "5"}])
    assert merge_candidates(None, None, "كونكور 5") is None
    for first, second in ((a, None), (None, a)):
        merged = merge_candidates(first, second, "كونكور 5")
        assert merged and merged.single_source == ("order:0",) and not merged.issues
    b = value("orders", [{"action": "continue", "drug": "كونكور", "dose": "99"}])
    merged = merge_candidates(a, b, "كونكور 5")
    assert merged and merged.candidate._dropped_numbers == ("99",)
    assert any(
        i.code == "unsupported_number" and i.blocked
        for i in candidate_issues(merged.candidate, "كونكور 5")
    )


def test_malformed_repeated_number_still_discloses_the_dropped_item() -> None:
    raw = {
        "orders": [
            {"action": "start", "drug": "كونكور", "dose": "5"},
            {"action": "bad", "drug": "فورسيجا", "dose": "5"},
        ]
    }
    a = DictationCandidate.model_validate(raw)
    result = merge_candidates(a, a, "كونكور 5 فورسيجا 5")
    assert result and result.candidate._dropped_numbers == ("5",)
    assert any(
        i.code == "unassigned_number" and i.numbers == ("5",)
        for i in candidate_issues(result.candidate, "كونكور 5 فورسيجا 5")
    )
