"""Mechanical comparison of two untrusted reads, independent of identity lookup."""

import re
from typing import Literal

from sanad.domain.boundaries import _BoundaryValue
from sanad.media.vision import DocumentItem, DocumentRead, ReaderResult
from sanad.safety.kernel import grade_lab
from sanad.safety.models import LabCandidate, Quantity
from sanad.safety.policy import SafetyPolicy
from sanad.scribe.extract import (
    DictationCandidate,
    FactCandidate,
    LabRowCandidate,
    OrderCandidate,
    ProposalIssue,
)

SHIFT_WARNING = "⚠️ الأرقام ممكن تكون متزحزحة عن الأسماء، راجع الصورة"


class PhotoReview(_BoundaryValue):
    reads: DocumentRead
    kind: Literal["prescription", "lab", "other"]
    resolved_fields: tuple[str, ...] = ()
    edited_rows: tuple[int, ...] = ()
    shift_detected: bool = False
    danger_raised: bool = False
    intake_id: str
    media_work_ids: tuple[str, ...]


def shift_guard(first: ReaderResult, second: ReaderResult) -> bool:
    for reader in (first, second):
        values = [row.item.value for row in reader.items]
        if values and values[0] is None and any(v is not None for v in values[1:]):
            if len(values) > sum(v is not None for v in values):
                return True
    # A shared nonempty sequence offset by exactly one named row is suspicious,
    # even when every row happens to have a value. One equal value is insufficient.
    a, b = first.items, second.items
    for left, right in ((a, b), (b, a)):
        pairs = list(zip(left, right[1:], strict=False))
        if len(pairs) >= 2 and all(
            x.item.value is not None
            and x.item.value == y.item.value
            and x.item.name.strip().casefold() != y.item.name.strip().casefold()
            for x, y in pairs
        ):
            return True
    return False


def first_items(read: DocumentRead) -> tuple[DocumentItem, ...]:
    return tuple(r.item for r in read.first.items) + tuple(
        DocumentItem(name="غير مقروء")
        for _ in range(max(0, len(read.second.items) - len(read.first.items)))
    )


def prescription_items(read: DocumentRead) -> tuple[OrderCandidate, ...]:
    return tuple(
        OrderCandidate(
            action="start",
            drug=r.name,
            dose=" ".join(v for v in (r.dose, r.unit) if v) or None,
            frequency=r.frequency,
            route=r.route,
            timing=r.timing,
        )
        for r in first_items(read)
    )


def grade_row(row: LabRowCandidate, policy: SafetyPolicy) -> LabRowCandidate:
    verdict = (
        grade_lab(
            LabCandidate(
                analyte_raw=row.analyte,
                value=Quantity(raw_value=row.value, raw_unit=row.unit or None),
            ),
            policy=policy,
        )
        if row.value is not None and row.analyte.strip()
        else None
    )
    return row.model_copy(
        update={
            "verdict": verdict,
            "judgment": "cannot_judge"
            if not row.unit or verdict is None or verdict.level in {"cannot_judge", "not_in_table"}
            else "kernel_graded",
        }
    )


def lab_rows(read: DocumentRead, policy: SafetyPolicy) -> tuple[LabRowCandidate, ...]:
    return tuple(
        grade_row(
            LabRowCandidate(
                analyte=r.name,
                value=r.value,
                unit=r.unit,
                flag=r.flag,
            ),
            policy,
        )
        for r in first_items(read)
    )


def lab_text(row: LabRowCandidate) -> str:
    return " ".join((row.analyte, row.value or "غير مقروء", row.unit or "بدون وحدة")) + (
        "؛ علامة مطبوعة: " + row.flag if row.flag else ""
    )


def candidate_from(read: DocumentRead, kind: str, policy: SafetyPolicy) -> DictationCandidate:
    if kind == "prescription":
        return DictationCandidate(orders=prescription_items(read))
    if kind == "lab":
        return DictationCandidate(
            facts=tuple(
                FactCandidate(category="patient_report", text=lab_text(r), lab=r)
                for r in lab_rows(read, policy)
            )
        )
    return DictationCandidate()


def source_text(read: DocumentRead) -> str:
    # Code derives coverage from every printed field in BOTH readers. The
    # caption supplies identity only; a printed drug need not occur in it.
    return "\n".join(
        str(value)
        for reader in (read.first, read.second)
        for item in reader.items
        for value in item.item.model_dump().values()
        if value is not None
    )


def review_issues(review: PhotoReview) -> tuple[ProposalIssue, ...]:
    issues = []
    family = "order" if review.kind == "prescription" else "fact"
    count = max(len(review.reads.first.items), len(review.reads.second.items))
    for disagreement in review.reads.disagreements:
        if disagreement.field in review.resolved_fields:
            continue
        if disagreement.field.startswith("items."):
            index = int(disagreement.field.split(".")[1])
            issues.append(ProposalIssue(item=f"{family}:{index}", code="reader_disagreement"))
        elif disagreement.field != "printed_identity_hint":
            issues.append(ProposalIssue(item="all", code="reader_disagreement"))
    if review.shift_detected:
        issues.extend(
            ProposalIssue(item=f"{family}:{i}", code="shifted_rows")
            for i in range(count)
            if i not in review.edited_rows
        )
    multiple = any(
        re.search(r"two documents|multiple documents|مستندين|أكتر من مستند", note, re.I)
        for r in (review.reads.first, review.reads.second)
        for note in r.notes
    )
    if review.kind == "other" or not count or multiple:
        issues.append(ProposalIssue(item="all", code="document_unclear"))
    return tuple(dict.fromkeys(issues))
