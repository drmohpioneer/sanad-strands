import pytest

from sanad.scribe.commit import value_alert
from sanad.scribe.extract import DictationCandidate, candidate_issues


@pytest.mark.parametrize(
    "text,metric,threshold",
    [
        ("البوتاسيوم عدى 5.5", "potassium", "5.5"),
        ("sodium below 120", "sodium", "120"),
        ("LDL above 190", "LDL", "190"),
    ],
)
def test_measurable_alert_preserves_threshold_and_cannot_lower_floor(
    text: str, metric: str, threshold: str
) -> None:
    alert = value_alert(text)
    assert alert and alert.metric == metric and alert.threshold == threshold
    assert alert.floor_mode == "add_only"


def test_general_alert_is_not_promoted_to_a_value_threshold() -> None:
    assert value_alert("مش قادر يعمل التحليل") is None
    assert value_alert("راجعني بعد 5 أيام") is None


def test_western_digit_storage_keeps_exact_source_support() -> None:
    value = DictationCandidate.model_validate(
        {
            "intent": "update_record",
            "patient": {"name_as_spoken": "أحمد", "age": "٦٠"},
            "orders": [{"action": "start", "drug": "أملوديبين", "dose": "٢٫٥ مج"}],
            "numbers_used": ["٦٠", "٢٫٥"],
        }
    )
    assert value.patient.age == "60" and value.orders[0].dose == "2.5 مج"
    issues = candidate_issues(value, "أحمد ٦٠ سنة أملوديبين ٢٫٥ مج")
    # The addendum counts coverage in clinical fields, not demographic/identity fields.
    assert len(issues) == 1 and issues[0].code == "unassigned_number"
    assert issues[0].numbers == ("60",) and not issues[0].blocked
