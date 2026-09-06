from typing import Literal

import pytest

from sanad.domain import VersionRef
from sanad.safety import (
    SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY,
)
from sanad.safety import (
    OrderSummary,
    OutputContext,
    SafetyPolicy,
    render_urgent,
    templates,
    validate_patient_output,
    wants_treatment_change,
)

ORDER = OrderSummary(
    order_ref=VersionRef(entity_type="care_order", id="synthetic-order", version=1),
    drug_names=("atorvastatin",),
)


@pytest.mark.parametrize(
    "mode", ["plan_explanation", "general_education", "safety_response", "barrier_help"]
)
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Don't worry, everything is fine.", "unsupported_reassurance"),
        ("اطمن، كله تمام", "unsupported_reassurance"),
        ("kollo tamam", "unsupported_reassurance"),
        ("Ask about metformin.", "drug_not_in_active_orders"),
        ("Take Zyrexivan 40 mg", "drug_not_in_active_orders"),
        ("The amount is 500 mg.", "unsupported_clinical_number"),
        ("الكمية ٥٠٠ مجم", "unsupported_clinical_number"),
        ("take 2 tablets", "imperative_dose_or_frequency"),
        ("Take once daily.", "imperative_dose_or_frequency"),
        ("خدي ٢ قرص", "imperative_dose_or_frequency"),
        ("You could double the dose.", "treatment_change"),
        ("Stop atorvastatin.", "treatment_change"),
        ("Call 911 now.", "wrong_emergency_number"),
        ("اتصل بالإسعاف ٩١١", "wrong_emergency_number"),
        ("your doctor knows", "unsupported_doctor_awareness"),
        ("دكتورك اتبلغ", "unsupported_doctor_awareness"),
        ("I told your doctor.", "unsupported_doctor_awareness"),
    ],
)
def test_violations_are_not_disabled_by_mode(mode: str, text: str, reason: str) -> None:
    context = OutputContext.model_validate(
        {"mode": mode, "active_orders": (ORDER,), "allowed_numbers": ("2 tablets", "40 mg")}
    )
    verdict = validate_patient_output(text, context=context, policy=POLICY)
    assert not verdict.ok
    assert reason in {violation.reason for violation in verdict.violations}
    assert verdict.policy_version == POLICY.policy_version


@pytest.mark.parametrize(
    ("text", "numbers"),
    [
        ("Atorvastatin is on your active plan.", ()),
        ("Your doctor wrote a quantity of 500 mg.", ("500 mg",)),
        ("The written interval is 7 days.", ("7 days",)),
        ("Please call 123 now.", ()),
        ("Your message was received.", ()),
    ],
)
def test_supported_output(text: str, numbers: tuple[str, ...]) -> None:
    context = OutputContext(
        mode="plan_explanation", active_orders=(ORDER,), allowed_numbers=numbers
    )
    verdict = validate_patient_output(text, context=context, policy=POLICY)
    assert verdict.ok, verdict.violations


@pytest.mark.parametrize(
    ("text", "numbers"),
    [
        ("The quantity is 500 mg", ("500",)),
        ("The quantity is 500 g", ("500 mg",)),
        ("The quantity is 7 mg", ("7 days",)),
        ("Your blood pressure is 40", ("40 mg",)),
        ("The quantity is 123 mg", ()),
        ("Take 2 tablets", ("2 tablets",)),
    ],
)
def test_numbers_cannot_cross_units_contexts_or_license_instructions(
    text: str, numbers: tuple[str, ...]
) -> None:
    verdict = validate_patient_output(
        text, context=OutputContext(mode="plan_explanation", allowed_numbers=numbers), policy=POLICY
    )
    assert not verdict.ok


def test_notified_is_distinct_from_human_reading() -> None:
    context = OutputContext(mode="safety_response", doctor_notified=True)
    assert validate_patient_output("Your doctor was notified.", context=context, policy=POLICY).ok
    assert not validate_patient_output(
        "Your doctor has read this.", context=context, policy=POLICY
    ).ok


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Can I take two tablets instead?", True),
        ("ممكن أقلل الجرعة؟", True),
        ("momken a2alel el gar3a?", True),
        ("when do I take atorvastatin?", False),
        ("what is LDL?", False),
    ],
)
def test_concierge_treatment_gate(text: str, expected: bool) -> None:
    assert wants_treatment_change(text, policy=POLICY) is expected


@pytest.mark.parametrize("language", ["ar", "en"])
@pytest.mark.parametrize("gender", ["m", "f", "u"])
def test_every_urgent_template_renders_without_placeholders(
    language: Literal["ar", "en"],
    gender: Literal["m", "f", "u"],
) -> None:
    for template_id in ("patient_emergency", "patient_unreadable_resend", "patient_safety_ack"):
        rendered = render_urgent(template_id, language=language, gender=gender, policy=POLICY)
        assert "{" not in rendered and "}" not in rendered
        verdict = validate_patient_output(
            rendered, context=OutputContext(mode="safety_response"), policy=POLICY
        )
        assert verdict.ok, (rendered, verdict.violations)
    doctor = render_urgent(
        "doctor_danger",
        language=language,
        gender=gender,
        policy=POLICY,
        patient="Synthetic patient",
        concept="BP threshold",
        source="synthetic-observation",
        uncertainty="Unverified report",
    )
    assert "Synthetic patient" in doctor and "Unverified report" in doctor


def test_arabic_gender_and_emergency_policy() -> None:
    genders: tuple[Literal["m", "f", "u"], ...] = ("m", "f", "u")
    texts = [
        render_urgent("patient_emergency", language="ar", gender=g, policy=POLICY) for g in genders
    ]
    assert len(set(texts)) == 3
    assert "روحي" in texts[1] and "روح " in texts[0] and "المطلوب" in texts[2]
    other = SafetyPolicy.model_validate(
        POLICY.model_dump()
        | {"policy_version": "synthetic-number-policy", "emergency_number": "112"}
    )
    rendered = render_urgent("patient_emergency", language="en", gender="u", policy=other)
    assert "112" in rendered and "123" not in rendered
    assert validate_patient_output(
        rendered, context=OutputContext(mode="safety_response"), policy=other
    ).ok
    assert not validate_patient_output(
        "Call 123", context=OutputContext(mode="safety_response"), policy=other
    ).ok


def test_missing_extra_and_nested_template_fields_raise() -> None:
    with pytest.raises(ValueError, match="requires exactly"):
        render_urgent("doctor_danger", language="en", gender="u", policy=POLICY)
    with pytest.raises(ValueError, match="placeholders"):
        render_urgent(
            "doctor_danger",
            language="en",
            gender="u",
            policy=POLICY,
            patient="Synthetic",
            concept="{dose}",
            source="synthetic",
            uncertainty="unknown",
        )
    with pytest.raises(ValueError, match="only from"):
        render_urgent(
            "patient_emergency", language="en", gender="u", policy=POLICY, emergency_number="911"
        )
    with pytest.raises(ValueError, match="requires exactly"):
        render_urgent("patient_safety_ack", language="en", gender="u", policy=POLICY, dose="500 mg")


def test_template_field_validation_rejects_a_corrupted_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        templates.URGENT_TEMPLATES["patient_safety_ack"]["en"], "u", "A leaked {dose}"
    )
    with pytest.raises(ValueError, match="inconsistent"):
        templates.check_template_fields()
