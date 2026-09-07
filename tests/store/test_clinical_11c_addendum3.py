"""Attempt 4: reviewable terms, stable corrections and honest order lines."""

from typing import Any

import pytest
from harness import FakeClock
from providers.rxnorm_fixture import RxNormFixture

from sanad.scribe.card import dictation_questions, render_card
from sanad.scribe.lookup import DrugLookupService
from sanad.scribe.memory import memory_rows
from sanad.scribe.names import Resolution
from sanad.scribe.patients import panel
from sanad.store._base import StoreBase
from store.scribe_fixtures import ScribeWorld

TERM_QUESTION = "الأسماء اللي بالعربي اتكتبت زي ما سمعتها؛ لو عايز تكتبها بالإنجليزي عدّلها"


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    value = ScribeWorld.create(store, clock)
    value.approve()
    return value


def fact(spoken: str, english: str, kind: str = "Complaint") -> dict[str, Any]:
    return {
        "category": "history",
        "kind": kind,
        "text": spoken,
        "name_latin": english,
        "terms": [{"spoken": spoken, "english": english}],
    }


def test_anchored_unverified_english_has_one_question_and_tap_teaches_it(
    world: ScribeWorld,
) -> None:
    value: dict[str, Any] = {
        "patient": {"name_as_spoken": "سامي اختبار"},
        "facts": [fact("طنين", "tinnitus"), fact("دوخة", "dizziness")],
    }
    first = world.dictate("سامي اختبار طنين ودوخة", value)
    card = render_card(first)[0]
    assert "History: طنين" in card and "History: دوخة" in card
    assert dictation_questions(first) == (TERM_QUESTION,)
    assert not any(i.blocked for i in first.issues)
    assert all(not n.verified for n in first.names)
    assert not memory_rows(world.store, world.doctor.scope)
    world.tap()
    learned = memory_rows(world.store, world.doctor.scope)
    assert {r.latin for r in learned} == {"طنين", "دوخة"}
    assert all(r.source == "doctor_confirmation" for r in learned)
    second = world.dictate("سامي اختبار طنين ودوخة", value, id=30)
    assert "History: طنين\nHistory: دوخة" in render_card(second)[0]
    assert not dictation_questions(second)
    assert all(n.verified for n in second.names)


def test_edit_replaces_the_displayed_pair_before_learning(world: ScribeWorld) -> None:
    first = world.dictate(
        "سامي اختبار طنين",
        {"patient": {"name_as_spoken": "سامي اختبار"}, "facts": [fact("طنين", "tinnitus")]},
    )
    world.tap("✏️ تعديل")
    changed = first.candidate.model_dump()
    changed["facts"] = [fact("طنين", "ringing in ears")]
    changed["correction_edits"] = [
        {"item": "fact:0", "proposal_index": 0, "source_quote": "طنين يعني ringing in ears"}
    ]
    second = world.dictate("طنين يعني ringing in ears", changed, id=21)
    assert "History: ringing in ears" in render_card(second)[0]
    assert "tinnitus" not in render_card(second)[0]
    world.tap(id=22)
    assert [r.latin for r in memory_rows(world.store, world.doctor.scope)] == ["ringing in ears"]


@pytest.mark.parametrize("fabricate_fact", [False, True])
@pytest.mark.parametrize(
    "english", ["J wave", "ST depression", "LVH", "RBBB", "grade", "New disease"]
)
def test_no_english_without_a_spoken_anchor_in_fact_and_source(
    world: ScribeWorld, fabricate_fact: bool, english: str
) -> None:
    f = fact("عبارة لم تتقال" if fabricate_fact else "طنين", english)
    f["terms"][0]["spoken"] = "عبارة لم تتقال"
    p = world.dictate(
        "سامي اختبار طنين",
        {"patient": {"name_as_spoken": "سامي اختبار"}, "facts": [f]},
    )
    assert english not in render_card(p)[0]
    assert dictation_questions(p) == (TERM_QUESTION,)
    world.tap()
    assert english not in str(memory_rows(world.store, world.doctor.scope))


def test_verified_name_survives_dose_reply_without_another_resolution(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = RxNormFixture()
    world.scribe.rxnorm_client = fixture.client
    first = world.dictate(
        "سامي اختبار bisoprolol 5",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "orders": [{"action": "continue", "drug": "bisoprolol", "dose": "5"}],
        },
    )
    before = next(n for n in first.names if n.kind == "drug")
    assert before.verified and before.source == "seed"
    calls: list[str] = []

    def unavailable(self: DrugLookupService, *args: Any) -> Resolution:
        calls.append(str(args[0]))
        return Resolution(None)

    monkeypatch.setattr(DrugLookupService, "resolve", unavailable)
    changed = first.candidate.model_dump()
    changed["orders"][0]["dose"] = "10"
    second = world.dictate("bisoprolol 10", changed, id=11)
    assert not calls
    after = next(n for n in second.names if n.kind == "drug")
    assert after.verified and after.source == before.source and after.spoken == before.spoken
    assert after.strengths == ("10",)
    assert not any(i.code == "drug_unclear" for i in second.issues)
    assert dictation_questions(second) == ('سمعت "5"، ده يخص إيه؟',)
    assert "Bisoprolol 10" in render_card(second)[0]


@pytest.mark.parametrize("compressed", ["560", "516"])
def test_compound_answer_retires_only_its_old_compressed_number(
    world: ScribeWorld, compressed: str
) -> None:
    first = world.dictate(
        f"سامي اختبار 53 إكس فورش إتش سي تي {compressed} 12.5 وكونكور 5",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "orders": [
                {"action": "continue", "drug": "إكس فورش إتش سي تي", "dose": "5/160/12.5"},
                {"action": "continue", "drug": "كونكور", "dose": "5"},
            ],
        },
    )
    assert first.blocked("order:0")
    assert any(compressed in q for q in dictation_questions(first))
    changed = first.candidate.model_dump()
    second = world.dictate("إكسفورج 5/160/12.5", changed, id=11)
    assert "Exforge HCT 5/160/12.5" in render_card(second)[0]
    assert not second.blocked("order:0")
    assert not any(compressed in q or "Exforge" in q for q in dictation_questions(second))
    assert any('"53"' in q for q in dictation_questions(second))


@pytest.mark.parametrize(
    ("field", "unsupported"),
    [("frequency", "1x2"), ("dose", "99 mg"), ("timing", "every 8 hours"), ("duration", "14 days")],
)
def test_unsupported_order_field_is_omitted_quoted_once_and_still_blocked(
    world: ScribeWorld, field: str, unsupported: str
) -> None:
    order = {"action": "continue", "drug": "Concor", "dose": "5", field: unsupported}
    p = world.dictate(
        "سامي اختبار Concor 5",
        {"patient": {"name_as_spoken": "سامي اختبار"}, "orders": [order]},
    )
    card = render_card(p)[0]
    order_line = next(line for line in card.splitlines() if line.startswith("Concor"))
    assert unsupported not in order_line and "[رقم غير مسموع]" not in order_line
    question = f'سمعت "{unsupported}" بس مش لاقي الرقم ده في كلامك'
    assert dictation_questions(p).count(question) == 1
    assert len(dictation_questions(p)) == (2 if field == "dose" else 1)
    assert card.count(unsupported) == 1
    assert p.blocked("order:0") and any(i.code == "unsupported_number" for i in p.issues)
    world.tap()
    patient = panel(world.store, world.doctor.scope)[0]
    assert not world.store.list_records(patient.scope, "care_order_head")[0]
    assert not memory_rows(world.store, world.doctor.scope)


@pytest.mark.parametrize("frequency", ["مرة واحدة يوميا", "مرتين يوميا"])
def test_spoken_frequency_words_never_generate_digits(world: ScribeWorld, frequency: str) -> None:
    p = world.dictate(
        "سامي اختبار Concor 5 " + frequency,
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "orders": [
                {"action": "continue", "drug": "Concor", "dose": "5", "frequency": frequency}
            ],
        },
    )
    assert "Concor 5, " + frequency in render_card(p)[0]
    assert "1x" not in render_card(p)[0]
