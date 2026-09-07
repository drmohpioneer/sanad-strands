"""Generated, correctly shaped Arabic control and synthetic document pixels."""

from io import BytesIO
from pathlib import Path

import arabic_reshaper  # type: ignore[import-untyped]
from bidi.algorithm import get_display  # type: ignore[import-untyped]
from PIL import Image, ImageDraw, ImageFont

FONT = Path(__file__).parents[1] / "data/fonts/DejaVuSans.ttf"
CONTROL_PHRASES = (
    "مع بدء الفطار",
    "صباحا ومساء",
    "مرتين في اليوم",
    "مع الفطار",
    "صباحا",
    "بعد العشاء",
)


def control_image() -> bytes:
    canvas = Image.new("RGB", (1000, 650), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(str(FONT), 40)
    for i, phrase in enumerate(CONTROL_PHRASES):
        draw.text((30, 40 + i * 95), f"Control {i + 1}", font=font, fill="black")
        draw.text(
            (400, 40 + i * 95),
            get_display(arabic_reshaper.reshape(phrase)),
            font=font,
            fill="black",
        )
    output = BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


def paper(fmt: str = "PNG", *, size: tuple[int, int] = (240, 320)) -> bytes:
    canvas = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(canvas)
    for i in range(1, 7):
        y = i * size[1] // 8
        draw.text((size[0] // 12, y), f"Medicine {i}", fill="black")
        draw.rectangle((size[0] * 3 // 4, y, size[0] * 9 // 10, y + 3), fill="black")
    output = BytesIO()
    canvas.save(output, format=fmt, **({"quality": -1} if fmt == "HEIF" else {}))
    return output.getvalue()
