"""Architect's measured failures, exercised through the real card and confirmation."""

import re

import pytest
from harness import FakeClock

from sanad.scribe.card import dictation_questions, render_card
from sanad.scribe.memory import memory_rows
from sanad.scribe.patients import panel
from sanad.store._base import StoreBase
from store.scribe_fixtures import ScribeWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    result = ScribeWorld.create(store, clock)
    result.approve()
    return result


@pytest.mark.parametrize(
    ("spoken", "english", "expected"),
    [
        ("بانو كريات", "CBC", "BUN, creatinine"),
        ("بانو كريات وسوديوم وبوتاسيوم", "CBC", "BUN, creatinine, Na, K"),
        ("تحاليل بانو كريات و sodium و potassium", "CBC", "BUN, creatinine, Na, K"),
        ("Bano Creatine, sodum, potassum", "CBC", "BUN, creatinine, Na, K"),
        ("Banoo Creatinee", "CBC", "BUN, creatinine"),
        ("BUN والكرياتينين والصوديوم والبوتاسيوم", "CBC", "BUN, creatinine, Na, K"),
        ("Noveltest", "CBC", "Noveltest"),
    ],
)
def test_test_names_come_from_spoken_form_even_when_model_says_cbc(
    world: ScribeWorld, spoken: str, english: str, expected: str
) -> None:
    p = world.dictate(
        "سامي اختبار محتاج " + spoken,
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "missions": [{"kind": "TEST", "text": spoken, "clinical_en": english}],
        },
    )
    assert p.candidate.missions[0].text == spoken
    assert p.candidate.missions[0].clinical_en == expected
    assert "TEST: " + expected in render_card(p)[0]
    assert "CBC" not in render_card(p)[0]
    world.tap()
    learned = memory_rows(world.store, world.doctor.scope)
    assert {r.latin for r in learned} == set(expected.split(", "))
    assert all(r.kind == "test" for r in learned)
    again = world.dictate(
        "سامي اختبار محتاج " + spoken,
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "missions": [{"kind": "TEST", "text": spoken, "clinical_en": english}],
        },
        id=30,
    )
    assert again.candidate.missions[0].clinical_en == expected
    assert "TEST: " + expected in render_card(again)[0]


def test_unresolved_spoken_test_cannot_borrow_model_translation(world: ScribeWorld) -> None:
    spoken = "فحص زيلورا"
    p = world.dictate(
        "سامي اختبار محتاج " + spoken,
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "missions": [{"kind": "TEST", "text": spoken, "clinical_en": "CBC"}],
        },
    )
    assert p.candidate.missions[0].clinical_en is None
    assert "TEST: فحص زيلورا (؟)" in render_card(p)[0]
    assert "CBC" not in render_card(p)[0]
    assert len(dictation_questions(p)) == 1
    assert not p.names


def test_test_name_resolution_does_not_hide_an_unsupported_model_number(world: ScribeWorld) -> None:
    p = world.dictate(
        "سامي اختبار محتاج بانو كريات",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "missions": [{"kind": "TEST", "text": "بانو كريات", "clinical_en": "CBC 99"}],
        },
    )
    assert p.candidate.missions[0].clinical_en is None
    assert "TEST: بانو كريات (؟)" in render_card(p)[0]
    assert "99" not in render_card(p)[0]
    assert len(dictation_questions(p)) == 1


def test_test_name_uses_learned_spelling_before_seed(world: ScribeWorld) -> None:
    from sanad.scribe.names import normalize
    from sanad.store import keys
    from sanad.store._base import Write
    from sanad.store.records import NameMemory, record_item, to_record

    row = NameMemory(
        id=keys.digest(keys.partition(world.doctor.scope) + ":test:" + normalize("Noveltest")),
        scope=world.doctor.scope,
        latin="Noveltest",
        kind="test",
        spoken_forms=("زيلورا",),
        source="doctor_confirmation",
        created_at=world.clock(),
        updated_at=world.clock(),
        last_confirmed_at=world.clock(),
    )
    assert world.store._atomic([Write(record_item(to_record(row, row.scope)), None)], [])
    p = world.dictate(
        "سامي اختبار محتاج زيلورا وبوتاسيوم",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "missions": [{"kind": "TEST", "text": "زيلورا وبوتاسيوم", "clinical_en": "CBC"}],
        },
    )
    assert p.candidate.missions[0].clinical_en == "Noveltest, K"
    assert "TEST: Noveltest, K" in render_card(p)[0]
    assert not dictation_questions(p)


def test_fact_prefixes_order_and_separate_payloads_survive_confirmation(world: ScribeWorld) -> None:
    facts = [
        ("History", "ضغط وسكر", "hypertension, diabetes"),
        ("Complaint", "أنجينا", "angina"),
        ("ECG", "تي ويف انفريجن في لاترال ليدز", "T wave inversion in lateral leads"),
        (
            "Echo",
            "الأكو فانكشن 45%",
            "EF 45% %",
        ),
    ]
    p = world.dictate(
        "سامي اختبار " + "، ".join(raw for _, raw, _ in facts),
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "facts": [
                {
                    "category": "history",
                    "clinical_kind": kind,
                    "text": raw,
                    "terms": [{"spoken": raw, "english": en}],
                }
                for kind, raw, en in facts
            ],
        },
    )
    expected = [kind + ": " + en.replace("% %", "%") for kind, _, en in facts]
    lines = render_card(p)[0].splitlines()
    assert [
        line for line in lines if line.startswith(tuple(k + ":" for k, _, _ in facts))
    ] == expected
    assert [f.text for f in p.candidate.facts] == [raw for _, raw, _ in facts]
    assert not dictation_questions(p)
    world.tap()
    patient = panel(world.store, world.doctor.scope)[0]
    rows = world.store.list_records(patient.scope, "clinical_fact")[0]
    assert len(rows) == len(facts)
    assert {
        payload["clinical_kind"] for r in rows if isinstance(payload := r.body["payload"], dict)
    } == {kind for kind, _, _ in facts}


@pytest.mark.parametrize(
    "english",
    [
        "grade II angina",
        "grade angina",
        "class 2 angina",
        "stage angina",
        "II angina",
        "iv angina",
        "severe angina",
        "unstable angina",
        "acute angina",
        "recurrent angina",
    ],
)
def test_unsupported_qualifier_or_roman_grade_falls_back_to_spoken_fact(
    world: ScribeWorld, english: str
) -> None:
    p = world.dictate(
        "سامي اختبار عنده أنجينا 2 أيام",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "facts": [
                {
                    "category": "history",
                    "clinical_kind": "Complaint",
                    "text": "أنجينا 2 أيام",
                    "clinical_en": english,
                }
            ],
        },
    )
    assert p.candidate.facts[0].clinical_en is None
    assert "أنجينا 2 أيام (؟)" in render_card(p)[0]
    assert english not in render_card(p)[0]
    assert len(dictation_questions(p)) == 1


def test_explicit_grade_is_retained_without_inventing_one(world: ScribeWorld) -> None:
    p = world.dictate(
        "سامي اختبار grade II angina",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "facts": [
                {
                    "category": "history",
                    "clinical_kind": "Complaint",
                    "text": "grade II angina",
                    "clinical_en": "grade II angina",
                    "terms": [{"spoken": "grade II angina", "english": "grade II angina"}],
                }
            ],
        },
    )
    assert "Complaint: grade II angina" in render_card(p)[0]
    assert not dictation_questions(p)


@pytest.mark.parametrize("empty", [None, "null", "None", " NULL ", "none", ""])
def test_absent_medication_fields_never_render_literal_null(
    world: ScribeWorld, empty: str | None
) -> None:
    p = world.dictate(
        "سامي اختبار إكسفورج إتش سي تي 5/160/12.5",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "orders": [
                {
                    "action": "continue",
                    "drug": "إكسفورج إتش سي تي",
                    "dose": "5/160/12.5",
                    "frequency": empty,
                    "route": empty,
                    "timing": empty,
                    "duration": empty,
                }
            ],
        },
    )
    lines = render_card(p)[0].splitlines()
    assert "Exforge HCT 5/160/12.5" in lines
    assert not re.search(r"\b(?:null|none)\b", "\n".join(lines), re.I)
    assert not any(i.code == "order_missing" for i in p.issues)
