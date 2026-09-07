import asyncio
import io
import json
import struct
import zlib
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from domain_fixtures import NOW
from strands.models import BedrockModel

from sanad.domain import Provenance
from sanad.media.audio import ConversionFailure, ConvertedAudio
from sanad.media.storage import S3MediaStore
from sanad.media.telegram import FileBytes, MediaFailure
from sanad.models.io import CallMetadata, ModelReply, ModelUnavailable

SOURCE = Provenance(
    source_observation_id="synthetic-receipt",
    actor_kind="doctor",
    actor_id="synthetic-doctor",
    source_kind="doctor_statement",
    received_at=NOW,
)


def png(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + (
            chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * (width * 3)) * height))
            + chunk(b"IEND", b"")
        )
    )


def response(
    text: str = "", *, calls: list[tuple[str, dict[str, Any]]] | None = None
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"text": text}] if text else []
    for i, (name, arguments) in enumerate(calls or []):
        content.append({"toolUse": {"toolUseId": f"tool-{i}", "name": name, "input": arguments}})
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use" if calls else "end_turn",
        "usage": {"inputTokens": 100, "outputTokens": 25, "totalTokens": 125},
        "metrics": {"latencyMs": 1},
    }


def candidate(value: dict[str, Any], spans: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return response(json.dumps({"value": value, "spans": spans or []}, ensure_ascii=False))


class ScriptedConverse:
    def __init__(self, *scripts: Any):
        self.scripts = deque(scripts)
        self.calls: list[dict[str, Any]] = []
        self.meta = SimpleNamespace(region_name="us-east-1")

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self.scripts:
            raise AssertionError("unexpected model call")
        script = self.scripts.popleft()
        if isinstance(script, Exception):
            raise script
        result: dict[str, Any] = script(kwargs) if callable(script) else script
        return result


class ScriptedModel(BedrockModel):
    """The actual pinned Bedrock/Strands loop, backed by queued Converse responses."""

    def __init__(self, *scripts: Any):
        self.script = ScriptedConverse(*scripts)
        session: Any = SimpleNamespace(region_name="us-east-1", client=lambda **kwargs: self.script)
        super().__init__(
            boto_session=session,
            model_id="us.amazon.nova-lite-v1:0",
            temperature=0,
            streaming=False,
            max_tokens=2048,
        )


class ScriptedSpeech:
    def __init__(self, *scripts: str | ModelUnavailable):
        self.scripts = deque(scripts)
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def call(
        self, model_id: str, content: list[dict[str, Any]], *, max_tokens: int = 2048
    ) -> ModelReply | ModelUnavailable:
        self.calls.append((model_id, content))
        script = self.scripts.popleft()
        await asyncio.sleep(0)
        if isinstance(script, ModelUnavailable):
            return script
        return ModelReply(
            text=script,
            metadata=CallMetadata(
                model_id=model_id,
                policy_version="synthetic-policy",
                latency_ms=1,
                input_tokens=100,
                output_tokens=25,
            ),
        )


class ScriptedVision(ScriptedSpeech):
    pass


@dataclass
class ScriptedConverter:
    result: ConvertedAudio | ConversionFailure = ConvertedAudio(data=b"ID3synthetic", duration=15)
    calls: int = 0

    def convert(self, audio: bytes, fmt: str) -> ConvertedAudio | ConversionFailure:
        self.calls += 1
        return self.result


class FakeTelegramFiles:
    def __init__(self, result: bytes | MediaFailure):
        self.result = result
        self.calls: list[str] = []

    def fetch(self, handle: str) -> FileBytes | MediaFailure:
        self.calls.append(handle)
        return FileBytes(data=self.result) if isinstance(self.result, bytes) else self.result


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.writes: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["ServerSideEncryption"] == "AES256"
        assert "ACL" not in kwargs
        self.objects[kwargs["Key"]] = kwargs["Body"]
        self.writes.append(kwargs)
        return {"VersionId": "synthetic-version"}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        data = self.objects[kwargs["Key"]]
        return {"Body": io.BytesIO(data), "ContentLength": len(data)}


class FakeS3(S3MediaStore):
    def __init__(self) -> None:
        self.fake = FakeS3Client()
        super().__init__("synthetic-private-bucket", self.fake)


def document(**changes: Any) -> str:
    return json.dumps(
        {
            "document_type": "lab",
            "printed_name": "Untrusted printed name",
            "printed_date": "2026-09-06",
            "items": [
                {"name": "Potassium", "value": "6.3", "unit": "mmol/L", "flag": "normal"},
                {"name": "Creatinine", "value": "2.4", "unit": "mg/dL"},
            ],
            "unreadable": False,
            "notes": [],
        }
        | changes
    )
