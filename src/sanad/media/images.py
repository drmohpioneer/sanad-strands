"""Bounded, deterministic document normalization and pixel-only instruction crops."""

from dataclasses import dataclass, field
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif.as_plugin import register_heif_opener

from sanad.media.limits import (
    MAX_DIMENSION,
    MAX_IMAGE_BYTES,
    MAX_PIXELS,
    MediaInvalid,
    image_info,
)

register_heif_opener()
NORMALIZATION_VERSION = "document-image-v1"
UPSCALE_LONG_SIDE = 2600


def _dimensions(size: tuple[int, int]) -> None:
    width, height = size
    if min(size) <= 0:
        raise MediaInvalid("invalid_image")
    if max(size) > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise MediaInvalid("dimensions_exceeded")


def _open(data: bytes) -> Image.Image:
    if len(data) > MAX_IMAGE_BYTES:
        raise MediaInvalid("too_large")
    try:
        result = Image.open(BytesIO(data))
        try:
            _dimensions(result.size)
            # EPS delegates decoding to an external executable. Documents here
            # must be a single raster page, decoded inside the bounded library.
            if result.format == "EPS" or getattr(result, "n_frames", 1) != 1:
                raise MediaInvalid("unsupported_type")
        except Exception:
            result.close()
            raise
        return result
    except MediaInvalid:
        raise
    except UnidentifiedImageError:
        raise MediaInvalid("unsupported_type") from None
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise MediaInvalid("dimensions_exceeded") from None
    except (OSError, ValueError, SyntaxError):
        raise MediaInvalid("invalid_image") from None


def source_image_mime(data: bytes) -> str:
    """Inspect bounded image headers before pixel decode; do not call image_info on HEIF."""
    with _open(data) as source:
        return Image.MIME.get(source.format or "", "image/" + (source.format or "unknown").lower())


def _jpeg(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="JPEG", quality=85, optimize=False, progressive=False)
    data = output.getvalue()
    image_info(data)  # Conversion output retains the byte, dimension and pixel caps.
    return data


def normalize_document(data: bytes) -> bytes:
    """Applied once at MediaWork.normalize; recovery reads the stored derivative."""
    try:
        with _open(data) as source:
            oriented = ImageOps.exif_transpose(source)
            gray = ImageOps.autocontrast(oriented.convert("L"), cutoff=1)
            width, height = gray.size
            long_side = max(gray.size)
            # This ceiling bounds enlargement, never shrinks an already larger upload.
            scale = max(1.0, min(2.0, UPSCALE_LONG_SIDE / long_side))
            # Respect the existing pixel cap even for an almost-square large page.
            scale = min(scale, (MAX_PIXELS / (width * height)) ** 0.5)
            size = (int(width * scale), int(height * scale))
            if size != gray.size:
                gray = gray.resize(size, Image.Resampling.LANCZOS)
            return _jpeg(gray)
    except MediaInvalid:
        raise
    except (OSError, ValueError, SyntaxError):
        raise MediaInvalid("conversion_failed") from None


@dataclass(frozen=True)
class InstructionCrop:
    data: bytes = field(repr=False)
    box: tuple[int, int, int, int]
    layout: str
    row_bands: tuple[tuple[int, int], ...]


# Fractions are geometry only; no model decides the box or reads these pixels.
# Known screenshot layout is selected by its delivered aspect ratio. Other
# pages use their entire width and the rightmost 35 percent instruction column.
FORM_TABLES = {
    "page": (0.0, 0.0, 1.0, 1.0),
    "opd_screenshot": (0.17, 0.30, 0.83, 0.70),
}


def instruction_column(data: bytes, *, layout: str | None = None) -> InstructionCrop:
    with _open(data) as page:
        gray = page.convert("L")
        width, height = gray.size
        selected = layout or (
            "opd_screenshot" if abs(width / height - 1179 / 2556) < 0.01 else "page"
        )
        if selected not in FORM_TABLES:
            raise MediaInvalid("unknown_image_layout")
        x0, y0, x1, y1 = FORM_TABLES[selected]
        left, top = int(x0 * width), int(y0 * height)
        right, bottom = max(left + 1, int(x1 * width)), max(top + 1, int(y1 * height))
        # Detect horizontal ink bands across the table, joining nearby scanlines.
        # Exclude nearly solid screenshot borders/table rules from the projection.
        ink = gray.crop((left, top, right, bottom)).point(lambda p: 255 if p < 150 else 0)
        bands: list[tuple[int, int]] = []
        gap = max(1, height // 500)
        for y in range(ink.height):
            count = ink.crop((0, y, ink.width, y + 1)).histogram()[255]
            if max(1, ink.width // 100) <= count < ink.width * 0.9:
                absolute = top + y
                if bands and absolute - bands[-1][1] <= gap:
                    bands[-1] = (bands[-1][0], absolute + 1)
                else:
                    bands.append((absolute, absolute + 1))
        if bands:
            top, bottom = max(top, bands[0][0] - gap), min(bottom, bands[-1][1] + gap)
        column_left = left + int((right - left) * 0.65)
        box = (min(column_left, right - 1), top, right, bottom)
        return InstructionCrop(_jpeg(gray.crop(box)), box, selected, tuple(bands))
