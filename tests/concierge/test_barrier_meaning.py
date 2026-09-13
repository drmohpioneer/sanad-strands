"""Independent source, polarity, agreement and area-privacy oracles."""

import pytest

from sanad.concierge.barrier_evidence import verify
from sanad.concierge.records import BarrierReading
from sanad.resolver.area_exclusions import excluded
from sanad.safety.sentinel import CONCEPT_RULES


def reading(
    category: str, quote: str, *, asserted: bool = True, subject: str = "patient"
) -> BarrierReading:
    return BarrierReading.model_validate(
        {
            "problems": [
                {
                    "category": category,
                    "quote": quote,
                    "asserted": asserted,
                    "subject": subject,
                }
            ]
        }
    )


PARAPHRASES = (
    ("the pharmacy near me didn't have it", "availability"),
    ("it costs more than I can pay this month", "cost"),
    ("I keep forgetting the evening one", "forgot"),
    ("I felt dizzy after the new pill", "side_effect_experience"),
    ("I'm not sure if I take it before food", "confusion"),
    ("الصيدلية اللي جنبي ملقيتش عندها العلاج", "availability"),
    ("تمنه اكتر من اللي اقدر ادفعه الشهر ده", "cost"),
    ("كل مرة بنسى اللي بالليل", "forgot"),
    ("حسيت بدوخة بعد الحباية الجديدة", "side_effect_experience"),
    ("مش متأكد اخده قبل الاكل", "confusion"),
    ("can't afford it", "cost"),
    ("ازاي اخده", "confusion"),
    ("I wasn't able to buy it", "availability"),
)


@pytest.mark.parametrize("text,category", PARAPHRASES)
def test_verified_paraphrases(text: str, category: str) -> None:
    r = reading(category, text)
    outcome = verify(text, (r, r))
    assert outcome.status == "accepted" and outcome.category == category
    assert len(outcome.citations) == 2
    assert all(
        text[c.start : c.end] and c.asserted and c.subject == "patient" for c in outcome.citations
    )


@pytest.mark.parametrize(
    "text,quote,category",
    [
        ("I feel fine", "dizzy", "side_effect_experience"),
        ("I'm not having any side effects", "side effects", "side_effect_experience"),
        (
            "I'm not having any side effects",
            "I'm not having any side effects",
            "side_effect_experience",
        ),
        ("I did not forget", "I did not forget", "forgot"),
        ("I did not say it is not available", "not available", "availability"),
        ("I wasn't able to buy it", "able to buy it", "cost"),
        ("I wasn't able to buy it", "I wasn't able to buy it", "other"),
        ("my wife can't afford hers", "can't afford", "cost"),
        ("my wife can't afford hers", "my wife can't afford hers", "cost"),
        ("dizzy describes my wife", "dizzy", "side_effect_experience"),
        ("my wife's medicine is too expensive", "too expensive", "cost"),
        ("she’s dizzy", "dizzy", "side_effect_experience"),
        ("دوا جوزي غالي", "غالي", "cost"),
    ],
)
def test_wrong_readers_cannot_override_code(text: str, quote: str, category: str) -> None:
    r = reading(category, quote)
    result = verify(text, (r, r))
    assert result.status == "uncertain" and not result.category and not result.citations


@pytest.mark.parametrize(
    "change", ["unasserted", "other_person", "disagreement", "one_bad_quote", "all_bad_quotes"]
)
def test_failed_reading_never_becomes_none(change: str) -> None:
    source = "I forgot the evening one"
    a = reading("forgot", source)
    b = {
        "unasserted": reading("forgot", source, asserted=False),
        "other_person": reading("forgot", source, subject="someone_else"),
        "disagreement": reading("cost", source),
        "one_bad_quote": reading("forgot", "made up words"),
        "all_bad_quotes": reading("forgot", "made up words"),
    }[change]
    if change == "all_bad_quotes":
        a = b
    assert verify(source, (a, b)).status == "uncertain"


def test_question_none_and_differing_valid_citations() -> None:
    none = BarrierReading(problems=())
    assert verify("is it expensive?", (none, none)).status == "none"
    source = "I felt dizzy, not nauseous"
    result = verify(
        source,
        (
            reading("side_effect_experience", "I felt dizzy"),
            reading("side_effect_experience", "dizzy"),
        ),
    )
    assert result.status == "accepted" and len(result.citations) == 2
    assert result.citations[0].start != result.citations[1].start


def test_first_valid_repeated_quote() -> None:
    source = "I did not forget. I forgot it."
    result = verify(source, (reading("forgot", "forgot"),) * 2)
    assert result.status == "accepted"
    assert result.citations[0].start == source.rindex("forgot")
    source = "aspirin and CBC are too expensive. CBC is too expensive."
    result = verify(
        source, (reading("cost", "too expensive"),) * 2, {"med": ("aspirin",), "test": ("CBC",)}
    )
    assert result.status == "accepted" and result.citations[0].start == source.rindex(
        "too expensive"
    )


@pytest.mark.parametrize("several", [True, False])
def test_two_targets_cannot_borrow_one_problem(several: bool) -> None:
    source = "aspirin too expensive and the CBC lab was closed"
    r = reading("cost", source)
    if several:
        r = BarrierReading(
            problems=(
                reading("cost", "aspirin too expensive").problems[0],
                reading("availability", "CBC lab was closed").problems[0],
            )
        )
    result = verify(source, (r, r), {"med": ("aspirin",), "test": ("CBC",)})
    assert result.status == "uncertain" and not result.category


@pytest.mark.parametrize(
    "term",
    [
        "nausea",
        "nauseous",
        "dizziness",
        "دوخة",
        "صـدري",
        "صَدْري",
        "الصدر",
        "بصدري",
        "sadry",
        "aspirin",
        "CBC",
        "not",
        "لا",
        *(t.removesuffix("*") for _, groups in CONCEPT_RULES for group in groups for t in group),
    ],
)
def test_clinical_area_exclusions(term: str) -> None:
    assert excluded(term)


def test_control_area_and_dynamic_mission_name() -> None:
    assert not excluded("Synthetic Quarter, Cairo")
    assert excluded("UnlistedDrug", ("UnlistedDrug",))
    assert not excluded("UnlistedDrug")
