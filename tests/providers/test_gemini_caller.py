"""Hermetic HTTP boundary checks; synthetic credentials only."""

import asyncio
import base64
import json
import logging
import time
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from sanad.models.gemini import GeminiCaller
from sanad.models.io import ModelReply, ModelUnavailable


@pytest.mark.parametrize(
    ("timeout", "statuses", "expected", "waits"),
    [
        (25.0, [429, 200], "reply", [2.0]),
        (11.0, [429], "unavailable", []),
        (25.0, [429, 429], "unavailable", [2.0]),
    ],
)
def test_bounded_rate_limit_retry(
    timeout: float,
    statuses: list[int],
    expected: str,
    waits: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    requests: list[httpx.Request] = []
    responses = []
    sleep = AsyncMock()

    def respond(request: httpx.Request) -> httpx.Response:
        status = statuses[len(requests)]
        requests.append(request)
        response = httpx.Response(
            status,
            json=body()
            if status == 200
            else {"error": {"message": "private-body-marker", "details": [{"retryDelay": "2s"}]}},
        )
        responses.append(response)
        return response

    caller = GeminiCaller(
        "synthetic",
        "synthetic-secret",
        timeout=timeout,
        transport=httpx.MockTransport(respond),
        sleep=sleep,
    )
    result = asyncio.run(caller.call("gemini-3.8-flash", [{"text": "test"}]))
    if expected == "reply":
        assert isinstance(result, ModelReply)
    else:
        assert isinstance(result, ModelUnavailable) and result.reason == expected
    assert len(requests) == len(statuses)
    assert all(request is requests[0] for request in requests)
    assert all(response.is_closed for response in responses)
    assert [call.args[0] for call in sleep.await_args_list] == waits
    assert "sleep=" not in repr(caller)
    for marker in ("synthetic-secret", "private-body-marker"):
        assert marker not in repr(caller) + repr(result) + caplog.text
    assert all(request.headers["x-goog-api-key"] == "synthetic-secret" for request in requests)
    assert all("synthetic-secret" not in str(request.url) for request in requests)


@pytest.mark.parametrize(
    ("details", "wait"),
    [
        ([{"retryDelay": "40s"}], 8.0),
        ([{"retryDelay": "0s"}], 0.0),
        ([{}, {"retryDelay": "1.5s"}, {"retryDelay": "2s"}], 1.5),
        ([{"retryDelay": "bad"}, {"retryDelay": "2s"}], 8.0),
        *[
            ([{"retryDelay": value}], 8.0)
            for value in (None, 2, "-1s", "NaNs", "infs", "2", "1e999s")
        ],
        ([], 8.0),
        (None, 8.0),
    ],
)
def test_rate_limit_delay_parsing_and_cap(details: Any, wait: float) -> None:
    sleep = AsyncMock()
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"details": details}}),
            httpx.Response(200, json=body()),
        ]
    )
    caller = GeminiCaller(
        "synthetic",
        "synthetic",
        sleep=sleep,
        transport=httpx.MockTransport(lambda r: next(responses)),
    )
    assert isinstance(asyncio.run(caller.call("gemini-3.8-flash", [{"text": "test"}])), ModelReply)
    sleep.assert_awaited_once_with(wait)


@pytest.mark.parametrize("payload", [b"not-json", b"null", b"[]", b"{}"])
def test_rate_limit_malformed_body_uses_default(payload: bytes) -> None:
    sleep = AsyncMock()
    responses = iter([httpx.Response(429, content=payload), httpx.Response(200, json=body())])
    caller = GeminiCaller(
        "synthetic",
        "synthetic",
        sleep=sleep,
        transport=httpx.MockTransport(lambda r: next(responses)),
    )
    assert isinstance(asyncio.run(caller.call("gemini-3.8-flash", [{"text": "test"}])), ModelReply)
    sleep.assert_awaited_once_with(8.0)


def test_rate_limit_retry_keeps_wall_clock_deadline(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("sanad.models.gemini.RATE_LIMIT_HEADROOM", 0.0)
    caplog.set_level(logging.DEBUG)
    requests = []
    sleep = AsyncMock()

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429,
                json={
                    "error": {"message": "private-body-marker", "details": [{"retryDelay": "0s"}]}
                },
            )
        await asyncio.sleep(1.0)
        return httpx.Response(200, json=body())

    caller = GeminiCaller(
        "synthetic",
        "synthetic-secret",
        timeout=0.2,
        transport=httpx.MockTransport(respond),
        sleep=sleep,
    )
    started = time.monotonic()
    result = asyncio.run(caller.call("gemini-3.8-flash", [{"text": "test"}]))
    elapsed = time.monotonic() - started
    assert isinstance(result, ModelUnavailable) and result.reason == "timeout"
    assert 0.18 <= elapsed < 0.8 and len(requests) == 2
    sleep.assert_awaited_once_with(0.0)
    for marker in ("synthetic-secret", "private-body-marker"):
        assert marker not in repr(caller) + repr(result) + caplog.text


def body(**extra: Any) -> dict[str, Any]:
    return {
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "synthetic"}]}}],
        **extra,
    }


@pytest.mark.parametrize(
    ("model_id", "generation_config"),
    [
        (
            "gemini-3.8-flash",
            {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}, "maxOutputTokens": 2048},
        ),
        ("gemini-3.5-flash-lite", {"temperature": 0, "maxOutputTokens": 2048}),
    ],
)
def test_mapping_header_usage_and_no_proxy(
    monkeypatch: pytest.MonkeyPatch, model_id: str, generation_config: dict[str, Any]
) -> None:
    captured: list[httpx.Request] = []
    options: list[dict[str, Any]] = []
    original = httpx.AsyncClient

    def client(**kw: Any) -> httpx.AsyncClient:
        options.append(kw)
        return original(**kw)

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json=body(
                usageMetadata={
                    "promptTokenCount": 12,
                    "candidatesTokenCount": 3,
                    "thoughtsTokenCount": 7,
                }
            ),
        )

    monkeypatch.setattr(httpx, "AsyncClient", client)
    caller = GeminiCaller(
        "synthetic-policy", "synthetic-secret", transport=httpx.MockTransport(respond)
    )
    result = asyncio.run(
        caller.call(
            model_id,
            [
                {"audio": {"format": "mp3", "source": {"bytes": b"ID3synthetic"}}},
                {"image": {"format": "png", "source": {"bytes": b"synthetic-image"}}},
                {"text": "synthetic prompt"},
            ],
        )
    )
    assert isinstance(result, ModelReply)
    assert result.metadata.input_tokens == 12 and result.metadata.output_tokens == 10
    assert result.metadata.usage_known and result.metadata.policy_version == "synthetic-policy"
    request = captured[0]
    assert request.headers["x-goog-api-key"] == "synthetic-secret"
    assert "synthetic-secret" not in str(request.url) + repr(caller)
    data = json.loads(request.content)
    assert data["contents"][0]["parts"] == [
        {
            "inline_data": {
                "mime_type": "audio/mpeg",
                "data": base64.b64encode(b"ID3synthetic").decode(),
            }
        },
        {
            "inline_data": {
                "mime_type": "image/png",
                "data": base64.b64encode(b"synthetic-image").decode(),
            }
        },
        {"text": "synthetic prompt"},
    ]
    assert data["generationConfig"] == generation_config
    assert options[0]["trust_env"] is False


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"promptTokenCount": -1, "candidatesTokenCount": 4},
        {"promptTokenCount": True, "candidatesTokenCount": 4},
        {"promptTokenCount": 3, "candidatesTokenCount": "4"},
        "private-body",
    ],
)
def test_unknown_usage(usage: Any) -> None:
    caller = GeminiCaller(
        "synthetic",
        "synthetic",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=body(usageMetadata=usage))
        ),
    )
    result = asyncio.run(caller.call("gemini-3.8-flash", [{"text": "test"}]))
    assert isinstance(result, ModelReply) and not result.metadata.usage_known
    assert result.metadata.input_tokens == result.metadata.output_tokens == 0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, text="synthetic-secret"),
        httpx.Response(200, text="not json synthetic-secret"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"candidates": [{"finishReason": "MAX_TOKENS"}]}),
        httpx.Response(200, json=body(promptFeedback={"blockReason": "SAFETY"})),
        httpx.Response(
            200,
            json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": 42}]}}]},
        ),
    ],
)
def test_errors_are_opaque(response: httpx.Response, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def respond(request: httpx.Request) -> httpx.Response:
        return response

    caller = GeminiCaller("synthetic", "synthetic-secret", transport=httpx.MockTransport(respond))
    result = asyncio.run(caller.call("gemini-3.8-flash", [{"text": "test"}]))
    assert isinstance(result, ModelUnavailable) and result.reason == "unavailable"
    assert "synthetic-secret" not in repr(result) + repr(caller) + caplog.text


@pytest.mark.parametrize("slow_sync", [False, True])
def test_wall_clock_deadline(slow_sync: bool) -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        if slow_sync:
            time.sleep(0.03)
        else:
            await asyncio.sleep(0.03)
        return httpx.Response(200, json=body())

    caller = GeminiCaller(
        "synthetic", "synthetic", timeout=0.01, transport=httpx.MockTransport(respond)
    )
    result = asyncio.run(caller.call("gemini-3.8-flash", [{"text": "test"}]))
    assert isinstance(result, ModelUnavailable) and result.reason == "timeout"


def test_request_size_checked_before_transport() -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=body())

    caller = GeminiCaller("synthetic", "synthetic", transport=httpx.MockTransport(respond))
    result = asyncio.run(caller.call("gemini-3.8-flash", [{"text": "x" * 20_000_000}]))
    assert isinstance(result, ModelUnavailable) and not calls


def test_exception_and_metadata_errors_never_escape(caplog: pytest.LogCaptureFixture) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic-secret", request=request)

    caller = GeminiCaller("synthetic", "synthetic-secret", transport=httpx.MockTransport(fail))
    result = asyncio.run(caller.call("gemini-3.8-flash", [{"text": "test"}]))
    assert isinstance(result, ModelUnavailable)
    assert "synthetic-secret" not in str(result) + caplog.text
    with pytest.raises(ValueError, match="30 seconds"):
        GeminiCaller("synthetic", "synthetic", timeout=31)


@pytest.mark.parametrize("chunked", [False, True])
def test_default_transport_never_logs_headers_and_preserves_shared_loggers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, chunked: bool
) -> None:
    from sanad.models.gemini import PrivateTransport

    key = "synthetic-secret-marker"
    payload = json.dumps(body()).encode()
    headers = b"HTTP/1.1 200 synthetic-secret-marker\r\nx-goog-api-key: synthetic-secret-marker\r\n"
    if chunked:
        wire = (
            headers
            + b"Transfer-Encoding: chunked\r\n\r\n"
            + (f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
        )
    else:
        wire = headers + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
    packets = []

    class Writer:
        closed = False

        def write(self, packet: bytes) -> None:
            packets.append(packet)

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    writer = Writer()

    async def connect(*args: Any, **kwargs: Any) -> Any:
        assert args == ("generativelanguage.googleapis.com", 443)
        assert kwargs["ssl"].check_hostname
        reader = asyncio.StreamReader()
        reader.feed_data(wire)
        reader.feed_eof()
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    loggers = [logging.getLogger(n) for n in ("httpx", "httpcore", "httpcore.new_logger")]
    before = [(logger.disabled, logger.level, tuple(logger.filters)) for logger in loggers]
    caplog.set_level(logging.DEBUG)
    captured: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = Capture()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        result = asyncio.run(
            GeminiCaller("synthetic", key, transport=PrivateTransport()).call(
                "gemini-3.5-flash-lite", [{"text": "synthetic"}]
            )
        )
    finally:
        root.removeHandler(handler)
    assert isinstance(result, ModelReply) and writer.closed
    assert b"x-goog-api-key: synthetic-secret-marker" in b"".join(packets)
    assert before == [(logger.disabled, logger.level, tuple(logger.filters)) for logger in loggers]
    assert all(
        key not in r.getMessage() and "x-goog-api-key" not in r.getMessage() for r in captured
    )
    assert key not in caplog.text
    assert "generativelanguage.googleapis.com" not in caplog.text
    logging.getLogger("httpcore.new_logger").warning("unrelated-log-still-visible")
    assert "unrelated-log-still-visible" in caplog.text


def test_default_transport_cancellation_closes_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    class Writer:
        closed = False

        def write(self, packet: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    writer = Writer()

    async def connect(*args: Any, **kwargs: Any) -> Any:
        return asyncio.StreamReader(), writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    result = asyncio.run(
        GeminiCaller("synthetic", "synthetic", timeout=0.03).call(
            "gemini-3.8-flash", [{"text": "synthetic"}]
        )
    )
    assert isinstance(result, ModelUnavailable) and result.reason == "timeout" and writer.closed


def test_metadata_construction_failure_is_opaque(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**kwargs: Any) -> Any:
        raise ValueError("synthetic-secret")

    monkeypatch.setattr("sanad.models.gemini.CallMetadata", fail)
    result = asyncio.run(
        GeminiCaller(
            "synthetic",
            "synthetic-secret",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body())),
        ).call("gemini-3.8-flash", [{"text": "synthetic"}])
    )
    assert isinstance(result, ModelUnavailable) and "synthetic-secret" not in repr(result)
