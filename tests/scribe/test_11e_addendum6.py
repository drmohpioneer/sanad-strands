"""The four measured card defects, including negative boundaries."""

import pytest

from sanad.scribe.alerts import explicitly_requested, prepare_alerts
from sanad.scribe.changes import combine_changes, drop_bare_continues
from sanad.scribe.extract import (
    DictationCandidate,
    MissionCandidate,
    OrderCandidate,
    ProposalIssue,
    candidate_issues,
    missing_request,
)
from sanad.scribe.resolver import Context


@pytest.mark.parametrize("action", ["start", "change", "stop"])
def test_bare_continue_does_not_duplicate_an_instruction(action: str) -> None:
    active = OrderCandidate.model_validate({"action": action, "drug": "Concor", "dose": "5"})
    value = DictationCandidate(orders=(OrderCandidate(action="continue", drug="Concor"), active))
    assert combine_changes(value, "Concor 5", Context()).orders == (active,)


@pytest.mark.parametrize("extra", [{"dose": "10"}, {"frequency": "daily"}, {"duration": "5 days"}])
def test_nonbare_continue_is_not_silently_removed(extra: dict[str, str]) -> None:
    value = DictationCandidate.model_validate(
        {
            "orders": [
                {"action": "continue", "drug": "Concor", **extra},
                {"action": "start", "drug": "Concor", "dose": "5"},
            ]
        }
    )
    assert combine_changes(value, "Concor 5", Context()).orders == value.orders


def test_standalone_continue_and_other_brand_keep_their_behavior() -> None:
    value = DictationCandidate.model_validate(
        {
            "orders": [
                {"action": "continue", "drug": "Concor"},
                {"action": "start", "drug": "Forxiga"},
            ]
        }
    )
    assert combine_changes(value, "Taking Concor. Add Forxiga.", Context()).orders == value.orders


@pytest.mark.parametrize(
    "source",
    [
        "Blood pressure chart, three times a day for five days. Request sodium.",
        "سجل الضغط ثلاث مرات في اليوم لمدة خمس ايام وطلبت سوديوم",
    ],
)
def test_test_mission_does_not_hide_missing_monitoring_task(source: str) -> None:
    value = DictationCandidate(missions=(MissionCandidate(kind="TEST", text="sodium"),))
    assert missing_request(value, source)
    complete = value.model_copy(
        update={"missions": (*value.missions, MissionCandidate(kind="TASK", text=source))}
    )
    assert not missing_request(complete, source)


@pytest.mark.parametrize(
    "source,alert,allowed",
    [
        ("BP is high 150/90", "BP is high 150/90", False),
        ("BP is high 150/90", "Notify me if BP is high 150/90", False),
        ("Tell me if BP is high 150/90", "BP is high 150/90", True),
        ("If BP is high 150/90, notify me.", "BP is high 150/90", True),
        ("Never notify me if BP is high", "BP is high", False),
        ("If BP is high, do not notify me.", "BP is high", False),
        ("Notify me if no BP is recorded", "BP is recorded", False),
        ("Notify me if BP > 150", "BP < 150", False),
        ("Notify me if K is high. BP is high 150/90.", "BP is high 150/90", False),
        ("BP is high 150/90. Notify me if K is high.", "BP is high 150/90", False),
        ("الضغط عالي 150/90", "الضغط عالي 150/90", False),
        ("قوللي لو الضغط عالي 150/90", "الضغط عالي 150/90", True),
        ("بلّغني لو الضغط تحت 90", "الضغط تحت 90", True),
        ("لو البوتاسيوم عدى 5.5 قولي فوراً.", "البوتاسيوم عدى 5.5", True),
        ("والضغط لو نزل تحت 90 يتصل بيا.", "الضغط نزل تحت 90", True),
        ("ما تقوللي لو الضغط عالي", "الضغط عالي", False),
    ],
)
def test_alert_requires_its_own_explicit_condition(source: str, alert: str, allowed: bool) -> None:
    assert explicitly_requested(alert, source) == allowed


def test_alert_filter_remaps_metadata_and_does_not_erase_unsupported_digits() -> None:
    c = DictationCandidate(alerts=("BP is high 200", "K above 6"))
    c._single_source = ("alert:0", "alert:1")
    c._merge_issues = (ProposalIssue(item="alert:1", code="extraction_conflict"),)
    result = prepare_alerts(c, "BP is high 150. Tell me if K above 6.")
    assert result.alerts == ("K above 6",) and result._single_source == ("alert:0",)
    assert result._merge_issues[0].item == "alert:0"
    assert any(
        i.code == "unsupported_number" and i.numbers == ("200",)
        for i in candidate_issues(result, "BP is high 150. Tell me if K above 6.")
    )


def test_explicit_alert_with_wrong_number_still_reaches_numeric_guard() -> None:
    source = "Notify me if BP above 100"
    result = prepare_alerts(DictationCandidate(alerts=("BP above 200",)), source)
    assert result.alerts == ("BP above 200",)
    assert any(
        i.item == "alert:0" and i.code == "unsupported_number"
        for i in candidate_issues(result, source)
    )


def test_removed_continue_question_and_surviving_indices_stay_aligned() -> None:
    c = DictationCandidate.model_validate(
        {
            "orders": [
                {"action": "continue", "drug": "Concor"},
                {"action": "start", "drug": "Concor", "dose": "5"},
                {"action": "start", "drug": "Forxiga"},
            ]
        }
    )
    c._single_source = ("order:0", "order:2")
    c._merge_issues = (
        ProposalIssue(item="order:0", code="fact_medication"),
        ProposalIssue(item="order:2", code="dose_missing"),
    )
    result, _ = drop_bare_continues(c, "Concor 5. Add Forxiga.", Context())
    assert result._single_source == ("order:1",)
    assert [(i.item, i.code) for i in result._merge_issues] == [("order:1", "dose_missing")]


def test_unrelated_task_cannot_satisfy_monitoring_request() -> None:
    c = DictationCandidate(missions=(MissionCandidate(kind="TASK", text="Call the clinic"),))
    assert missing_request(c, "Blood pressure chart, three times a day for five days")
