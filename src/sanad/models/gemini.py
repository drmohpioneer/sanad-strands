"""Bounded Gemini media transport; credentials and provider bodies stay private."""

import asyncio
import base64
import json
import math
import os
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

import h11
import httpx

from sanad.models.io import CALL_TIMEOUT, CallMetadata, ModelReply, ModelUnavailable
from sanad.models.timeouts import TRANSCRIPTION_TIMEOUT

MAX_REQUEST_BYTES = 20_000_000
RATE_LIMIT_DEFAULT_WAIT = 8.0
RATE_LIMIT_HEADROOM = 10.0
# Pinned by contract 11M binding addendum 4 after the architect's measurement.
CONFIGS: dict[str, dict[str, Any]] = {
    "gemini-3.8-flash": {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}},
    "gemini-3.5-flash-lite": {"temperature": 0},
}


def retry_delay(response: httpx.Response) -> float:
    """Read only the first body delay; malformed or missing delays use the default."""
    try:
        details = response.json()["error"]["details"]
        if not isinstance(details, list):
            return RATE_LIMIT_DEFAULT_WAIT
        for detail in details:
            if not isinstance(detail, dict) or "retryDelay" not in detail:
                continue
            value = detail["retryDelay"]
            if isinstance(value, str) and value.endswith("s"):
                delay = float(value[:-1])
                if math.isfinite(delay) and delay >= 0:
                    return delay
            return RATE_LIMIT_DEFAULT_WAIT
    except (ValueError, KeyError, TypeError):
        pass
    return RATE_LIMIT_DEFAULT_WAIT


class PrivateTransport(httpx.AsyncBaseTransport):
    """One TLS HTTP/1.1 exchange without httpcore's request/response tracing.

    h11 (already locked through httpx) parses framing, including chunked bodies.
    asyncio owns the socket, so cancellation closes it within the caller deadline.
    No shared logger, proxy, redirect, retry or provider header reaches a log.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https" or request.url.host != "generativelanguage.googleapis.com":
            raise ValueError("invalid provider origin")
        reader, writer = await asyncio.open_connection(
            request.url.host,
            443,
            ssl=ssl.create_default_context(),
            server_hostname=request.url.host,
        )
        try:
            connection = h11.Connection(h11.CLIENT)
            for event in (
                h11.Request(
                    method=request.method, target=request.url.raw_path, headers=request.headers.raw
                ),
                h11.Data(data=await request.aread()),
                h11.EndOfMessage(),
            ):
                packet = connection.send(event)
                if packet:
                    writer.write(packet)
            await writer.drain()
            status = 0
            data = bytearray()
            while True:
                received = connection.next_event()
                if received is h11.NEED_DATA:
                    connection.receive_data(await reader.read(65536))
                elif isinstance(received, h11.Response):
                    status = received.status_code
                elif isinstance(received, h11.Data):
                    data.extend(received.data)
                    if len(data) > MAX_REQUEST_BYTES:
                        raise ValueError("response too large")
                elif isinstance(received, h11.EndOfMessage):
                    # Provider reason phrases and headers are deliberately discarded.
                    return httpx.Response(status, content=bytes(data))
                elif not isinstance(received, h11.InformationalResponse):
                    raise ValueError("incomplete response")
        finally:
            writer.close()


@dataclass
class GeminiCaller:
    policy_version: str
    api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""), repr=False)
    timeout: float = CALL_TIMEOUT
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)

    def __post_init__(self) -> None:
        if not 0 < self.timeout <= TRANSCRIPTION_TIMEOUT:
            raise ValueError("provider timeout must be within 30 seconds")

    async def call(
        self, model_id: str, content: list[dict[str, Any]], *, max_tokens: int = 2048
    ) -> ModelReply | ModelUnavailable:
        started = monotonic()
        try:
            async with asyncio.timeout(self.timeout):
                if model_id not in CONFIGS or not self.api_key or type(max_tokens) is not int:
                    raise ValueError
                if max_tokens <= 0:
                    raise ValueError
                parts: list[dict[str, Any]] = []
                for block in content:
                    if set(block) == {"text"} and isinstance(block["text"], str):
                        parts.append({"text": block["text"]})
                    elif len(block) == 1 and ("audio" in block or "image" in block):
                        kind = "audio" if "audio" in block else "image"
                        media = block[kind]
                        fmt = media["format"]
                        mime = {
                            "audio": {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg"},
                            "image": {
                                "png": "image/png",
                                "jpeg": "image/jpeg",
                                "webp": "image/webp",
                            },
                        }[kind][fmt]
                        parts.append(
                            {
                                "inline_data": {
                                    "mime_type": mime,
                                    "data": base64.b64encode(media["source"]["bytes"]).decode(
                                        "ascii"
                                    ),
                                }
                            }
                        )
                    else:
                        raise ValueError
                payload = json.dumps(
                    {
                        "contents": [{"role": "user", "parts": parts}],
                        "generationConfig": {**CONFIGS[model_id], "maxOutputTokens": max_tokens},
                    },
                    ensure_ascii=False,
                ).encode()
                if len(payload) > MAX_REQUEST_BYTES:
                    raise ValueError
                if monotonic() - started >= self.timeout:
                    raise TimeoutError
                transport = self.transport or PrivateTransport()
                async with httpx.AsyncClient(
                    trust_env=False, timeout=self.timeout, transport=transport
                ) as client:
                    # Build through httpx, execute through the private transport.
                    # AsyncClient.send emits an HTTP summary through a shared logger.
                    request = client.build_request(
                        "POST",
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent",
                        headers={
                            "x-goog-api-key": self.api_key,
                            "content-type": "application/json",
                            "accept-encoding": "identity",
                        },
                        content=payload,
                    )
                    for attempt in range(2):
                        response = await transport.handle_async_request(request)
                        response.request = request
                        try:
                            await response.aread()
                            if response.status_code == 429 and attempt == 0:
                                wait = min(retry_delay(response), RATE_LIMIT_DEFAULT_WAIT)
                                remaining = self.timeout - (monotonic() - started)
                                if wait + RATE_LIMIT_HEADROOM > remaining:
                                    return ModelUnavailable(reason="unavailable")
                            else:
                                response.raise_for_status()
                                body = response.json()
                                break
                        finally:
                            await response.aclose()
                        await self.sleep(wait)
                if body.get("promptFeedback", {}).get("blockReason"):
                    raise ValueError
                candidates = body["candidates"]
                if len(candidates) != 1 or candidates[0]["finishReason"] != "STOP":
                    raise ValueError
                candidate = candidates[0]
                if any(r.get("blocked") for r in candidate.get("safetyRatings", [])):
                    raise ValueError
                text = "\n".join(
                    p["text"]
                    for p in candidate["content"]["parts"]
                    if "text" in p and not p.get("thought", False)
                )
                if not text.strip():
                    raise ValueError
                usage = body.get("usageMetadata", {})
                counts = (
                    [
                        usage.get("promptTokenCount"),
                        usage.get("candidatesTokenCount"),
                        usage.get("thoughtsTokenCount", 0),
                    ]
                    if isinstance(usage, dict)
                    else []
                )
                known = len(counts) == 3 and all(type(n) is int and n >= 0 for n in counts)
                meta = CallMetadata(
                    model_id=model_id,
                    policy_version=self.policy_version,
                    latency_ms=(monotonic() - started) * 1000,
                    input_tokens=counts[0] if known else 0,
                    output_tokens=counts[1] + counts[2] if known else 0,
                    usage_known=known,
                )
                result = ModelReply(text=text, metadata=meta)
                # Synchronous serialization/parsing also consumes the same deadline.
                if monotonic() - started >= self.timeout:
                    raise TimeoutError
                return result
        except (TimeoutError, httpx.TimeoutException):
            return ModelUnavailable(reason="timeout")
        except Exception:
            return ModelUnavailable(reason="unavailable")
