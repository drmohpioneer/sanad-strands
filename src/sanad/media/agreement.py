"""Literal name comparison shared by the photo gate and offline scorer."""

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sanad.media.vision import DocumentRead, ReaderResult


def normalized_name(value: str) -> str:
    return re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def within_two_edits(first: str, second: str) -> bool:
    """Literal normalized Levenshtein distance, without drug-resolution knowledge."""
    if abs(len(first) - len(second)) > 2:
        return False
    previous = list(range(len(second) + 1))
    for i, a in enumerate(first, 1):
        current = [i]
        for j, b in enumerate(second, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1] <= 2


def name_assignment(first: "ReaderResult", second: "ReaderResult") -> dict[int, int]:
    """Maximum one-to-one matches; repeated names never reuse the same row."""
    names = [
        [
            normalized_name(row.item.name)
            if row.item.name
            and "[unreadable]" not in row.item.name.casefold()
            and "[غير مقروء]" not in row.item.name
            else ""
            for row in reader.items
        ]
        for reader in (first, second)
    ]
    matches = [
        sorted(
            [j for j, b in enumerate(names[1]) if a and b and within_two_edits(a, b)],
            key=lambda j: names[1][j] != a,
        )
        for a in names[0]
    ]
    assigned: dict[int, int] = {}

    def match(index: int, seen: set[int]) -> bool:
        for other in matches[index]:
            if other in seen:
                continue
            seen.add(other)
            if other not in assigned or match(assigned[other], seen):
                assigned[other] = index
                return True
        return False

    for i in range(len(names[0])):
        match(i, set())
    return {i: j for j, i in assigned.items()}


def agreed_rows(read: "DocumentRead") -> int:
    return len(name_assignment(read.first, read.second))


def readable(name: str | None) -> bool:
    return bool(
        name
        and name.strip()
        and "[unreadable]" not in name.casefold()
        and "[غير مقروء]" not in name
    )


def row_assignment(
    first: "ReaderResult", second: "ReaderResult"
) -> tuple[tuple[int | None, int | None], ...]:
    """Name matches first; unreadable leftovers pair in their remaining printed order."""
    paired = name_assignment(first, second)
    left = [i for i in range(len(first.items)) if i not in paired]
    right = [j for j in range(len(second.items)) if j not in paired.values()]
    for i, j in zip(left, right, strict=False):
        if not readable(first.items[i].item.name) or not readable(second.items[j].item.name):
            paired[i] = j
    return tuple((i, paired.get(i)) for i in range(len(first.items))) + tuple(
        (None, j) for j in range(len(second.items)) if j not in paired.values()
    )
