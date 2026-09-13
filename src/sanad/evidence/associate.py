"""Scoped deterministic matching; a caption is never a patient lookup key."""

from collections.abc import Sequence

from sanad.domain import Mission
from sanad.domain.entities import TERMINAL_STATES, TestDetails
from sanad.evidence.classify import analyte, caption_categories, expected, normalize
from sanad.store.records import Evidence


def identity_required(evidence: Evidence) -> bool:
    return bool(
        {"identity_mismatch", "identity_unverifiable"}.intersection(evidence.flags)
        and "identity_confirmed" not in evidence.flags
    )


def open_missions(missions: Sequence[Mission]) -> tuple[Mission, ...]:
    return tuple(
        m
        for m in missions
        if m.state in {"open", "waiting_patient", "blocked", "unreachable", "overdue"}
        and m.objective_predicate.kind == "evidence"
    )


def choose(
    missions: Sequence[Mission], evidence: Evidence, caption: str
) -> tuple[Mission | None, str | None, tuple[Mission, ...]]:
    plausible = tuple(m for m in open_missions(missions) if evidence.category in expected(m))
    words = normalize(caption)
    named = []
    for mission in plausible:
        title = normalize(mission.title)
        match = bool(words and title and title in words)
        if isinstance(mission.details, TestDetails):
            names = {analyte(w) for w in words.split()}
            match |= bool(names.intersection(analyte(a) for a in mission.details.analytes))
        else:
            match |= bool(caption_categories(caption).intersection(expected(mission)))
        if match:
            named.append(mission)
    if len(named) == 1:
        return named[0], "caption", plausible
    if len(plausible) == 1:
        return plausible[0], "single_open_mission", plausible
    return None, None, plausible


def closed_match(missions: Sequence[Mission], evidence: Evidence) -> Mission | None:
    matches = [
        m for m in missions if m.state in TERMINAL_STATES and evidence.category in expected(m)
    ]
    return max(matches, key=lambda m: m.updated_at) if matches else None
