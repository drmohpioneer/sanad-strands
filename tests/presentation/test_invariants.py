"""Adversarial catalogs and clinical payloads across the released adapter boundary."""

from dataclasses import FrozenInstanceError
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

from sanad.channels.telegram import wording
from sanad.domain.language import Language
from sanad.presentation.catalog import Catalog, OpaqueValue, render, validate_catalog
from sanad.presentation.context import PresentationContext, resolve
from sanad.presentation.doctor import CATALOG

# Synthetic payloads explicitly cover every field/category in contract C.
CLINICAL = {
    "drug": "Exforge HCT",
    "previous_drug": "Exforge",
    "NameReading.latin": "Concor",
    "name_latin": "Nebilet",
    "generic": "amlodipine/valsartan/hydrochlorothiazide",
    "brand_suffix": "HCT",
    "strength_components": "5/160/12.5",
    "dose": "0.5 mg",
    "previous_dose": "5/160",
    "frequency": "twice daily",
    "route": "oral",
    "timing": "after dinner",
    "duration": "seven days",
    "effective_expression": "2026-09-08T21:30:00+03:00",
    "checkin_expression": "2026-09-11T21:30:00+03:00",
    "LabRowCandidate.analyte": "K",
    "value": "4.25",
    "unit": "mmol/L",
    "flag": "H",
    "TEST_analytes": "CBC, creatinine, Na, K",
    "finding_qualifier": "possible angina on exertion",
    "negation": "No angina",
    "experiencer": "Mother has diabetes",
    "ECG_Echo_EF": "ECG: T wave inversion; Echo: EF 45%",
    "grades_percentages": "grade II; 45%",
    "decimals": "12.50",
    "compound_order_separators": "5/160/12.5; 160 + 5",
    "ranges_counts": "2–3; count 4",
    "deadline": "2026-10-30T01:30:00+03:00",
    "escalation_instant": "2026-10-30T01:30:00+02:00",
    "monitoring_slots": "2026-09-09 08:00, 2026-09-09 20:00",
    "raw_source": "  Add Exforge 5 over 160.\n",
    "canonical_source": "Add Exforge 5/160.",
    "quoted_evidence": '"Do not start Concor"',
    "offsets": "[(0, 19), (21, 27)]",
    "source_ref": "receipt:synthetic-001",
    "patient_name": "أحمد Synthetic",
    "patient_identifier": "000123",
    "field_name": "previous_drug",
    "enum_value": "accepted_pending_identity",
    "event_name": "ORDER_SUPERSEDED",
    "callback_action": "confirm_identity",
    "template_id": "patient_evidence_partial",
    "version": "version:17",
    "hash": "sha256:0000abc123",
    "braces": "5/160 {evening}",
    "looks_like_template": "doctor.scribe_confirmed {body}",
    "RTL_in_English": "English: الاسم ١٢.٥",
    "English_in_RTL": "الاسم: Exforge HCT 5/160/12.5",
}


@pytest.mark.parametrize("locale", ["ar", "en"])
@pytest.mark.parametrize("category,value", CLINICAL.items())
def test_each_clinical_payload_category_is_opaque(
    locale: Language, category: str, value: str
) -> None:
    context = PresentationContext(locale, "doctor")
    actual = render(CATALOG, "doctor.scribe_confirmed", context, body=OpaqueValue(value))
    prefix = "Recorded:\n" if locale == "en" else "اتسجل:\n"
    assert actual.encode() == (prefix + value).encode(), category
    assert wording.render("scribe_confirmed", context, body=value) == actual


@pytest.mark.parametrize("locale", ["ar", "en"])
def test_amendment_invariant_starts_after_unchanged_untrusted(locale: Language) -> None:
    fields = {
        "drug": "Exforge HCT\u202e\n <synthetic>",
        "old": "5/160",
        "new": "5/160/12.5 {evening} " + "x" * 170,
    }
    clean = {k: wording.untrusted(v) for k, v in fields.items()}
    expected = wording.SCRIBE_TEMPLATES["scribe_amendment_line"][locale == "en"].format(**clean)
    actual = wording.render(
        "scribe_amendment_line", PresentationContext(locale, "doctor"), **fields
    )
    assert actual.encode() == expected.encode()
    assert all(value in actual for value in clean.values())
    assert "&#123;evening&#125;" in actual and "\u202e" not in actual


@pytest.mark.parametrize("surface", ["contact", "evidence"])
def test_legacy_field_validation_is_retained(surface: str) -> None:
    from sanad.contact.templates import render as contact
    from sanad.evidence.templates import render as evidence

    wrapper = contact if surface == "contact" else evidence
    key = "doctor_objective_done" if surface == "contact" else "patient_evidence_accepted"
    for fields in ({}, {"title": "CBC", "extra": "ignored"}):
        with pytest.raises(ValueError, match=surface + "_template_fields"):
            wrapper(key, "en", **fields)
    if surface == "evidence":
        for value in (" ", "Synthetic {A}", "Synthetic }"):
            with pytest.raises(ValueError, match="evidence_template_fields"):
                wrapper(key, "en", title=value)


@pytest.mark.parametrize(
    "entry,error",
    [
        ({"ar": "Text {value}."}, "catalog_locales"),
        ({"ar": "Text {value}.", "en": "Text {different}."}, "catalog_placeholder_mismatch"),
        ({"ar": "{value!r}", "en": "{value!r}"}, "catalog_unsafe_placeholder"),
        ({"ar": "{value:.3}", "en": "{value:.3}"}, "catalog_unsafe_placeholder"),
        ({"ar": "{value.text}", "en": "{value.text}"}, "catalog_unsafe_placeholder"),
        ({"ar": "{value[0]}", "en": "{value[0]}"}, "catalog_unsafe_placeholder"),
        ({"ar": "{}", "en": "{}"}, "catalog_unsafe_placeholder"),
        ({"ar": "{value}", "en": ""}, "catalog_text"),
    ],
)
def test_bad_catalog_fails_during_module_import(
    tmp_path: Path, entry: dict[str, str], error: str
) -> None:
    path = tmp_path / "invalid_catalog.py"
    path.write_text(
        "from sanad.presentation.catalog import validate_catalog\n"
        + f"CATALOG = {{'example.sentence': {entry!r}}}\n"
        + "validate_catalog('example', CATALOG)\n"
    )
    spec = spec_from_file_location("invalid_catalog", path)
    assert spec is not None and spec.loader is not None
    with pytest.raises(ValueError, match=error):
        spec.loader.exec_module(module_from_spec(spec))


@pytest.mark.parametrize("template", ["Generic substitute.", "{drug:.3}", "{drug!a}"])
def test_catalog_cannot_drop_or_rewrite_a_clinical_value(template: str) -> None:
    catalog: Catalog = {"example.drug": {"ar": template, "en": template}}
    with pytest.raises(ValueError, match="catalog_fields|catalog_unsafe_placeholder"):
        render(
            catalog,
            "example.drug",
            PresentationContext("en", "doctor"),
            drug=OpaqueValue("Exforge HCT"),
        )


def test_no_recursive_interpolation_and_repeated_placeholders() -> None:
    catalog: Catalog = {"example.echo": {"ar": "{value} / {value}", "en": "{value} / {value}"}}
    validate_catalog("example", catalog)
    value = "{value} doctor.scribe_card"
    assert (
        render(
            catalog, "example.echo", PresentationContext("en", "patient"), value=OpaqueValue(value)
        )
        == value + " / " + value
    )


@pytest.mark.parametrize(
    "surface,key,fields",
    [
        (
            "doctor",
            "scribe_amendment_line",
            {"drug": "Exforge HCT", "old": "5/160", "new": "5/160/12.5"},
        ),
        ("contact", "patient_chase_medication_start", {"drug": "Exforge HCT"}),
        ("concierge", "patient_barrier_recorded", {"drug": "Exforge HCT"}),
        ("evidence", "patient_evidence_accepted", {"title": "CBC"}),
        ("resolver", "place", {"name": "Synthetic", "distance": "500", "details": ""}),
    ],
)
def test_wrapper_cannot_hide_a_clinical_value_when_a_catalog_is_changed(
    monkeypatch: pytest.MonkeyPatch, surface: str, key: str, fields: dict[str, str]
) -> None:
    catalog = import_module("sanad.presentation." + surface).CATALOG
    module = "channels.telegram.wording" if surface == "doctor" else surface + ".templates"
    wrapper = import_module("sanad." + module).render
    monkeypatch.setitem(
        catalog, surface + "." + key, {"ar": "Generic substitute.", "en": "Generic substitute."}
    )
    with pytest.raises(ValueError, match="catalog_fields"):
        wrapper(key, "en", **fields)


def test_context_is_frozen_and_audiences_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    doctor, patient = resolve("en", "doctor"), resolve("ar", "patient")
    assert (doctor.locale, patient.locale) == ("en", "ar")
    with pytest.raises(FrozenInstanceError):
        patient.locale = "en"  # type: ignore[misc]
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    assert resolve("ar", "doctor").locale == resolve("ar", "patient").locale == "en"
    assert resolve(patient, "patient") is patient and patient.locale == "ar"
    with pytest.raises(ValueError, match="presentation_audience"):
        resolve(patient, "doctor")


@pytest.mark.parametrize("field,value", [("locale", "fr"), ("audience", "admin")])
def test_invalid_context_is_rejected(field: str, value: str) -> None:
    values: dict[str, Any] = {"locale": "en", "audience": "doctor", field: value}
    with pytest.raises(ValueError, match="presentation_context"):
        PresentationContext(**values)
