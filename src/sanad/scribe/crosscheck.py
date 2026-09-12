"""Mechanical comparison of two untrusted reads, independent of identity lookup."""

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from sanad.domain.boundaries import _BoundaryValue
from sanad.domain.language import effective
from sanad.media.agreement import agreed_rows, readable, row_assignment
from sanad.media.vision import DocumentItem, DocumentRead, ReaderResult
from sanad.safety.kernel import grade_lab
from sanad.safety.models import LabCandidate, Quantity
from sanad.safety.policy import SafetyPolicy
from sanad.scribe.extract import (
    DictationCandidate,
    FactCandidate,
    LabRowCandidate,
    MissionCandidate,
    OrderCandidate,
    ProposalIssue,
)

if TYPE_CHECKING:
    from sanad.scribe.lookup import DrugLookup
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

# The Arabic aliases above remain available to the accepted literal fixtures.
PHOTO_WORDING = {
    "shift_warning": (
        SHIFT_WARNING,
        "⚠️ Values may be shifted away from their row names; check the image.",
    ),
    "photo_supported_scope": (
        PHOTO_SUPPORTED_SCOPE,
        "Supported now: printed or typed documents in Latin script. "
        "Handwriting and Arabic script in images are not supported yet.",
    ),
    "handwriting_reply": (
        HANDWRITING_REPLY,
        "I could not read this paper confidently. Photograph it from above in good light, "
        "or tell me what it says so I can record it.\n"
        "Supported now: printed or typed documents in Latin script. "
        "Handwriting and Arabic script in images are not supported yet.",
    ),
    "single_reader_warning": (
        SINGLE_READER_WARNING,
        "Only one reader could read the paper; I will record nothing from it.",
    ),
    "agreement_warning": (
        AGREEMENT_WARNING,
        "The two readings of the paper differ; I will record nothing from it.",
    ),
    "column_caption": (
        COLUMN_CAPTION,
        "The Arabic instruction column, photographed as it is on the paper; "
        "the corresponding rows are in the card.",
    ),
    "missing_unit": ("بدون وحدة", "no unit"),
    "printed_flag": ("؛ علامة مطبوعة: ", "; printed flag: "),
}


def shift_warning(language: str) -> str:
    return PHOTO_WORDING["shift_warning"][effective(language, audience="doctor") == "en"]


def photo_supported_scope(language: str) -> str:
    return PHOTO_WORDING["photo_supported_scope"][effective(language, audience="doctor") == "en"]


def handwriting_reply(language: str) -> str:
    return PHOTO_WORDING["handwriting_reply"][effective(language, audience="doctor") == "en"]


def single_reader_warning(language: str) -> str:
    return PHOTO_WORDING["single_reader_warning"][effective(language, audience="doctor") == "en"]


def agreement_warning(language: str) -> str:
    return PHOTO_WORDING["agreement_warning"][effective(language, audience="doctor") == "en"]


def column_caption(language: str) -> str:
    return PHOTO_WORDING["column_caption"][effective(language, audience="doctor") == "en"]


def two_readers(read: DocumentRead) -> bool:
    # Check both the persisted marker and actual slots, including older caches.
    return not read.single_reader and len(read.readers) == 2


def unreadable_reply(read: DocumentRead, language: str = "ar") -> str:
    if not two_readers(read):
        return handwriting_reply(language) + "\n" + single_reader_warning(language)
    return handwriting_reply(language) + (
        "\n" + agreement_warning(language) if unreadable_read(read) else ""
    )


def render_card(proposal: "Proposal", language: str | None = None) -> tuple[str, ...]:
    """Keep the accepted layout behind the minimum-independent-read gate."""
    from sanad.scribe.card import render_card as accepted_card

    if proposal.photo and unreadable_read(proposal.photo.reads):
        return (unreadable_reply(proposal.photo.reads, language or proposal.language),)
    return accepted_card(proposal, language)


def unreadable_read(read: DocumentRead) -> bool:
    if not two_readers(read):
        return True
    total = max(len(read.first.items), len(read.second.items))
    return total == 0 or agreed_rows(read) < (total + 1) // 2


class PhotoReview(_BoundaryValue):
    reads: DocumentRead
    kind: Literal["prescription", "lab", "other"]
    resolved_fields: tuple[str, ...] = ()
    row_targets: tuple[str, ...] = ()
    row_items: tuple[DocumentItem, ...] = ()
    single_rows: tuple[int, ...] = ()
    unresolved_rows: tuple[int, ...] = ()
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
    if read.first.document_type == read.second.document_type == "prescription":
        return projected_items(read)[0]
    return tuple(r.item for r in read.first.items) + tuple(
        DocumentItem(name=None)
        for _ in range(max(0, len(read.second.items) - len(read.first.items)))
    )


def projected_items(
    read: DocumentRead,
) -> tuple[tuple[DocumentItem, ...], tuple[str, ...], tuple[int, ...]]:
    """Keep source reads immutable; projected field choices refer to aligned row indices."""
    items, resolved, single = [], [], []
    prescription = read.first.document_type == read.second.document_type == "prescription"
    for index, (left, right) in enumerate(row_assignment(read.first, read.second)):
        a = read.first.items[left].item if left is not None else DocumentItem(name=None)
        b = read.second.items[right].item if right is not None else DocumentItem(name=None)
        values = a.model_dump()
        if prescription or (left is not None and right is not None):
            if readable(a.name) != readable(b.name):
                single.append(index)
                if not readable(a.name):
                    values = b.model_dump()
                resolved.append(f"items.{index}.name")
            if prescription:
                from sanad.media.vision import compatible_dose, quantity

                for field in ("dose", "frequency", "route", "timing"):
                    av, bv = getattr(a, field), getattr(b, field)
                    if av != bv and (
                        (field == "dose" and compatible_dose(av, bv)) or not av or not bv
                    ):
                        values[field] = (
                            max(
                                (av or "", bv or ""),
                                key=lambda value: (
                                    bool(quantity(value)) if field == "dose" else False,
                                    len(value),
                                ),
                            )
                            or None
                        )
                        resolved.append(f"items.{index}.{field}")
        items.append(DocumentItem.model_validate(values))
    return tuple(items), tuple(resolved), tuple(single)


def prescription_projection(
    items: tuple[DocumentItem, ...],
    lookup: Callable[[str], "DrugLookup"] | None = None,
) -> tuple[DictationCandidate, tuple[str, ...], tuple[int, ...]]:
    from sanad.scribe.names import entry_for
    from sanad.scribe.resolver import resolve_name

    orders: list[OrderCandidate] = []
    missions: list[MissionCandidate] = []
    facts: list[FactCandidate] = []
    targets: list[str] = []
    unresolved: list[int] = []
    for index, row in enumerate(items):
        name = row.name or ""
        drug = lookup(name).found if lookup and readable(name) else bool(entry_for(name))
        resolved = resolve_name(name, "finding", name)
        from sanad.safety.labs import ALIASES, EXTRA_PANEL_WORDS, canonical

        analytes = set(ALIASES.values()) | set(EXTRA_PANEL_WORDS)
        test = (
            bool(
                resolved.tier in {"seed", "memory", "clinic"}
                and canonical(resolved.latin or name) in analytes
            )
            or canonical(name) in analytes
            or bool(re.fullmatch(r"echo|ECG|X-ray|ultrasound|CT|MRI|Holter", name, re.I))
        )
        if drug:
            targets.append(f"order:{len(orders)}")
            orders.append(
                OrderCandidate(
                    action="start",
                    drug=name,
                    dose=" ".join(v for v in (row.dose, row.unit) if v) or None,
                    frequency=row.frequency,
                    route=row.route,
                    timing=row.timing,
                )
            )
        elif test and readable(name):
            targets.append(f"mission:{len(missions)}")
            missions.append(MissionCandidate(kind="TEST", text=name))
        else:
            targets.append(f"fact:{len(facts)}")
            facts.append(FactCandidate(category="history", text=name or "[unreadable]"))
            unresolved.append(index)
    return (
        DictationCandidate(orders=tuple(orders), missions=tuple(missions), facts=tuple(facts)),
        tuple(targets),
        tuple(unresolved),
    )


def prescription_items(read: DocumentRead) -> tuple[OrderCandidate, ...]:
    return prescription_projection(first_items(read))[0].orders


def edit_prescription(
    candidate: DictationCandidate,
    review: PhotoReview,
    index: int,
    field: str,
    value: str | None,
) -> tuple[DictationCandidate, PhotoReview]:
    """Reclassify one explicitly edited source row and rebuild all target indices together."""
    from sanad.scribe.lookup import DrugLookup
    from sanad.scribe.names import entry_for

    rows = list(review.row_items or first_items(review.reads))
    if not 0 <= index < len(rows):
        return candidate, review
    target = row_target(review, index)
    old_orders = {
        i: candidate.orders[int(t.split(":")[1])]
        for i, t in enumerate(review.row_targets)
        if t.startswith("order:")
    }
    if not review.row_targets:
        old_orders = dict(enumerate(candidate.orders))
    key = "dose" if field == "quantity" else field
    if field == "row" and value:
        rows[index] = DocumentItem.model_validate_json(value)
    elif key in {"name", "dose", "frequency", "route", "timing", "unit"}:
        if key == "dose" and value and re.fullmatch(r"[0-9.]+", value):
            unit = re.sub(r"^[0-9.]+\s*", "", rows[index].dose or "")
            value += " " + unit if unit else ""
        if key == "unit":
            dose = re.sub(r"[^0-9.]+.*$", "", rows[index].dose or "")
            value = " ".join(v for v in (dose, value) if v) or None
            key = "dose"
        rows[index] = rows[index].model_copy(update={key: value})
    elif field in {"action", "duration"} and target.startswith("order:"):
        old_orders[index] = OrderCandidate.model_validate(
            old_orders[index].model_dump() | {field: value}
        )
    known = {o.drug.casefold() for o in candidate.orders}
    projected, targets, unresolved = prescription_projection(
        tuple(rows),
        lambda name: DrugLookup(found=name.casefold() in known or entry_for(name) is not None),
    )
    orders = list(projected.orders)
    for row, target in enumerate(targets):
        if target.startswith("order:") and row in old_orders:
            position = int(target.split(":")[1])
            old = old_orders[row]
            orders[position] = old.model_copy(
                update={
                    key: getattr(orders[position], key)
                    for key in ("drug", "dose", "frequency", "route", "timing")
                }
            )
    mapped = (
        set(review.row_targets) if review.row_targets else {f"order:{i}" for i in range(len(rows))}
    )
    return candidate.model_copy(
        update={
            "orders": (
                *orders,
                *(o for i, o in enumerate(candidate.orders) if f"order:{i}" not in mapped),
            ),
            "missions": (
                *projected.missions,
                *(m for i, m in enumerate(candidate.missions) if f"mission:{i}" not in mapped),
            ),
            "facts": (
                *projected.facts,
                *(f for i, f in enumerate(candidate.facts) if f"fact:{i}" not in mapped),
            ),
        }
    ), review.model_copy(
        update={"row_items": tuple(rows), "row_targets": targets, "unresolved_rows": unresolved}
    )


def reading_text(value: str | None, field: str) -> str | None:
    if value and field.endswith(".row"):
        row = DocumentItem.model_validate_json(value)
        return " ".join(v for v in (row.name, row.dose, row.frequency, row.route, row.timing) if v)
    return value


def row_target(review: PhotoReview, index: int) -> str:
    if review.row_targets:
        return review.row_targets[index]
    return f"{'order' if review.kind == 'prescription' else 'fact'}:{index}"


def source_row(review: PhotoReview, target: str) -> int | None:
    if review.row_targets:
        return review.row_targets.index(target) if target in review.row_targets else None
    return int(target.split(":")[1])


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


def lab_text(row: LabRowCandidate, language: str = "ar") -> str:
    return " ".join(
        (
            row.analyte,
            row.value or "",
            row.unit
            or PHOTO_WORDING["missing_unit"][effective(language, audience="doctor") == "en"],
        )
    ) + (
        PHOTO_WORDING["printed_flag"][effective(language, audience="doctor") == "en"] + row.flag
        if row.flag
        else ""
    )


def candidate_from(read: DocumentRead, kind: str, policy: SafetyPolicy) -> DictationCandidate:
    if unreadable_read(read):
        return DictationCandidate()
    if kind == "prescription":
        return prescription_projection(first_items(read))[0]
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
            issues.append(ProposalIssue(item=row_target(review, i), code="reader_disagreement"))
    for i in review.unresolved_rows:
        issues.append(ProposalIssue(item=row_target(review, i), code="drug_unclear"))
    for disagreement in review.reads.disagreements:
        if disagreement.field in review.resolved_fields:
            continue
        if disagreement.field.startswith("items."):
            index = int(disagreement.field.split(".")[1])
            issues.append(
                ProposalIssue(
                    item=row_target(review, index),
                    code="reader_disagreement",
                    field=disagreement.field.split(".")[-1],
                )
            )
        elif disagreement.field != "printed_identity_hint":
            issues.append(ProposalIssue(item="all", code="reader_disagreement"))
    if review.shift_detected:
        issues.extend(
            ProposalIssue(item=row_target(review, i), code="shifted_rows")
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
