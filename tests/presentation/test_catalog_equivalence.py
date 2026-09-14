"""Keep this oracle until the accepted tuple tables are removed in a later slice."""

from importlib import import_module
from string import Formatter

import pytest

from sanad.domain.language import Language

SURFACES = {
    "doctor": (
        "sanad.channels.telegram.wording",
        ("TEMPLATES", "ENROLLMENT_TEMPLATES", "SCRIBE_TEMPLATES"),
    ),
    "contact": ("sanad.contact.templates", ("TEMPLATES",)),
    "concierge": ("sanad.concierge.templates", ("TEMPLATES",)),
    "evidence": ("sanad.evidence.templates", ("TEXT",)),
    "resolver": ("sanad.resolver.templates", ("TEMPLATES",)),
    "coordinator": ("sanad.coordinator.templates", ("TEMPLATES",)),
}


def legacy(surface: str) -> dict[str, tuple[str, str]]:
    module_name, tables = SURFACES[surface]
    module = import_module(module_name)
    result = {}
    for table in tables:
        for key, value in getattr(module, table).items():
            result[key] = (value, value) if isinstance(value, str) else value
    return result


CASES = [
    (surface, key, locale)
    for surface in SURFACES
    for key in legacy(surface)
    for locale in ("ar", "en")
]


@pytest.mark.parametrize("surface,key,locale", CASES)
def test_every_accepted_key_and_locale_is_byte_identical(
    surface: str, key: str, locale: Language
) -> None:
    from sanad.presentation.catalog import OpaqueValue, render
    from sanad.presentation.context import PresentationContext

    catalog = import_module("sanad.presentation." + surface).CATALOG
    original = legacy(surface)[key][locale == "en"]
    fields = {name: "Synthetic " + name for _, name, _, _ in Formatter().parse(original) if name}
    context = PresentationContext(
        locale, "doctor" if surface == "doctor" or key.startswith("doctor_") else "patient"
    )
    actual = render(
        catalog, surface + "." + key, context, **{k: OpaqueValue(v) for k, v in fields.items()}
    )
    assert actual.encode("utf-8") == original.format(**fields).encode("utf-8")


@pytest.mark.parametrize("surface", SURFACES)
def test_no_keys_renamed_or_new_prose_created(surface: str) -> None:
    # Keep the grandfathered fragment set exact. Contract 17c explicitly adds
    # these three catalogs; they are not legacy prose.
    catalog = import_module("sanad.presentation." + surface).CATALOG
    added = (
        {"doctor.scribe_digest", "doctor.scribe_digest_usage", "doctor.doctor_question_digest"}
        if surface == "doctor"
        else set()
    )
    if surface == "concierge":
        added |= {
            "concierge.doctor_question_deferred",
            "concierge.doctor_answer_reused",
            "concierge.patient_education_unavailable",  # Contract 20 addendum 6f.
        }
        # Contract 28 part 1.
        added |= {
            "concierge.patient_barrier_uncertain",
            "concierge.patient_barrier_targets",
            "concierge.patient_barrier_option_cost",
            "concierge.patient_barrier_option_availability",
            "concierge.patient_barrier_option_forgot",
            "concierge.patient_barrier_option_confusion",
            "concierge.patient_barrier_option_side_effect_experience",
            "concierge.patient_barrier_option_other",
        }
        # Contract 29.
        added |= {
            "concierge.patient_schedule_start",
            "concierge.patient_schedule_yes",
            "concierge.patient_schedule_no",
            "concierge.patient_schedule_rules",
            "concierge.patient_schedule_refused",
            "concierge.patient_schedule_filled",
            "concierge.patient_schedule_past_end",
            "concierge.patient_schedule_unchanged",
            "concierge.patient_schedule_which_half",
            "concierge.patient_schedule_choose",
        }
    assert set(catalog) == {surface + "." + key for key in legacy(surface)} | added
    assert all(set(locales) == {"ar", "en"} for locales in catalog.values())


@pytest.mark.parametrize("surface,key,locale", [c for c in CASES if c[0] != "coordinator"])
def test_legacy_wrapper_keeps_accepted_bytes(
    monkeypatch: pytest.MonkeyPatch, surface: str, key: str, locale: Language
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    module = import_module(SURFACES[surface][0])
    original = legacy(surface)[key][locale == "en"]
    fields = {name: "Synthetic " + name for _, name, _, _ in Formatter().parse(original) if name}
    old_fields = fields
    if surface == "doctor":
        old_fields = {
            k: v
            if k == "link" or (k == "body" and key in {"scribe_card", "scribe_confirmed"})
            else module.untrusted(v)
            for k, v in fields.items()
        }
    assert module.render(key, locale, **fields).encode() == original.format(**old_fields).encode()


def test_equality_oracle_detects_one_changed_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanad.presentation.contact import CATALOG

    key = "contact.doctor_medication_anchor_unknown"
    monkeypatch.setitem(CATALOG, key, CATALOG[key] | {"en": CATALOG[key]["en"] + " "})
    with pytest.raises(AssertionError):
        test_every_accepted_key_and_locale_is_byte_identical(
            "contact", "doctor_medication_anchor_unknown", "en"
        )


@pytest.mark.parametrize(
    "locale,expected",
    [
        ("en", "Exforge 5/160 → Exforge HCT 5/160/12.5"),
        ("ar", "Exforge 5/160 ← Exforge HCT 5/160/12.5"),
    ],
)
def test_11k_brand_change_template_exact_bytes(
    monkeypatch: pytest.MonkeyPatch, locale: str, expected: str
) -> None:
    from sanad.channels.telegram import wording

    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    assert (
        wording.render(
            "scribe_brand_change_line",
            locale,
            old_drug="Exforge",
            old="5/160",
            new_drug="Exforge HCT",
            new="5/160/12.5",
        )
        == expected
    )
    with pytest.raises(ValueError):
        wording.render(
            "scribe_brand_change_line", locale, drug="Exforge HCT", old="5/160", new="5/160/12.5"
        )
