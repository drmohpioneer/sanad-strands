"""The printed-document gate uses saved fields only; no model calls."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from providers.fixtures import SOURCE
from sanad.media.agreement import agreed_rows, normalized_name
from sanad.media.vision import (
    DocumentItem,
    DocumentRead,
    ItemRead,
    PrintedIdentityHint,
    ReaderResult,
    diff,
)
from sanad.models.io import CallMetadata
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY
from sanad.scribe.crosscheck import (
    AGREEMENT_WARNING,
    HANDWRITING_REPLY,
    PHOTO_SUPPORTED_SCOPE,
    PhotoReview,
    candidate_from,
    review_issues,
    unreadable_read,
    unreadable_reply,
)

PRIVATE_READINGS = Path(
    "/var/folders/4c/9xfkl7sn2rbfm09fcf6kwlxm0000gn/T/"
    "sanad-11d-private-_2pv_bds/latin-readings.json"
)
NAMES = ("Alphamed", "Betatab", "Gammatest", "Deltapill", "Epsiloncap")
OTHER = ("Quartz", "Jupiter", "Winter", "Octopus", "Horizon")


def reading(first: Sequence[str | None], second: Sequence[str | None]) -> DocumentRead:
    def reader(names: Sequence[str | None], model: str) -> ReaderResult:
        return ReaderResult(
            document_type="prescription",
            printed_identity_hint=PrintedIdentityHint(text=None),
            printed_date=None,
            items=tuple(
                ItemRead(item=DocumentItem(name=name), judgment="cannot_judge") for name in names
            ),
            unreadable=False,
            notes=(),
            provenance=SOURCE,
            metadata=CallMetadata(model_id=model, policy_version="test", latency_ms=0),
        )

    a, b = reader(first, "first"), reader(second, "second")
    return DocumentRead(first=a, second=b, disagreements=diff(a, b))


@pytest.mark.parametrize(
    ("first_count", "second_count", "agreed", "editable"),
    [
        (5, 5, 3, True),  # exactly ceil(5 / 2)
        (5, 5, 4, True),
        (5, 5, 2, False),
        (4, 4, 2, True),
        (4, 4, 3, True),
        (4, 4, 1, False),
        (2, 5, 2, False),
        (3, 5, 3, True),
        (5, 2, 2, False),
        (5, 3, 3, True),
    ],
)
def test_half_rows_gate_boundaries_and_unequal_lengths(
    first_count: int, second_count: int, agreed: int, editable: bool
) -> None:
    read = reading(NAMES[:first_count], (NAMES[:agreed] + OTHER)[:second_count])
    assert agreed_rows(read) == agreed
    assert unreadable_read(read) is not editable
    review = PhotoReview(reads=read, kind="prescription", intake_id="test", media_work_ids=())
    candidate = candidate_from(read, "prescription", POLICY)
    if editable:
        assert not candidate.orders  # Unrecognized names are unresolved rows, never starts.
        assert len(candidate.facts) == first_count + second_count - agreed
        # 6d: absent counterparts are single-reader rows. Unknown names
        # remain facts, never medication starts (the row-classification route
        # supplies their clarification separately).
        issues = review_issues(review, candidate)
        assert not any(issue.code == "reader_disagreement" for issue in issues)
        assert not issues
    else:
        assert not candidate.orders and not candidate.facts
        assert unreadable_reply(read) == HANDWRITING_REPLY + "\n" + AGREEMENT_WARNING
        assert any(
            issue.item == "all" and issue.blocked for issue in review_issues(review, candidate)
        )


@pytest.mark.parametrize(
    ("first", "second", "agreed"),
    [
        (("Alphamed",) * 5, ("Alphamed", "Quartz", "Jupiter", "Winter", "Octopus"), 1),
        (("Alphamed", "Quartz", "Jupiter", "Winter", "Octopus"), ("Alphamed",) * 5, 1),
        (("Alphamed", "Alphamed"), ("Alphamed", "Alphamed"), 2),
        (("abc", "abcd"), ("abc", "a"), 2),  # needs reassignment, not greedy pairing
        (("Alphamed", "Betatab"), ("Betatab", "Alphamed"), 2),
        (("Ｆｏｏ-BAR",), ("foo.bar",), 1),
        (("Alphamed",), ("Alphamxx",), 1),
        (("Alphamed",), ("Alphaxxx",), 0),
        (
            (None, "", "---", "[UNREADABLE]", "[غير مقروء]"),
            (None, "", "---", "[UNREADABLE]", "[غير مقروء]"),
            0,
        ),
        ((), (), 0),
        (("Alphamed",), (), 0),
    ],
)
def test_row_matching_is_normalized_one_to_one_and_ignores_unreadable_names(
    first: tuple[str | None, ...], second: tuple[str | None, ...], agreed: int
) -> None:
    read = reading(first, second)
    assert agreed_rows(read) == agreed
    if not agreed:
        assert unreadable_read(read)


def test_scorer_and_gate_share_the_attempt3_normalization_and_distance() -> None:
    from live.check11d import normalized_name as scorer_normalize
    from live.check11d import within_two_edits as scorer_distance

    from sanad.media.agreement import within_two_edits

    assert scorer_normalize is normalized_name and scorer_distance is within_two_edits
    assert normalized_name("  Ｍｅｄ.Name--MR  ") == "med name mr"


def test_honest_card_discloses_printed_latin_scope_without_file_workaround() -> None:
    from sanad.scribe.photos import unreadable

    assert PHOTO_SUPPORTED_SCOPE == (
        "المدعوم حاليًا: مستندات مطبوعة أو مكتوبة بالكمبيوتر بحروف لاتينية؛ "
        "خط اليد والكتابة العربية في الصور لسه مش مدعومين."
    )
    for text in (unreadable("readers_failed"), unreadable_reply(reading(NAMES, OTHER))):
        assert PHOTO_SUPPORTED_SCOPE in text and "File" not in text and "كملف" not in text


def private_replay_counts(path: Path) -> dict[str, tuple[int, int, bool, bool]]:
    """Return counts/booleans only; private strings never enter assertions or output."""
    saved = json.loads(path.read_text())
    result = {}
    for paper in ("handwritten_note", "opd_form"):
        names = saved[paper]["names_before_resolution"]
        if (
            not isinstance(names, list)
            or len(names) != 2
            or any(not isinstance(rows, list) for rows in names)
            or any(
                name is not None and not isinstance(name, str) for rows in names for name in rows
            )
        ):
            raise ValueError("private replay requires two saved name-field lists")
        read = reading(*names)
        candidate = candidate_from(read, "prescription", POLICY)
        result[paper] = (
            agreed_rows(read),
            max(map(len, names)),
            unreadable_read(read)
            and unreadable_reply(read) == HANDWRITING_REPLY + "\n" + AGREEMENT_WARNING,
            not candidate.orders and not candidate.facts,
        )
    return result


@pytest.mark.skipif(not PRIVATE_READINGS.is_file(), reason="private attempt-3 readings absent")
def test_saved_attempt3_papers_both_receive_honest_card() -> None:
    outcomes = private_replay_counts(PRIVATE_READINGS)
    assert set(outcomes) == {"handwritten_note", "opd_form"}
    for agreed, total, honest_card, no_candidates in outcomes.values():
        assert agreed < (total + 1) // 2
        assert honest_card and no_candidates
