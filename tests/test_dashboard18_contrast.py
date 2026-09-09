"""Compute every rendered token combination, including controls and status badges."""

import re
from pathlib import Path

import pytest

CSS = Path(__file__).parents[1] / "src/sanad/web/static/browser.css"


def luminance(color: str) -> float:
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
    return sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def tokens(theme: str) -> dict[str, str]:
    css = CSS.read_text()
    light = re.search(r":root,\[data-theme=light\]\{([^}]+)", css)
    dark = re.search(r"\[data-theme=dark\]\{([^}]+)", css)
    assert light and dark
    result = dict(re.findall(r"--([a-z-]+):(#[a-fA-F0-9]{6})(?:;|})", light[1] + "}"))
    if theme == "dark":
        result.update(dict(re.findall(r"--([a-z-]+):(#[a-fA-F0-9]{6})(?:;|})", dark[1] + "}")))
    return result


def pairs() -> list[tuple[str, str, float]]:
    # Text roles appear on background, work surface and raised sections; badges
    # deliberately keep the work-surface background, with no translucent wash.
    result = [
        (fg, bg, 4.5)
        for fg in (
            "text",
            "text-muted",
            "accent-text",
            "danger-text",
            "warning-text",
            "success-text",
            "link",
            "validation",
        )
        for bg in ("bg", "surface", "raised")
    ]
    result += [("text", state, 4.5) for state in ("hover", "selected", "pressed")]
    result += [("disabled-text", "disabled-bg", 4.5), ("on-accent", "accent-fill", 4.5)]
    result += [
        ("focus", bg, 3) for bg in ("bg", "surface", "raised", "hover", "selected", "pressed")
    ]
    result += [
        (fg, bg, 3)
        for fg in ("border-control", "input-boundary")
        for bg in ("bg", "surface", "hover", "pressed", "disabled-bg")
    ]
    return result


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_all_rendered_contrast_pairs(theme: str) -> None:
    values = tokens(theme)
    for foreground, background, minimum in pairs():
        ratio = contrast(values[foreground], values[background])
        assert ratio >= minimum, (theme, foreground, background, ratio, minimum)
