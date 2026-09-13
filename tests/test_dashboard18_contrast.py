"""Logical geometry stays independent of the owner's readability report."""

import re
from pathlib import Path

CSS = Path(__file__).parents[1] / "src/sanad/web/static/browser.css"


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
