from domain_fixtures import NOW
from evidence_cases import CLASSIFICATION_ROWS, EVALUATOR_ROWS, read

from sanad.evidence.classify import classify
from sanad.evidence.evaluate import evaluate


def test_hand_computed_evaluator_table(subtests) -> None:  # type: ignore[no-untyped-def]
    for label, mission, candidate, previous, satisfied, missing in EVALUATOR_ROWS:
        with subtests.test(row=label):
            result = evaluate(mission, candidate, previous, NOW)
            assert result.satisfied is satisfied
            assert result.missing == missing


def test_hand_computed_classification_table(subtests) -> None:  # type: ignore[no-untyped-def]
    for label, coarse, item, caption, expected in CLASSIFICATION_ROWS:
        with subtests.test(row=label):
            assert classify(read(coarse, (item,)), caption, ()) == expected
