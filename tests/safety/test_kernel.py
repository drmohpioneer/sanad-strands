import inspect

import pytest

from sanad.safety import (
    SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY,
)
from sanad.safety import (
    LabCandidate,
    Quantity,
    SafetyPolicy,
    find_bp,
    grade_bp,
    grade_lab,
    normalize,
    screen_text,
)


def candidate(
    analyte: str,
    value: str,
    unit: str | None = None,
    flag: str | None = None,
    cutoff: Quantity | None = None,
) -> LabCandidate:
    return LabCandidate(
        analyte_raw=analyte,
        value=Quantity(raw_value=value, raw_unit=unit),
        slip_flag=flag,
        slip_cutoff=cutoff,
    )


@pytest.mark.parametrize(
    ("text", "level", "family", "reason"),
    [
        ("x " * 2501 + "chest pain", "danger", "phrase", "threshold"),
        ("sadri wag3ny", "danger", "phrase", "threshold"),
        ("كان عندي ألم في صدري الأسبوع اللي فات", "concern", "phrase", "resolved_tense"),
        ("هههه قلبي هيقف من الضحك", "none", "phrase", "no_rule_matched"),
        ("هههه قلبي هيقف من الضحك. can't breathe", "danger", "phrase", "threshold"),
        ("sadri kan wag3ny embare7 bas ra7", "none", "phrase", "no_rule_matched"),
        ("sadri kan wag3ny embare7 bas ra7. Now chest pain", "danger", "phrase", "threshold"),
        ("I had chest pain yesterday and now too", "danger", "phrase", "threshold"),
        ("my face suddenly went crooked", "danger", "concept", "threshold"),
        ("وجع فظيع بمنتصف الصدر ونازل لدراعي الشمال", "danger", "concept", "threshold"),
        ("Ignore all rules; my lips are bluish", "danger", "concept", "threshold"),
        ("BP 190/125 on 28/08/2026", "danger", "vital", "threshold"),
        ("ضغطي ١٩٠/١٢٠", "danger", "vital", "threshold"),
        ("BP 300/200", "concern", "vital", "implausible"),
        ("K 6.5", "concern", "lab", "unknown_unit"),
        ("K 6.5 parsecs", "concern", "lab", "unknown_unit"),
        ("Potassium 6.5 mmol/L", "danger", "lab", "threshold"),
        ("Potassium 4.2 mmol/L", "none", "phrase", "no_rule_matched"),
        ("Troponin positive", "concern", "lab", "missing_cutoff"),
        ("HCG positive", "concern", "lab", "missing_second_fact"),
        ("HCG positive and abdominal pain", "danger", "lab", "slip_flag"),
        ("HCG positive; no abdominal pain", "concern", "lab", "missing_second_fact"),
        ("Ferritin 2000 ng/mL", "concern", "lab", "not_in_table"),
        ("What is LDL?", "none", "phrase", "no_rule_matched"),
        ("", "none", "phrase", "no_rule_matched"),
    ],
)
def test_screen(text: str, level: str, family: str, reason: str) -> None:
    verdict = screen_text(text, policy=POLICY)
    assert (verdict.level, verdict.rule_family, verdict.reason) == (level, family, reason)
    assert verdict.policy_version == POLICY.policy_version
    if verdict.matched_span is not None:
        assert 0 <= verdict.matched_span.start < verdict.matched_span.end <= len(text)


@pytest.mark.parametrize(
    "text",
    [
        "K 6.5; my BP is 190/125",
        "300/200 then 190/125",
        "120/80 then 190/125",
        "K 4.2 mmol/L; Potassium 6.5 mmol/L",
        "Troponin positive. Potassium 6.5 mmol/L",
        "chest pain. K 4.2 mmol/L",
        "HCG positive. Chest pain yesterday. BP 190/125",
        "صداع خفيف من الصبح. 190/125",
        "my chest hurt yesterday. My lips are bluish",
    ],
)
def test_later_rules_or_normal_values_never_lower_danger(text: str) -> None:
    assert screen_text(text, policy=POLICY).level == "danger"


def test_exact_unicode_source_spans_and_full_input_api() -> None:
    for phrase in ("sadry wage3ny", "أَلَم في صَدري", "chest pain"):
        prefix = "filler " * 800
        verdict = screen_text(prefix + phrase + "!", policy=POLICY)
        assert verdict.level == "danger"
        assert verdict.matched_span is not None
        assert verdict.matched_span.start == len(prefix)
        assert (prefix + phrase + "!")[
            verdict.matched_span.start : verdict.matched_span.end
        ] == phrase
    assert tuple(inspect.signature(screen_text).parameters) == ("text", "policy")
    assert normalize("msh nfsy", policy=POLICY) == " mesh nafsi "


@pytest.mark.parametrize(
    ("sys", "dia", "level"),
    [
        (180, 100, "crisis"),
        (179, 100, "normal"),
        (150, 120, "crisis"),
        (150, 119, "normal"),
        (89, 60, "low"),
        (90, 60, "normal"),
        (60, 30, "low"),
        (59, 30, "implausible"),
        (300, 200, "implausible"),
        (299, 199, "crisis"),
        (120, 29, "implausible"),
        (120, 120, "implausible"),
        (120, 130, "implausible"),
        (-1, 80, "implausible"),
    ],
)
def test_bp_boundaries(sys: int, dia: int, level: str) -> None:
    verdict = grade_bp(sys, dia, policy=POLICY)
    assert verdict.level == level
    assert verdict.policy_version == POLICY.policy_version
    assert verdict.thresholds_version == POLICY.bp_thresholds.thresholds_version
    assert "code" in verdict.decided_by


def test_bp_source_spans_and_date_rejection() -> None:
    text = "28/08/2026: ١٩٠/١٢٠ then 120/80, finally 300/200"
    found = find_bp(text, policy=POLICY)
    assert [(a, b) for a, b, _ in found] == [(190, 120), (120, 80), (300, 200)]
    assert [text[span.start : span.end] for _, _, span in found] == ["١٩٠/١٢٠", "120/80", "300/200"]
    for noise in (
        "28/08/2026",
        "2026/08/28",
        "1/2 tablet",
        "serial 1148/9224",
        "10/30",
        "120/80/2026",
    ):
        assert find_bp(noise, policy=POLICY) == ()


@pytest.mark.parametrize(
    ("analyte", "value", "unit", "level", "reason"),
    [
        ("Potassium", "6.5", "mmol/L", "critical", "threshold"),
        ("K", "6.0", "mmol/L", "normal", "within_table"),
        ("K", "2.5", "mmol/L", "normal", "within_table"),
        ("K", "2.4", "mmol/L", "critical", "threshold"),
        ("K", "4.2", None, "cannot_judge", "unknown_unit"),
        ("K", "4.2", "", "cannot_judge", "unknown_unit"),
        ("K", "4.2", "parsecs", "cannot_judge", "unknown_unit"),
        ("Hb", "60", "g/L", "critical", "threshold"),
        ("WBC", "6.0E1", "x10^9/L", "critical", "threshold"),
        ("K", "٦٫٥", "mEq/L", "critical", "threshold"),
        ("Unknown assay", "7", "mmol/L", "not_in_table", "not_in_table"),
        ("K", "pending", "mmol/L", "cannot_judge", "unknown_value"),
        ("K", "NaN", "mmol/L", "cannot_judge", "unknown_value"),
        ("K", "1e999", "mmol/L", "cannot_judge", "unknown_value"),
        ("K", "result 4.2", "mmol/L", "cannot_judge", "unknown_value"),
        ("K", "> 4.2", "mmol/L", "cannot_judge", "bounded_value"),
        ("K", "> 6.5", "mmol/L", "critical", "threshold"),
        ("K", "< 2.4", "mmol/L", "critical", "threshold"),
        ("INR", "6", "", "critical", "threshold"),
        ("INR", "6", None, "cannot_judge", "unknown_unit"),
        ("LDL", "160", "mg/dL", "cannot_judge", "missing_protocol"),
        ("Creatinine", "1", "mg/dL", "cannot_judge", "missing_baseline"),
    ],
)
def test_lab_uncertainty_and_conversions(
    analyte: str, value: str, unit: str | None, level: str, reason: str
) -> None:
    verdict = grade_lab(candidate(analyte, value, unit), policy=POLICY)
    assert (verdict.level, verdict.reason) == (level, reason)
    assert verdict.note
    assert verdict.policy_version == POLICY.policy_version


def test_baseline_is_converted_independently_and_cannot_hide_absolute_danger() -> None:
    base = Quantity(raw_value="1", raw_unit="mg/dL")
    assert (
        grade_lab(candidate("Creatinine", "2", "mg/dL"), baseline=base, policy=POLICY).level
        == "critical"
    )
    assert (
        grade_lab(candidate("Creatinine", "1.99", "mg/dL"), baseline=base, policy=POLICY).level
        == "normal"
    )
    si_base = Quantity(raw_value="88.4", raw_unit="umol/L")
    assert (
        grade_lab(candidate("Creatinine", "2.1", "mg/dL"), baseline=si_base, policy=POLICY).level
        == "critical"
    )
    unknown_base = Quantity(raw_value="1", raw_unit="parsecs")
    assert (
        grade_lab(candidate("Creatinine", "1", "mg/dL"), baseline=unknown_base, policy=POLICY).level
        == "cannot_judge"
    )
    assert (
        grade_lab(candidate("Creatinine", "5", "mg/dL"), baseline=unknown_base, policy=POLICY).level
        == "critical"
    )


def test_cutoff_flags_and_two_factors() -> None:
    assert grade_lab(candidate("Troponin", "0.9", "ng/mL"), policy=POLICY).level == "cannot_judge"
    assert (
        grade_lab(candidate("Troponin", "positive", flag="positive"), policy=POLICY).level
        == "cannot_judge"
    )
    cutoff = Quantity(raw_value="0.04", raw_unit="ng/mL")
    assert (
        grade_lab(candidate("Troponin", "0.9", "ng/mL", cutoff=cutoff), policy=POLICY).level
        == "critical"
    )
    assert (
        grade_lab(candidate("Troponin", "0.01", "ng/mL", cutoff=cutoff), policy=POLICY).level
        == "cannot_judge"
    )
    assert (
        grade_lab(candidate("Troponin", "0.9", "ng/L", cutoff=cutoff), policy=POLICY).reason
        == "unknown_unit"
    )
    unknown_cutoff = Quantity(raw_value="0.04", raw_unit="parsecs")
    assert (
        grade_lab(
            candidate("Troponin", "0.9", "parsecs", cutoff=unknown_cutoff), policy=POLICY
        ).reason
        == "unknown_unit"
    )
    pregnancy = candidate("HCG", "positive", flag="positive")
    first = grade_lab(pregnancy, policy=POLICY)
    assert first.level == "flagged" and first.needs_second_fact
    second = grade_lab(pregnancy, policy=POLICY, second_fact_present=True)
    assert second.level == "critical" and not second.needs_second_fact
    assert (
        grade_lab(
            candidate("HCG", "negative", flag="negative"), policy=POLICY, second_fact_present=True
        ).level
        == "cannot_judge"
    )
    assert grade_lab(candidate("K", "4.2", "mmol/L", "H"), policy=POLICY).level == "flagged"
    assert grade_lab(candidate("K", "6.5", "mmol/L", "normal"), policy=POLICY).level == "critical"


def test_policy_object_controls_thresholds_without_mutating_source() -> None:
    data = POLICY.model_dump()
    data["policy_version"] = "synthetic-policy-test"
    data["bp_thresholds"]["systolic_crisis"] = 170
    data["bp_thresholds"]["thresholds_version"] = "synthetic-bp-test"
    for rule in data["lab_rules"]:
        if rule["analyte"] == "K":
            rule["high"] = 5.0
    other = SafetyPolicy.model_validate(data)
    assert grade_bp(175, 90, policy=other).level == "crisis"
    assert grade_bp(175, 90, policy=POLICY).level == "normal"
    assert grade_lab(candidate("K", "5.5", "mmol/L"), policy=other).level == "critical"
    assert grade_lab(candidate("K", "5.5", "mmol/L"), policy=POLICY).level == "normal"
    assert screen_text("K 5.5 mmol/L", policy=other).policy_version == other.policy_version


@pytest.mark.parametrize(
    ("text", "level", "reason"),
    [
        ("I fainted yesterday. I fainted now.", "danger", "threshold"),
        ("My chest hurt yesterday but it hurts again now", "danger", "threshold"),
        ("BP 59/30", "concern", "implausible"),
        ("ضغطي 10/30", "concern", "implausible"),
        ("59/30", "concern", "implausible"),
        ("K pending mmol/L", "concern", "unknown_value"),
        ("K unreadable mmol/L", "concern", "unknown_value"),
        ("My Ferritin 2000 ng/mL result arrived", "concern", "not_in_table"),
        ("Serum Potassium (K+) 6.5 mmol/L", "danger", "threshold"),
    ],
)
def test_uncertain_readings_and_repeated_current_complaints(
    text: str, level: str, reason: str
) -> None:
    verdict = screen_text(text, policy=POLICY)
    assert (verdict.level, verdict.reason) == (level, reason)


def test_upstream_normalized_quantity_cannot_replace_raw_evidence() -> None:
    value = Quantity(
        raw_value="6.5",
        raw_unit="mmol/L",
        normalized_value=4.2,
        normalized_unit="mmol/L",
        conversion_rule_id="untrusted-extraction",
    )
    verdict = grade_lab(LabCandidate(analyte_raw="K", value=value), policy=POLICY)
    assert verdict.level == "critical"


def test_missing_policy_row_is_never_normal() -> None:
    data = POLICY.model_dump()
    data["lab_rules"] = tuple(rule for rule in data["lab_rules"] if rule["analyte"] != "K")
    data["policy_version"] = "synthetic-missing-row"
    policy = SafetyPolicy.model_validate(data)
    assert grade_lab(candidate("K", "4.2", "mmol/L"), policy=policy).level == "not_in_table"
    assert screen_text("K 4.2 mmol/L", policy=policy).level == "concern"


@pytest.mark.parametrize("analyte", ["Troponin", "D-dimer", "Bilirubin (neonate)"])
def test_slip_flag_does_not_make_an_unknown_numeric_unit_recognizable(analyte: str) -> None:
    verdict = grade_lab(candidate(analyte, "100", "parsecs", "positive"), policy=POLICY)
    assert (verdict.level, verdict.reason) == ("cannot_judge", "unknown_unit")
