"""Versioned local education; no runtime network or embedding service."""

import json
from functools import lru_cache
from importlib.resources import files
from typing import Literal

from pydantic import TypeAdapter

from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY
from sanad.concierge.text import normalized, sentences
from sanad.domain.boundaries import NonblankStr, _BoundaryValue


class EducationEntry(_BoundaryValue):
    id: NonblankStr
    title_ar: NonblankStr
    title_en: NonblankStr
    topic_tags: tuple[NonblankStr, ...]
    permitted_questions: tuple[NonblankStr, ...]
    text_ar: NonblankStr
    text_en: NonblankStr
    source_url: NonblankStr
    source_title: NonblankStr
    source_label: NonblankStr
    source_label_en: NonblankStr
    retrieved_on: NonblankStr
    reviewed_by: NonblankStr
    version: NonblankStr
    source_basis: str = "Public source paraphrase; Arabic translation pending owner review."
    content_kind: Literal["clinical", "product", "safety"] = "clinical"

    def label(self, language: str) -> str:
        return self.source_label_en if language == "en" else self.source_label

    def lines(self, language: str) -> tuple[str, ...]:
        prefix = "General information: " if language == "en" else "معلومة عامة: "
        label = "Source: " if language == "en" else "مصدر: "
        return tuple(
            prefix + s + " (" + label + self.label(language) + ")"
            for s in sentences(self.text_en if language == "en" else self.text_ar)
        )


@lru_cache(maxsize=1)
def source_set() -> tuple[EducationEntry, ...]:
    # JSON is the deliberately restricted, dependency-free subset of YAML used here.
    return TypeAdapter(tuple[EducationEntry, ...]).validate_python(
        json.loads(files(__package__).joinpath("sources.yaml").read_text())
    )


_STOP = frozenset(normalized("ايه ليه هو هي هل يعني عايز اعرف عن what is why how the a of").split())


def retrieve(question: str, *, synthetic: bool = False) -> tuple[EducationEntry, ...]:
    query = normalized(question)
    words = set(query.split()) - _STOP
    ranked = []
    for entry in source_set():
        if not synthetic and entry.reviewed_by == "pending owner review":
            continue
        tags = [normalized(t) for t in entry.topic_tags]
        examples = set(normalized(" ".join(entry.permitted_questions)).split()) - _STOP
        # A complete tag or meaningful example term must match, never arbitrary substrings.
        tag_score = sum(4 for tag in tags if " " + tag + " " in " " + query + " ")
        score = tag_score + len(words & examples)
        if score:
            ranked.append((score, entry.id, entry))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return tuple(row[2] for row in ranked[: DRAFT_CONCIERGE_POLICY.education_top_k])
