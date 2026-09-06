"""ffmpeg normalization. Subprocesses get no shell or remote-protocol access."""

import json
import math
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Literal, Protocol

from pydantic import Field

from sanad.domain.boundaries import _BoundaryValue
from sanad.media.limits import MAX_AUDIO_BYTES, MAX_AUDIO_SECONDS, MediaInvalid, sniff


class ConvertedAudio(_BoundaryValue):
    data: bytes = Field(repr=False)
    format: Literal["mp3"] = "mp3"
    duration: float = Field(gt=0, le=MAX_AUDIO_SECONDS)


class ConversionFailure(_BoundaryValue):
    reason: Literal["too_long", "too_large", "unsupported_type", "conversion_failed"]


class AudioConverter(Protocol):
    def convert(self, audio: bytes, fmt: str) -> ConvertedAudio | ConversionFailure: ...


@dataclass
class FFmpegConverter:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run

    def convert(self, audio: bytes, fmt: str) -> ConvertedAudio | ConversionFailure:
        if len(audio) > MAX_AUDIO_BYTES:
            return ConversionFailure(reason="too_large")
        try:
            actual = sniff(audio)
        except MediaInvalid:
            return ConversionFailure(reason="unsupported_type")
        if actual not in {"ogg", "wav", "m4a", "mp3"}:
            return ConversionFailure(reason="unsupported_type")
        started = monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="sanad-audio-") as directory:
                source = Path(directory) / ("source." + actual)
                source.write_bytes(audio)
                options: dict[str, Any] = {"capture_output": True, "check": True, "shell": False}
                probe = self.run(
                    [
                        self.ffprobe,
                        "-v",
                        "error",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "json",
                        str(source),
                    ],
                    timeout=20,
                    **options,
                )
                duration = float(json.loads(probe.stdout)["format"]["duration"])
                if not math.isfinite(duration) or duration <= 0:
                    return ConversionFailure(reason="conversion_failed")
                if duration > MAX_AUDIO_SECONDS:
                    return ConversionFailure(reason="too_long")
                remaining = 20 - (monotonic() - started)
                if remaining <= 0:
                    return ConversionFailure(reason="conversion_failed")
                result = self.run(
                    [
                        self.ffmpeg,
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-threads",
                        "1",
                        "-i",
                        str(source),
                        "-t",
                        "300",
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-codec:a",
                        "libmp3lame",
                        "-b:a",
                        "48k",
                        "-f",
                        "mp3",
                        "pipe:1",
                    ],
                    timeout=remaining,
                    **options,
                )
                if (
                    not result.stdout
                    or len(result.stdout) > 2 * 1024 * 1024
                    or sniff(result.stdout) != "mp3"
                ):
                    return ConversionFailure(reason="conversion_failed")
                return ConvertedAudio(data=result.stdout, duration=duration)
        except Exception:
            # stderr, local paths and provider bytes are deliberately withheld.
            return ConversionFailure(reason="conversion_failed")
