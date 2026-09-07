"""ffmpeg normalization. Subprocesses get no shell or remote-protocol access."""

import json
import logging
import math
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field

from sanad.domain.boundaries import _BoundaryValue
from sanad.media.limits import MAX_AUDIO_BYTES, MAX_AUDIO_SECONDS, MediaInvalid, sniff

PROBE_TIMEOUT = 10.0
CONVERSION_TIMEOUT = 40.0
WARM_TIMEOUT = 20.0
logger = logging.getLogger(__name__)


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
    _warmed: bool = field(default=False, init=False, repr=False)

    def warm(self) -> None:
        """Load both binaries once during container init; failure is metadata only."""
        if self._warmed:
            return
        self._warmed = True
        for binary in (self.ffprobe, self.ffmpeg):
            try:
                self.run(
                    [binary, "-version"],
                    capture_output=True,
                    check=True,
                    shell=False,
                    timeout=WARM_TIMEOUT,
                )
            except Exception:
                logger.warning("ffmpeg_warm_failed")

    def convert(self, audio: bytes, fmt: str) -> ConvertedAudio | ConversionFailure:
        if len(audio) > MAX_AUDIO_BYTES:
            return ConversionFailure(reason="too_large")
        try:
            actual = sniff(audio)
        except MediaInvalid:
            return ConversionFailure(reason="unsupported_type")
        if actual not in {"ogg", "wav", "m4a", "mp3"}:
            return ConversionFailure(reason="unsupported_type")
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
                    timeout=PROBE_TIMEOUT,
                    **options,
                )
                duration = float(json.loads(probe.stdout)["format"]["duration"])
                if not math.isfinite(duration) or duration <= 0:
                    return ConversionFailure(reason="conversion_failed")
                if duration > MAX_AUDIO_SECONDS:
                    return ConversionFailure(reason="too_long")
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
                    timeout=CONVERSION_TIMEOUT,
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
