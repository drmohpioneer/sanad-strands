"""Resolve observed analyte aliases without changing raw value or unit provenance."""

from sanad.evidence.classify import analyte
from sanad.media.vision import Disagreement
from sanad.safety.policy import SafetyPolicy
from sanad.scribe.crosscheck import grade_row
from sanad.scribe.extract import LabRowCandidate


def meaningful_disagreements(values: tuple[Disagreement, ...]) -> tuple[Disagreement, ...]:
    """Empty and absent units are the same missing observation, never an inferred unit."""
    return tuple(
        d
        for d in values
        if not (
            d.field.endswith(".unit")
            and not (d.first or "").strip()
            and not (d.second or "").strip()
        )
    )


def grade(row: LabRowCandidate, policy: SafetyPolicy) -> LabRowCandidate:
    graded = grade_row(row.model_copy(update={"analyte": analyte(row.analyte)}), policy)
    return graded.model_copy(update={"analyte": row.analyte})
