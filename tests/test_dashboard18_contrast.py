"""Measured 18e text, boundaries, glass and explicitly exempt decorative pairs."""

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


def composite(rgb: str, alpha: float, background: str) -> str:
    foreground = [int(v) for v in rgb.split(",")]
    base = [int(background[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(
        f"{round(a * alpha + b * (1 - alpha)):02x}" for a, b in zip(foreground, base, strict=True)
    )


def tokens(theme: str) -> dict[str, str]:
    css = CSS.read_text()
    light = re.search(r":root,\[data-theme=light\]\{([^}]+)", css)
    dark = re.search(r"\[data-theme=dark\]\{([^}]+)", css)
    assert light and dark
    blocks = light[1] + (dark[1] if theme == "dark" else "")
    result = dict(re.findall(r"--([a-z0-9-]+):\s*(#[a-fA-F0-9]{6})", blocks))
    result["glass-composite"] = composite(
        ",".join(str(int(result["s2"][i : i + 2], 16)) for i in (1, 3, 5)), 0.85, result["s0"]
    )
    return result


def pairs() -> list[tuple[str, str, float]]:
    surfaces = ("s0", "s1", "s2", "s3")
    result = [
        (fg, bg, 4.5)
        for fg in (
            "text",
            "text-secondary",
            "brand-text",
            "danger-text",
            "warning-text",
            "success-text",
            "info-text",
            "teal-text",
        )
        for bg in surfaces
    ]
    # The verbatim light muted token fails 4.5 on s0 AND s1; those roles use secondary.
    result += [("text-muted", bg, 4.5) for bg in ("s2", "s3")]
    result += [("text", bg, 4.5) for bg in ("hover", "selected", "pressed")]
    result += [("text-secondary", bg, 4.5) for bg in ("danger-tint", "warning-tint", "selected")]
    result += [("on-" + role, role + "-solid", 4.5) for role in ("brand", "teal", "danger")]
    result += [
        (role + "-on-tint", role + "-tint", 4.5)
        for role in ("danger", "warning", "success", "info", "neutral")
    ]
    result += [(role + "-text", role + "-tint", 4.5) for role in ("danger", "warning")]
    result += [
        (fg, bg, 3)
        for fg in ("border-strong", "focus")
        for bg in (*surfaces, "hover", "selected", "pressed")
    ]
    result += [("border-strong", "disabled-bg", 3)]
    result += [("text-secondary", "brand-tint", 4.5), ("text", "brand-tint", 4.5)]
    result += [("focus", bg + "-tint", 3) for bg in ("danger", "warning")]
    result += [
        (fg, "glass-composite", 3 if fg == "focus" else 4.5)
        for fg in ("text", "text-secondary", "text-muted", "brand-text", "teal-text", "focus")
    ]
    return result


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_all_rendered_contrast_pairs(theme: str) -> None:
    values = tokens(theme)
    for foreground, background, minimum in pairs():
        ratio = contrast(values[foreground], values[background])
        assert ratio >= minimum, (theme, foreground, background, ratio, minimum)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_documented_decorative_and_disabled_floors(theme: str) -> None:
    values = tokens(theme)
    # WCAG 1.4.11: content dividers do not identify controls. The neutral s1/s3
    # pair is the limiting background, so use a measured 1.1 floor in both themes.
    for surface in ("s1", "s2", "s3"):
        assert contrast(values["border-subtle"], values[surface]) >= 1.1
    # Chip text identifies its state. The edge is decorative, never a focus ring.
    for role in ("brand", "danger", "warning", "success", "info", "neutral"):
        assert contrast(values[role + "-edge"], values[role + "-tint"]) >= 1.3
    # Inactive controls are exempt; keep the label distinguishable at a 2:1 floor.
    assert contrast(values["text-disabled"], values["disabled-bg"]) >= 2


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_surface_separation(theme: str) -> None:
    values = tokens(theme)
    assert contrast(values["s0"], values["s2"]) >= 1.12
    if theme == "dark":
        assert contrast(values["s2"], values["s3"]) > 1


def test_css_uses_logical_geometry() -> None:
    # Media width queries and orientation-specific transforms are not declarations.
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)
    properties = re.findall(r"(?:[;{])\s*([a-z-]+)\s*:", css)
    forbidden = {
        "left",
        "right",
        "top",
        "bottom",
        "width",
        "height",
        "min-width",
        "max-width",
        "min-height",
        "max-height",
        "margin-left",
        "margin-right",
        "margin-top",
        "margin-bottom",
        "padding-left",
        "padding-right",
        "padding-top",
        "padding-bottom",
        "border-left",
        "border-right",
        "border-top",
        "border-bottom",
        "inset",
    }
    assert not forbidden.intersection(properties)
