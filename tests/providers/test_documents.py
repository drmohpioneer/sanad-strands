"""Synthetic PDF boundary checks; no provider, account or patient data."""

import subprocess
import sys
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from PIL import Image, ImageDraw

from sanad.media import documents
from sanad.media.documents import render_page, validate_document
from sanad.media.limits import MAX_DOCUMENT_BYTES, MediaInvalid, image_info, sniff


def synthetic_pdf(pages: int) -> bytes:
    images = []
    for index in range(pages):
        image = Image.new("RGB", (400, 600), "white")
        ImageDraw.Draw(image).text((20, 20), f"Synthetic result page {index + 1}", fill="black")
        images.append(image)
    stream = BytesIO()
    images[0].save(stream, "PDF", save_all=True, append_images=images[1:])
    for image in images:
        image.close()
    return stream.getvalue()


@pytest.mark.parametrize("pages", [1, 10])
def test_pdf_validation_and_rendering(pages: int) -> None:
    data = synthetic_pdf(pages)
    assert validate_document(data).pages == pages
    for index in range(1, pages + 1):
        page = render_page(data, index)
        assert not page.blank
        info = image_info(page.data)
        assert info.format == "png"
        assert max(info.width, info.height) <= 2000


def test_pdf_byte_ceiling_precedes_child_start() -> None:
    with pytest.raises(MediaInvalid, match="^document_too_large$"):
        validate_document(b"%PDF-" + bytes(MAX_DOCUMENT_BYTES))


@pytest.mark.parametrize(
    "data,expected", [(b"%PDF-1.7", "pdf"), (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "doc")]
)
def test_documents_remain_unsupported_by_image_info(data: bytes, expected: str) -> None:
    assert sniff(data) == expected
    with pytest.raises(MediaInvalid, match="^unsupported_type$"):
        image_info(data)


def test_word_identification_uses_only_zip_directory() -> None:
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("word/document.xml", "not parsed")
    assert sniff(data.getvalue()) == "docx"
    with pytest.raises(MediaInvalid, match="^unsupported_type$"):
        image_info(data.getvalue())
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("other.xml", "not a document")
    with pytest.raises(MediaInvalid, match="^unsupported_type$"):
        sniff(data.getvalue())


@pytest.mark.parametrize("platform", ["linux", "freebsd"])
def test_address_space_limit_failure_is_closed(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []

    def limit(kind: int, bounds: tuple[int, int]) -> None:
        calls.append((kind, bounds))
        raise ValueError("limit unavailable")

    resource = SimpleNamespace(RLIMIT_AS=123, setrlimit=limit)
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(ValueError, match="limit unavailable"):
        exec(documents._CHILD.split("def deny")[0], {})
    assert calls == [(123, (1500000000, 1500000000))]


def test_linux_applies_exact_address_space_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    resource = SimpleNamespace(
        RLIMIT_AS=123, setrlimit=lambda kind, bounds: calls.append((kind, bounds))
    )
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(sys, "platform", "linux")
    exec(documents._CHILD.split("def deny")[0], {})
    assert calls == [(123, (1500000000, 1500000000))]


@pytest.mark.parametrize("returncode", [1, -9])
def test_failed_child_cannot_return_a_document(
    monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], returncode, b"", b""),
    )
    with pytest.raises(MediaInvalid, match="^document_invalid$"):
        validate_document(b"%PDF-1.7")
    with pytest.raises(MediaInvalid, match="^document_unreadable$"):
        render_page(b"%PDF-1.7", 1)


@pytest.mark.parametrize(
    "data,reason",
    [
        (b"%PDF-1.7\n%%EOF", "document_invalid"),
        (b"%PDF-1.7 truncated", "document_invalid"),
        (b"garbage", "document_invalid"),
    ],
)
def test_invalid_documents_are_refused(data: bytes, reason: str) -> None:
    with pytest.raises(MediaInvalid, match="^" + reason + "$"):
        validate_document(data)


def test_eleven_pages_are_refused_before_render() -> None:
    with pytest.raises(MediaInvalid, match="^document_too_many_pages$"):
        validate_document(synthetic_pdf(11))


def test_validation_timeout_is_file_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def timed_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("pdf-child", 5)

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(MediaInvalid, match="^document_invalid$"):
        validate_document(b"%PDF-")
    with pytest.raises(MediaInvalid, match="^document_unreadable$"):
        render_page(b"%PDF-", 1)


def test_blank_page_is_rendered_and_recorded() -> None:
    stream = BytesIO()
    with Image.new("RGB", (300, 600), "white") as blank:
        blank.save(stream, "PDF")
    assert validate_document(stream.getvalue()).pages == 1
    assert render_page(stream.getvalue(), 1).blank


def test_renderer_version_cannot_be_silently_upgraded() -> None:
    with pytest.raises(MediaInvalid, match="^document_unreadable$"):
        render_page(synthetic_pdf(1), 1, renderer_version="old-renderer")


def test_password_error_mapping_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub PDFium's password error inside the actual isolated child. No extra PDF library.
    stub = """
import types
fake = types.ModuleType("pypdfium2")
class PdfiumError(Exception):
    err_code = 4
def protected(data):
    raise PdfiumError()
fake.PdfiumError = PdfiumError
fake.PdfDocument = protected
raw = types.ModuleType("pypdfium2.raw")
raw.FPDF_ERR_PASSWORD = 4
fake.raw = raw
sys.modules["pypdfium2"] = fake
sys.modules["pypdfium2.raw"] = raw
"""
    child = documents._CHILD.replace(
        "try:\n    import pypdfium2 as pdfium", stub + "\ntry:\n    import pypdfium2 as pdfium", 1
    )
    monkeypatch.setattr(documents, "_CHILD", child)
    with pytest.raises(MediaInvalid, match="^document_encrypted$"):
        validate_document(b"%PDF-1.7\n%%EOF")


def tiny_pdf(*, rotation: int = 0, empty: bool = False) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 0 /Kids [] >>"
        if empty
        else b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
    ]
    if not empty:
        objects += [
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 200] /Rotate {rotation}"
                " /Resources << >> /Contents 4 0 R >>"
            ).encode(),
            b"<< /Length 24 >>\nstream\n0 0 0 rg 10 10 20 20 re f\nendstream",
        ]
    data = b"%PDF-1.7\n"
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets)
    return (
        data
        + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )


def test_zero_page_pdf_is_invalid() -> None:
    with pytest.raises(MediaInvalid, match="^document_invalid$"):
        validate_document(tiny_pdf(empty=True))


def test_pdf_rotation_is_honoured() -> None:
    normal = image_info(render_page(tiny_pdf(), 1).data)
    rotated = image_info(render_page(tiny_pdf(rotation=90), 1).data)
    assert (normal.width, normal.height) == (1000, 2000)
    assert (rotated.width, rotated.height) == (2000, 1000)


@pytest.mark.parametrize("always_large", [False, True])
def test_png_ceiling_scales_down_then_fails_closed(
    monkeypatch: pytest.MonkeyPatch, always_large: bool
) -> None:
    injected = f"""
    from PIL import Image
    original_save = Image.Image.save
    def bounded_save(image, target, *args, **kwargs):
        if {always_large!r} or max(image.size) > 1024:
            target.write(bytes(8 * 1024 * 1024 + 1))
        else:
            original_save(image, target, *args, **kwargs)
    Image.Image.save = bounded_save
"""
    monkeypatch.setattr(
        documents,
        "_CHILD",
        documents._CHILD.replace(
            "    data = sys.stdin.buffer.read(20000001)",
            injected + "    data = sys.stdin.buffer.read(20000001)",
        ),
    )
    if always_large:
        with pytest.raises(MediaInvalid, match="^document_unreadable$"):
            render_page(synthetic_pdf(1), 1)
    else:
        page = render_page(synthetic_pdf(1), 1)
        assert max(image_info(page.data).width, image_info(page.data).height) == 1024
        assert len(page.data) <= 8 * 1024 * 1024
