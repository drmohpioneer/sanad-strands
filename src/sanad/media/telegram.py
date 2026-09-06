"""Telegram file IO shares the accepted transport's redacting HTTP client."""

import re
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal, Protocol

import httpx
from pydantic import Field

from sanad.channels.telegram.transport import TelegramTransport
from sanad.domain.boundaries import _BoundaryValue
from sanad.media.limits import MAX_AUDIO_BYTES


class FileBytes(_BoundaryValue):
    data: bytes = Field(repr=False)


class MediaFailure(_BoundaryValue):
    reason: str
    route: Literal["media_failure"] = "media_failure"
    request_resend: bool = True
    durable: bool = False
    review_obligation_id: str | None = None
    resend_intent_id: str | None = None


class TelegramFiles(Protocol):
    def fetch(self, handle: str) -> FileBytes | MediaFailure: ...


@dataclass
class TelegramFileClient:
    transport: TelegramTransport = field(repr=False)

    def fetch(self, handle: str) -> FileBytes | MediaFailure:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", handle):
            return MediaFailure(reason="invalid_handle")
        settings, http = self.transport.settings, self.transport.http
        root = settings.api_base.rstrip("/")
        token = settings.bot_token.get_secret_value()
        started = monotonic()
        try:
            response = http.post(
                f"{root}/bot{token}/getFile",
                json={"file_id": handle},
                timeout=10,
                follow_redirects=False,
            )
            if response.status_code in {400, 404}:
                return MediaFailure(reason="expired_handle")
            if response.status_code != 200:
                return MediaFailure(reason="telegram_unavailable")
            body = response.json()
            result = body.get("result")
            if body.get("ok") is not True or not isinstance(result, dict):
                return MediaFailure(reason="invalid_get_file")
            path = result.get("file_path")
            if (
                not isinstance(path, str)
                or not re.fullmatch(r"[A-Za-z0-9_/-]+\.[A-Za-z0-9]+", path)
                or (path.startswith("/") or ".." in path)
            ):
                return MediaFailure(reason="invalid_file_path")
            size = result.get("file_size")
            if size is not None and (type(size) is not int or size > MAX_AUDIO_BYTES or size < 1):
                return MediaFailure(reason="too_large")
            chunks: list[bytes] = []
            total = 0
            with http.stream(
                "GET", f"{root}/file/bot{token}/{path}", timeout=10, follow_redirects=False
            ) as download:
                if download.status_code in {400, 404}:
                    return MediaFailure(reason="expired_handle")
                if download.status_code != 200:
                    return MediaFailure(reason="telegram_unavailable")
                for chunk in download.iter_bytes(chunk_size=65536):
                    total += len(chunk)
                    if total > MAX_AUDIO_BYTES:
                        return MediaFailure(reason="too_large")
                    if monotonic() - started >= 20:
                        return MediaFailure(reason="download_timeout")
                    chunks.append(chunk)
            if total == 0:
                return MediaFailure(reason="empty_file")
            return FileBytes(data=b"".join(chunks))
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return MediaFailure(reason="telegram_unavailable")
