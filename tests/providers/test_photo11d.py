import asyncio
import hashlib
import json
import re
from collections import defaultdict, deque
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image
from store.account_fixtures import settings

from providers.fixtures import SOURCE, ScriptedVision, document, png
from providers.photo11d_fixtures import CONTROL_PHRASES, control_image, paper
from sanad.agents import hygiene
from sanad.channels.telegram.transport import TelegramTransport
from sanad.media.images import instruction_column, normalize_document, source_image_mime
from sanad.media.limits import MAX_IMAGE_BYTES, MediaInvalid, image_info, sniff
from sanad.media.vision import (
    VISION_PROMPT,
    VISION_PROMPT_VERSION,
    DocumentFailure,
    DocumentRead,
    VisionAdapter,
    vision_prompt,
)
from sanad.models.io import CallMetadata, ModelReply, ModelUnavailable
from sanad.models.registry import ModelRegistry
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY
from sanad.scribe.crosscheck import candidate_from, unreadable_read


class Readers:
    def __init__(self, first: list[str | ModelUnavailable], second: list[str | ModelUnavailable]):
        registry = ModelRegistry()
        self.scripts = {registry.vision: deque(first), registry.cross_check: deque(second)}
        self.calls: dict[str, list[str]] = defaultdict(list)
        self.started: set[str] = set()
        self.concurrent = False

    async def call(
        self, model_id: str, content: list[dict[str, Any]], *, max_tokens: int = 2048
    ) -> ModelReply | ModelUnavailable:
        self.calls[model_id].append(content[1]["text"])
        self.started.add(model_id)
        await asyncio.sleep(0)
        self.concurrent |= len(self.started) == 2
        value = self.scripts[model_id].popleft()
        if isinstance(value, ModelUnavailable):
            return value
        return ModelReply(
            text=value,
            metadata=CallMetadata(model_id=model_id, policy_version="test", latency_ms=1),
        )


@pytest.mark.parametrize(
    "failure", ["invalid JSON", VISION_PROMPT, ModelUnavailable(reason="timeout")]
)
@pytest.mark.parametrize("failed_index", [0, 1])
def test_reader_retry_and_survivor_are_concurrent(
    failure: str | ModelUnavailable, failed_index: int
) -> None:
    scripts = [[document()], [document()]]
    replies: list[list[str | ModelUnavailable]] = [list(s) for s in scripts]
    replies[failed_index] = [failure, failure]
    caller = Readers(*replies)
    read = asyncio.run(
        VisionAdapter(caller, SOURCE, POLICY).read_document(png(), "png", kind_hint="lab")
    )
    assert isinstance(read, DocumentRead) and read.single_reader and caller.concurrent
    assert read.first.status == "ok" and read.second.status == "failed"
    assert len(read.second.items) == 0 and read.second.failure_reason
    assert len(read.metadata) == 3 and not read.disagreements and unreadable_read(read)
    for kind in ("prescription", "lab"):
        candidate = candidate_from(read, kind, POLICY)
        assert not candidate.orders and not candidate.facts
    attempts = caller.calls[read.second.provenance.model_id or ""]
    assert len(attempts) == 2 and attempts[1].endswith(
        "Return only one JSON object with no surrounding text."
    )


def test_retry_can_recover_without_single_reader() -> None:
    caller = Readers(["bad", document()], [document()])
    read = asyncio.run(
        VisionAdapter(caller, SOURCE, POLICY).read_document(png(), "png", kind_hint="lab")
    )
    assert isinstance(read, DocumentRead) and not read.single_reader and len(read.metadata) == 3


def test_two_dead_readers_have_only_failure_metadata() -> None:
    caller = Readers(["bad", "bad"], [ModelUnavailable(reason="unavailable")] * 2)
    read = asyncio.run(
        VisionAdapter(caller, SOURCE, POLICY).read_document(png(), "png", kind_hint="lab")
    )
    assert isinstance(read, DocumentFailure) and read.reason == "readers_failed"
    assert len(read.metadata) == 4 and not hasattr(read, "first")


@pytest.mark.parametrize(
    "field",
    ["name", "value", "unit", "dose", "frequency", "route", "timing", "printed_name", "notes"],
)
@pytest.mark.parametrize(
    "arabic", ["اليوم", "قبل الغذاء", "5 mg قبل الغذاء", "[غير مقروء]", "ﺍﻟﻴﻮﻡ"]
)
def test_arabic_is_nulled_before_any_candidate_or_grading(field: str, arabic: str) -> None:
    raw = json.loads(document())
    if field == "notes":
        raw[field] = [arabic]
    elif field == "printed_name":
        raw[field] = arabic
    else:
        raw["items"][0][field] = arabic
    reply = json.dumps(raw)
    result = asyncio.run(
        VisionAdapter(ScriptedVision(reply, reply), SOURCE, POLICY).read_document(
            png(), "png", kind_hint="lab"
        )
    )
    assert isinstance(result, DocumentRead)
    for reader in result.readers:
        assert reader.note == "arabic_dropped"
        if field == "notes":
            assert not reader.notes
        elif field == "printed_name":
            assert reader.printed_identity_hint.text is None
        else:
            assert getattr(reader.items[0].item, field) is None
            assert reader.items[0].item.note == "arabic_dropped"
        assert arabic not in reader.model_dump_json()
    assert arabic not in candidate_from(result, "lab", POLICY).model_dump_json()


def test_rail_is_reader_specific_and_control_is_shaped(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = document(items=[{"name": "Synthetic", "frequency": CONTROL_PHRASES[0]}])
    monkeypatch.setattr(hygiene, "ARABIC_CAPABLE_READERS", frozenset({ModelRegistry().cross_check}))
    result = asyncio.run(
        VisionAdapter(ScriptedVision(raw, raw), SOURCE, POLICY).read_document(
            control_image(), "png", kind_hint="prescription"
        )
    )
    assert isinstance(result, DocumentRead)
    assert result.first.items[0].item.frequency is None
    assert result.second.items[0].item.frequency == CONTROL_PHRASES[0]
    assert result.first.dropped_fields and not result.second.dropped_fields
    assert control_image() == control_image()
    assert image_info(control_image()).width == 1000


def test_all_six_scripted_arabic_control_phrases_are_dropped() -> None:
    raw = document(
        items=[
            {"name": f"Control {i}", "frequency": phrase}
            for i, phrase in enumerate(CONTROL_PHRASES)
        ]
    )
    result = asyncio.run(
        VisionAdapter(ScriptedVision(raw, raw), SOURCE, POLICY).read_document(
            control_image(), "png", kind_hint="other"
        )
    )
    assert isinstance(result, DocumentRead)
    assert all(row.item.frequency is None for reader in result.readers for row in reader.items)
    assert all(len(reader.dropped_fields) == 6 for reader in result.readers)


@pytest.mark.parametrize("fmt", ["HEIF", "BMP", "TIFF", "WEBP", "PNG", "JPEG"])
def test_supported_images_share_normalization_and_header_caps(fmt: str) -> None:
    data = paper(fmt)
    assert source_image_mime(data).startswith("image/")
    normalized = normalize_document(data)
    info = image_info(normalized)
    assert (info.format, info.width, info.height) == ("jpeg", 480, 640)
    assert normalized == normalize_document(data)
    if fmt == "HEIF":
        assert sniff(data) == "heif"
        with pytest.raises(MediaInvalid, match="unsupported_type"):
            image_info(data)


@pytest.mark.parametrize(
    "size,expected",
    [((960, 1280), (1920, 2560)), ((1500, 2000), (1950, 2600)), ((3024, 4032), (3024, 4032))],
)
def test_upscale_ceiling_never_shrinks_delivered_size(
    size: tuple[int, int], expected: tuple[int, int]
) -> None:
    result = image_info(normalize_document(paper(size=size)))
    assert (result.width, result.height) == expected


def test_exif_orientation_and_metadata_removed() -> None:
    source = Image.new("RGB", (40, 60), "white")
    exif = Image.Exif()
    exif[274] = 6
    data = BytesIO()
    source.save(data, format="JPEG", exif=exif)
    normalized = normalize_document(data.getvalue())
    with Image.open(BytesIO(normalized)) as image:
        assert image.size == (120, 80) and image.mode == "L" and not image.getexif()


def test_heif_size_rejected_before_decoder() -> None:
    data = paper("HEIF")
    with pytest.raises(MediaInvalid, match="too_large"):
        normalize_document(data + b"\0" * (9 * 1024 * 1024 - len(data)))


def test_crop_detects_bands_and_stays_within_page() -> None:
    normalized = normalize_document(paper())
    crop = instruction_column(normalized)
    info = image_info(normalized)
    left, top, right, bottom = crop.box
    assert left == int(info.width * 0.65) and right == info.width
    assert 0 <= top < bottom <= info.height and crop.row_bands
    assert len(crop.data) < MAX_IMAGE_BYTES and image_info(crop.data).height == bottom - top
    assert crop == instruction_column(normalized)


def test_opd_layout_geometry_without_private_content_in_tests() -> None:
    data = paper(size=(1179, 2556))
    crop = instruction_column(data)
    assert crop.layout == "opd_screenshot"
    x0, y0, x1, y1 = crop.box
    assert 0 <= x0 < x1 <= 1179 and 0 <= y0 < y1 <= 2556
    assert image_info(crop.data).width > 0


def test_prompt_has_no_vocabulary_and_unchanged_schema() -> None:
    entries = json.loads(Path("src/sanad/scribe/names.yaml").read_text())["entries"]
    names = [
        name
        for e in entries
        for name in [
            e["latin"],
            e.get("generic", ""),
            *e.get("arabic_spellings", []),
            *e.get("latin_spellings", []),
        ]
        if name
    ]
    assert all(
        not re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", VISION_PROMPT, re.I)
        for name in names
    )
    assert VISION_PROMPT_VERSION == "document-fields-v5"
    assert all(
        f"- {field}:" in VISION_PROMPT
        for field in (
            "document_type",
            "printed_name",
            "printed_date",
            "items",
            "name",
            "value",
            "unit",
            "flag",
            "ref",
            "dose",
            "frequency",
            "route",
            "timing",
            "unreadable",
            "notes",
        )
    )
    assert "[unreadable]" in VISION_PROMPT and "{" not in VISION_PROMPT
    assert all(phrase not in VISION_PROMPT for phrase in CONTROL_PHRASES)
    # Freeze the entire instruction: adding any vocabulary or any of the 25
    # prohibited phrase hints is a failing prompt change, even when no drug matches.
    assert (
        hashlib.sha256(VISION_PROMPT.encode()).hexdigest()
        == "9a99eb2f3e7d99cb1a2db9dc47fa76f97c00d3f636ceae343405a23d5a691d5a"
    )
    assert (
        vision_prompt("lab")
        == VISION_PROMPT
        + "The accompanying caption suggests lab; this is only a hint; "
        "report if the document differs."
    )


def test_column_transport_uses_multipart_photo_and_caption() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 17}})

    data = normalize_document(paper())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = TelegramTransport(settings(), client).send_photo("123", data, "synthetic caption")
    assert result.status == "accepted" and captured[0].url.path.endswith("/sendPhoto")
    assert b"image/jpeg" in captured[0].content and data in captured[0].content
    assert b"synthetic caption" in captured[0].content
