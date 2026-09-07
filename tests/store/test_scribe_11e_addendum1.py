"""Attempt-2 category, numeric-format and question outcomes on both stores."""

from typing import Any

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate

from sanad.scribe.card import dictation_questions, render_card
from sanad.scribe.extract import DictationCandidate, candidate_issues
from sanad.scribe.merge import merge_candidates
from sanad.scribe.patients import panel
from sanad.scribe.records import ClinicalFact
from sanad.store._base import StoreBase
from sanad.store.records import from_record
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    result = ScribeWorld.create(store, clock)
    result.approve()
    return result


@pytest.mark.parametrize(
    "category,text,prefix",
    [
        ("finding", "ECG في تي أوف إنفرجين في اللاترال", "ECG:"),
        ("finding", "أكو فانكشن 45%", "Echo:"),
        ("finding", "سيجمنتال إنفروبوسترو لاترال", "Echo:"),
        ("finding", "أنجينا", "Finding:"),
        ("complaint", "أنجينا", "Complaint:"),
        ("condition", "ضغطه سكر", "Dx:"),
        ("history", "ECG", "History:"),
        ("medication_history", "ECG", "History:"),
        ("unknown", "ECG", "History:"),
        (None, "ECG", "History:"),
    ],
)
def test_fact_category_never_quarantines_valid_content_and_roundtrips_on_confirmation(
    world: ScribeWorld, category: object, text: str, prefix: str
) -> None:
    p = world.dictate(
        "مريض جديد سامي اختبار " + text,
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "facts": [{"category": category, "text": text}],
        },
    )
    assert len(p.candidate.facts) == 1
    assert not p.candidate.ambiguities and not p.candidate._dropped_numbers
    assert any(line.startswith(prefix) for line in render_card(p)[0].splitlines())
    if text == "أكو فانكشن 45%":
        assert "Echo: EF 45%" in render_card(p)[0]
    assert not any(q.code == "unassigned_number" for q in p.issues)
    assert not any(q.code == "extraction_conflict" for q in p.issues)
    world.tap()
    saved = world.scribe.repo.load(p.scope, "scribe_proposal", p.id, type(p))
    assert saved and saved.status == "confirmed"
    patient = panel(world.store, world.doctor.scope)[0]
    rows, _ = world.store.list_records(patient.scope, "clinical_fact")
    facts = [from_record(row, ClinicalFact) for row in rows]
    assert len(facts) == 1
    assert facts[0].category == ("history" if category in {"unknown", None} else category)


@pytest.mark.parametrize(
    "left,right", [("560 12.5", "560/12.5"), ("560/12.5", "560 12.5"), ("560.0, 12.50", "560/12.5")]
)
def test_separator_only_doses_and_year_units_render_the_same_card(
    world: ScribeWorld, left: str, right: str
) -> None:
    scripts = [
        {
            "patient": {"name_as_spoken": name, "age": age},
            "orders": [{"action": "continue", "drug": "إكس فورش إتش سي تي 560 12.5", "dose": dose}],
        }
        for dose, age, name in ((left, "53", "سامي اختبار"), (right, "53 سنة سنة", "سامي   اختبار"))
    ]
    model = ScriptedModel(*(candidate(s) for s in scripts))
    world.scribe.model_factory = lambda *args: model
    world.post(
        update(APPLICANT, "مريض جديد سامي اختبار 53 سنة ماشي على إكس فورش إتش سي تي 560 12.5", 10)
    )
    p = world.proposal
    card = render_card(p)[0]
    assert card.splitlines()[0] == "مريض جديد: سامي اختبار، 53 سنة"
    assert "Exforge HCT 560 12.5\n" in card
    assert not any(i.code == "extraction_conflict" for i in p.issues)
    assert dictation_questions(p) == ('سمعت "560 12.5" لـ Exforge HCT، قصدك 5/160/12.5؟',)


@pytest.mark.parametrize("missing_first", [True, False])
def test_one_reading_cannot_erase_selected_patient(world: ScribeWorld, missing_first: bool) -> None:
    patient = world.named_stub("سامي اختبار")
    with_name = {
        "patient": {"name_as_spoken": patient.display_name, "age": "53 سنة"},
        "facts": [{"category": "condition", "text": "ضغط"}],
    }
    without_name = {"patient": {}, "facts": with_name["facts"]}
    values = (without_name, with_name) if missing_first else (with_name, without_name)
    model = ScriptedModel(*(candidate(v) for v in values))
    world.scribe.model_factory = lambda *args: model
    world.post(update(APPLICANT, "سامي اختبار عنده 53 سنة ضغط", 10))
    p = world.proposal
    assert p.selected_patient_id == patient.id and not p.creating_patient
    assert render_card(p)[0].splitlines()[0] == "المريض: سامي اختبار، 53 سنة"
    assert not dictation_questions(p)


def test_distinct_patient_values_keep_one_question_per_field_and_numeric_evidence() -> None:
    a = DictationCandidate.model_validate(
        {"patient": {"name_as_spoken": "سامي اختبار", "age": "53"}}
    )
    b = DictationCandidate.model_validate(
        {"patient": {"name_as_spoken": "علي اختبار", "age": "99 سنة"}}
    )
    result = merge_candidates(a, b, "سامي اختبار 53")
    assert result and {q.field for q in result.issues} == {"name_as_spoken", "age"}
    assert result.candidate.patient.name_as_spoken == "سامي اختبار"
    assert any(
        q.code == "unsupported_number" for q in candidate_issues(result.candidate, "سامي اختبار 53")
    )


def test_null_identifiers_do_not_discard_patient_or_create_unassigned_age(
    world: ScribeWorld,
) -> None:
    patient = world.named_stub("سامي اختبار")
    value: dict[str, object] = {
        "patient": {"name_as_spoken": patient.display_name, "age": "53 سنة", "identifiers": None},
        "facts": [{"category": "condition", "text": "ضغط"}],
    }
    p = world.dictate("سامي اختبار 53 سنة ضغط", value)
    assert p.selected_patient_id == patient.id
    assert render_card(p)[0].splitlines()[0] == "المريض: سامي اختبار، 53 سنة"
    assert not dictation_questions(p)
    assert not p.candidate._dropped_numbers


@pytest.mark.parametrize("reverse", [False, True])
def test_compound_model_rewrite_keeps_heard_line_one_question_and_numeric_block(
    world: ScribeWorld, reverse: bool
) -> None:
    values: list[dict[str, Any]] = [
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "orders": [{"action": "continue", "drug": "إكس فورش إتش سي تي " + dose, "dose": dose}],
        }
        for dose in ("560 12.5", "5/160/12.5")
    ]
    model = ScriptedModel(*(candidate(v) for v in (values[::-1] if reverse else values)))
    world.scribe.model_factory = lambda *args: model
    world.post(update(APPLICANT, "مريض جديد سامي اختبار ماشي على إكس فورش إتش سي تي 560 12.5", 10))
    p = world.proposal
    assert "Exforge HCT 560 12.5\n" in render_card(p)[0]
    assert p.blocked("order:0")
    assert any(q.code == "unsupported_number" and "160" in q.numbers for q in p.issues)
    assert dictation_questions(p) == ('سمعت "560 12.5" لـ Exforge HCT، قصدك 5/160/12.5؟',)


def test_placeholder_disappears_but_actual_doubt_and_malformed_numbers_remain(
    world: ScribeWorld,
) -> None:
    p = world.dictate(
        "مريض جديد سامي اختبار ضغط والميعاد مش واضح",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "facts": [{"category": "condition", "text": "ضغط"}],
            "ambiguities": ["فيه بند مش واضح؛ وضّحه في التعديل.", "الميعاد مش واضح"],
        },
    )
    assert dictation_questions(p) == ('سمعت "الميعاد مش واضح"، توضح المقصود؟',)
    broken = DictationCandidate.model_validate(
        {"facts": [{"category": "unknown", "text": {"bad": "99"}}]}
    )
    assert not broken.facts and broken._dropped_numbers == ("99",)
    assert any(i.code == "unsupported_number" for i in candidate_issues(broken, "سامي اختبار"))
