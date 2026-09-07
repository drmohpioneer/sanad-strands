"""Mechanical comparison of two untrusted reads, independent of identity lookup."""

import re
from typing import TYPE_CHECKING, Literal

from sanad.domain.boundaries import _BoundaryValue
from sanad.media.agreement import agreed_rows
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

if TYPE_CHECKING:
    from sanad.scribe.proposal import Proposal

SHIFT_WARNING = "⚠️ الأرقام ممكن تكون متزحزحة عن الأسماء، راجع الصورة"
# Contract 11d / Decision 023. OWNER_REVIEW_PENDING.
PHOTO_SUPPORTED_SCOPE = (
    "المدعوم حاليًا: مستندات مطبوعة أو مكتوبة بالكمبيوتر بحروف لاتينية؛ "
    "خط اليد والكتابة العربية في الصور لسه مش مدعومين."
)
HANDWRITING_REPLY = (
    ("مش قادر أقرا الورقة دي بثقة. صوّرها من فوق في نور كويس، أو قول لي اللي فيها وهسجّلها")
    + "\n"
    + PHOTO_SUPPORTED_SCOPE
)
SINGLE_READER_WARNING = "قريت الورقة قراءة واحدة بس، مش هسجّل منها حاجة"
AGREEMENT_WARNING = "قريت الورقة قراءتين مختلفتين، مش هسجّل منها حاجة"
COLUMN_CAPTION = "التعليمات بالعربي زي ما هي في الورقة؛ الصفوف جنبها في الكارت"


def two_readers(read: DocumentRead) -> bool:
    # Check both the persisted marker and actual slots, including older caches.
    return not read.single_reader and len(read.readers) == 2


def unreadable_reply(read: DocumentRead) -> str:
    if not two_readers(read):
        return HANDWRITING_REPLY + "\n" + SINGLE_READER_WARNING
    return HANDWRITING_REPLY + ("\n" + AGREEMENT_WARNING if unreadable_read(read) else "")


def render_card(proposal: "Proposal") -> tuple[str, ...]:
    """Keep the accepted layout behind the minimum-independent-read gate."""
    from sanad.scribe.card import render_card as accepted_card

    if proposal.photo and unreadable_read(proposal.photo.reads):
        return (unreadable_reply(proposal.photo.reads),)
    return accepted_card(proposal)


def unreadable_read(read: DocumentRead) -> bool:
    if not two_readers(read):
        return True
    total = max(len(read.first.items), len(read.second.items))
    return total == 0 or agreed_rows(read) < (total + 1) // 2


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
            and (x.item.name or "").strip().casefold() != (y.item.name or "").strip().casefold()
            for x, y in pairs
        ):
            return True
    return False


def first_items(read: DocumentRead) -> tuple[DocumentItem, ...]:
    return tuple(r.item for r in read.first.items) + tuple(
        DocumentItem(name=None)
        for _ in range(max(0, len(read.second.items) - len(read.first.items)))
    )


def prescription_items(read: DocumentRead) -> tuple[OrderCandidate, ...]:
    return tuple(
        OrderCandidate(
            action="start",
            drug=r.name or "",
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
                analyte=r.name or "",
                value=r.value,
                unit=r.unit,
                flag=r.flag,
            ),
            policy,
        )
        for r in first_items(read)
    )


def lab_text(row: LabRowCandidate) -> str:
    return " ".join((row.analyte, row.value or "", row.unit or "بدون وحدة")) + (
        "؛ علامة مطبوعة: " + row.flag if row.flag else ""
    )


def candidate_from(read: DocumentRead, kind: str, policy: SafetyPolicy) -> DictationCandidate:
    if unreadable_read(read):
        return DictationCandidate()
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
        for reader in read.readers
        for item in reader.items
        for key, value in item.item.model_dump().items()
        if key != "note"
        if value is not None
    )


def review_issues(review: PhotoReview, candidate: DictationCandidate) -> tuple[ProposalIssue, ...]:
    if unreadable_read(review.reads):
        return (ProposalIssue(item="all", code="document_unclear"),)
    issues = []
    family = "order" if review.kind == "prescription" else "fact"
    count = max(len(review.reads.first.items), len(review.reads.second.items))
    current_names = (
        [order.drug for order in candidate.orders]
        if review.kind == "prescription"
        else [fact.lab.analyte if fact.lab else "" for fact in candidate.facts]
    )
    for i, name in enumerate(current_names):
        if not name.strip():
            issues.append(ProposalIssue(item=f"{family}:{i}", code="reader_disagreement"))
    for i, item in enumerate(first_items(review.reads)):
        if not item.name and f"items.{i}.name" not in review.resolved_fields:
            issues.append(ProposalIssue(item=f"{family}:{i}", code="reader_disagreement"))
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
