"""Verify independently cited patient problems; never infer a category from words."""

import re
from typing import TYPE_CHECKING

from sanad.concierge.policy import BARRIER_CATEGORIES, BarrierType
from sanad.concierge.records import BarrierCitation, BarrierOutcome, BarrierReading
from sanad.scribe.grounding import clause, grounded
from sanad.scribe.names import normalize

if TYPE_CHECKING:
    from sanad.concierge.plan import Snapshot

NEGATIONS = (
    "not",
    "no",
    "never",
    "don't",
    "doesn't",
    "didn't",
    "isn't",
    "aren't",
    "without",
    "none",
    "مش",
    "ما",
    "مفيش",
    "معيش",
    "مافيش",
    "بدون",
    "لا",
    "cannot",
    "nothing",
    "nobody",
    "neither",
    "nor",
)
EXPERIENCERS = (
    "wife",
    "husband",
    "son",
    "daughter",
    "mother",
    "father",
    "brother",
    "sister",
    "friend",
    "neighbour",
    "he",
    "she",
    "his",
    "her",
    "they",
    "their",
    "him",
    "them",
    "مراتي",
    "جوزي",
    "ابني",
    "بنتي",
    "امي",
    "أمي",
    "ابويا",
    "أبويا",
    "اخويا",
    "أخويا",
    "اختي",
    "أختي",
    "هو",
    "هي",
)


def words(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", normalize(text).replace("’", "'")))


def negated(text: str) -> bool:
    return any(t in NEGATIONS or t.endswith("n't") for t in words(text))


def mission_names(snapshot: "Snapshot") -> dict[str, tuple[str, ...]]:
    from sanad.domain.entities import TestDetails
    from sanad.scribe.extract import OrderCandidate
    from sanad.scribe.names import entry_for

    result = {}
    for mission in snapshot.missions:
        names: list[str] = []
        if isinstance(mission.details, TestDetails):
            names.extend(mission.details.analytes)
        for order in (*snapshot.orders, *snapshot.stopped_orders):
            if any(ref.id == order.order_id for ref in mission.order_refs):
                instruction = order.structured_instruction
                if isinstance(instruction, OrderCandidate):
                    names.append(instruction.drug)
                    entry = entry_for(instruction.drug)
                    if entry:
                        names.extend((*entry.arabic_spellings, *entry.latin_spellings))
        result[mission.id] = tuple(names)
    return result


def verify(
    text: str,
    readers: tuple[BarrierReading, ...],
    names: dict[str, tuple[str, ...]] | None = None,
    model_ids: tuple[str, ...] = (),
) -> BarrierOutcome:
    uncertain = BarrierOutcome(status="uncertain", readers=readers, model_ids=model_ids)
    if len(readers) != 2:
        return uncertain
    if all(not r.problems for r in readers):
        return BarrierOutcome(status="none", readers=readers, model_ids=model_ids)
    if any(len(r.problems) != 1 for r in readers):
        return uncertain
    category = readers[0].problems[0].category
    if category not in BARRIER_CATEGORIES or any(
        r.problems[0].category != category for r in readers
    ):
        return uncertain
    citations = []
    targets: set[str] = set()
    for reader in readers:
        problem = reader.problems[0]
        if not problem.asserted or problem.subject != "patient":
            return uncertain
        accepted = None
        for start, end in grounded(problem.quote, text):
            left, right = clause(text, start, end)
            context = text[left:right]
            if negated(text[left:start]):
                continue
            if negated(text[start:end]) and category not in {"cost", "availability", "confusion"}:
                continue
            markers = {part for token in words(context) for part in token.split("'")}
            if markers & {normalize(t) for t in EXPERIENCERS}:
                continue
            occurrence_targets = {
                id
                for id, aliases in (names or {}).items()
                if any(grounded(alias, context) for alias in aliases)
            }
            if len(occurrence_targets) > 1:
                continue
            targets.update(occurrence_targets)
            accepted = BarrierCitation(quote=problem.quote, start=start, end=end)
            break
        if accepted is None or len(targets) > 1:
            return uncertain
        citations.append(accepted)
    return BarrierOutcome(
        status="accepted",
        category=category,
        citations=tuple(citations),
        readers=readers,
        model_ids=model_ids,
    )


def category(outcome: BarrierOutcome | None) -> BarrierType | None:
    return outcome.category if outcome and outcome.status == "accepted" else None
