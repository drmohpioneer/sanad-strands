"""Only current, explicitly stated area text can leave for geocoding."""

import pytest

from sanad.resolver.attempts import area_in


@pytest.mark.parametrize(
    "text,answering,expected",
    [
        ("The lab is too expensive in Madinaty because I have diabetes", False, "Madinaty"),
        ("I live in Madinaty and I take aspirin", True, "Madinaty"),
        ("in Madinaty with my medical records", False, "Madinaty"),
        ("في مدينتي وعندي سكر", False, "مدينتي"),
        ("Madinaty, Cairo", True, "Madinaty, Cairo"),
        ("Madinaty, Cairo", False, None),
        ("I take aspirin", True, None),
        ("my doctor prescribed aspirin", True, None),
        ("plan", True, None),
        ("thanks", True, None),
    ],
)
def test_only_the_area_clause(text: str, answering: bool, expected: str | None) -> None:
    assert area_in(text, answering=answering) == expected
