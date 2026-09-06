"""Exact transcript number support, without inferring doses or spelling out words."""

import re
from collections.abc import Iterable
from decimal import Decimal

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫–—−", "01234567890123456789.---")
_NUMBER = r"\d+(?:\.\d+)?"
_PATTERN = re.compile(rf"{_NUMBER}(?:\s*-\s*{_NUMBER})?")


def _canonical(value: str) -> str:
    return "-".join(format(Decimal(n).normalize(), "f") for n in value.replace(" ", "").split("-"))


def numbers_in(text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(_canonical(m.group()) for m in _PATTERN.finditer(text.translate(_DIGITS)))
    )


def number_mentions(text: str) -> tuple[str, ...]:
    """Keep each number and its following word, in order, including repeated mentions."""
    normalized = text.translate(_DIGITS)
    result = []
    for match in _PATTERN.finditer(normalized):
        following = re.match(r"[ \t]+([^\W\d_]+(?:/[^\W\d_]+)*|%)", normalized[match.end() :])
        word = " " + following[1] if following else ""
        result.append(_canonical(match.group()) + word)
    return tuple(result)


def unsupported_numbers(claimed: Iterable[str], transcript: str) -> tuple[str, ...]:
    present = set(numbers_in(transcript))
    present.update(n for value in tuple(present) for n in value.split("-"))
    return tuple(
        dict.fromkeys(
            claim
            for claim in claimed
            if not numbers_in(claim) or any(n not in present for n in numbers_in(claim))
        )
    )
