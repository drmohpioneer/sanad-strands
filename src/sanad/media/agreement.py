"""Literal name comparison shared by the photo gate and offline scorer."""

import re
import unicodedata

from sanad.media.vision import DocumentRead


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


def agreed_rows(read: DocumentRead) -> int:
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
        for reader in (read.first, read.second)
    ]
    matches = [
        [j for j, b in enumerate(names[1]) if a and b and within_two_edits(a, b)] for a in names[0]
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

    return sum(match(i, set()) for i in range(len(names[0])))
