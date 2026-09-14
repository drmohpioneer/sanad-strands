"""Header-only inspection: bounded bytes, dimensions and pixel count; no pixel decode."""

import struct
import zlib
from io import BytesIO
from typing import Literal
from zipfile import BadZipFile, ZipFile

from sanad.domain.boundaries import _BoundaryValue

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_DIMENSION = 8000
MAX_PIXELS = 20_000_000
MAX_AUDIO_SECONDS = 300.0
MAX_DOCUMENT_BYTES = 20_000_000
MAX_DOCUMENT_PAGES = 10


class ImageInfo(_BoundaryValue):
    format: Literal["png", "jpeg"]
    mime: str
    width: int
    height: int


class MediaInvalid(ValueError):
    """Only fixed reason codes are allowed in this exception."""


def sniff(data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "doc"
    if data.startswith(b"PK\x03\x04") and len(data) <= MAX_DOCUMENT_BYTES:
        try:
            with ZipFile(BytesIO(data)) as archive:
                if "word/document.xml" in archive.namelist():
                    return "docx"
        except (BadZipFile, ValueError):
            pass
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if data[4:8] == b"ftyp":
        brands = data[8:64]
        if any(brand in brands for brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1")):
            return "heif"
        if b"avif" in brands or b"avis" in brands:
            return "avif"
        return "m4a"
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 255 and data[1] & 224 == 224):
        return "mp3"
    raise MediaInvalid("unsupported_type")


def image_info(data: bytes) -> ImageInfo:
    if len(data) > MAX_IMAGE_BYTES:
        raise MediaInvalid("too_large")
    fmt = sniff(data)
    width = height = 0
    if fmt == "png":
        pos, seen_header, seen_data, ended = 8, False, False, False
        while pos + 12 <= len(data):
            size = int.from_bytes(data[pos : pos + 4], "big")
            end = pos + 12 + size
            if end > len(data):
                raise MediaInvalid("invalid_image")
            kind, chunk = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + size]
            if zlib.crc32(kind + chunk) != int.from_bytes(data[end - 4 : end], "big"):
                raise MediaInvalid("invalid_image")
            if not seen_header:
                if kind != b"IHDR" or size != 13:
                    raise MediaInvalid("invalid_image")
                width, height = struct.unpack(">II", chunk[:8])
                seen_header = True
            elif kind == b"IHDR" or kind == b"acTL":
                raise MediaInvalid("invalid_image")
            seen_data |= kind == b"IDAT"
            if kind == b"IEND":
                ended = size == 0 and end == len(data)
                break
            pos = end
        if not ended or not seen_data:
            raise MediaInvalid("invalid_image")
    elif fmt == "jpeg":
        pos = 2
        if not data.endswith(b"\xff\xd9"):
            raise MediaInvalid("invalid_image")
        while pos + 4 <= len(data):
            if data[pos] != 255:
                raise MediaInvalid("invalid_image")
            while pos < len(data) and data[pos] == 255:
                pos += 1
            if pos >= len(data):
                break
            marker = data[pos]
            pos += 1
            if marker == 0xDA:
                break
            if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            length = int.from_bytes(data[pos : pos + 2], "big")
            if length < 2 or pos + length > len(data):
                raise MediaInvalid("invalid_image")
            if marker in {0xC0, 0xC1, 0xC2}:
                if length < 8 or width:
                    raise MediaInvalid("invalid_image")
                height, width = struct.unpack(">HH", data[pos + 3 : pos + 7])
            pos += length
    else:
        raise MediaInvalid("unsupported_type")
    if min(width, height) <= 0:
        raise MediaInvalid("invalid_image")
    if max(width, height) > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise MediaInvalid("dimensions_exceeded")
    return ImageInfo(
        format="png" if fmt == "png" else "jpeg", mime="image/" + fmt, width=width, height=height
    )
