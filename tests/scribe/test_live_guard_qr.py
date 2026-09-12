import asyncio
from pathlib import Path

import httpx
import pytest
from live.check08 import SpendGuard
from live.check09a import ThreeRequests, run_check
from providers.fixtures import ScriptedConverse, ScriptedModel, candidate, response
from pydantic import JsonValue
from store.account_fixtures import settings

from sanad.channels.telegram.transport import TelegramTransport
from sanad.channels.transport import ProvablyUnsent
from sanad.models.registry import ModelRegistry
from sanad.scribe import qr
from scribe.dictations import MEASURED


def test_live_runner_is_hermetic_with_injected_models_and_refuses_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "live-09a-fixtureb.json"
    previous = tmp_path / "live-09a-2026-09-07.json"
    previous.write_text('{"attempt": 1, "state": "failed"}\n')
    monkeypatch.delenv("SANAD_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="SANAD_LIVE"):
        asyncio.run(run_check(destination))
    monkeypatch.setenv("SANAD_LIVE", "1")
    models = [ScriptedModel(candidate(example.candidate.model_dump())) for example in MEASURED]
    remaining = iter(models)
    report = asyncio.run(
        run_check(destination, model_factory=lambda registry, role: next(remaining))
    )
    assert report["state"] == "passed" and report["run_count"] == 1
    assert report["attempt"] == 2 and report["prompt_version"] == "scribe-v10"
    assert all(c["candidate_validated"] and c["orders_count_matches"] for c in report["checks"])
    assert all(len(model.script.calls) == 1 for model in models)
    assert all(example.input not in destination.read_text() for example in MEASURED)
    assert previous.read_text() == '{"attempt": 1, "state": "failed"}\n'
    with pytest.raises(RuntimeError, match="already recorded"):
        asyncio.run(run_check(destination, model_factory=lambda registry, role: next(remaining)))
    with pytest.raises(RuntimeError, match="already recorded"):
        asyncio.run(
            run_check(
                tmp_path / "live-09a-anotherb.json",
                model_factory=lambda registry, role: next(remaining),
            )
        )


def test_live_check_rejects_merged_orders_even_when_all_numbers_are_covered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    merged = MEASURED[0].candidate.model_dump()
    merged["orders"] = [{"action": "start", "drug": "بيزوبرولول وأتورفاستاتين", "dose": "5 و40 مج"}]
    models = iter(
        [
            ScriptedModel(candidate(merged)),
            *(ScriptedModel(candidate(example.candidate.model_dump())) for example in MEASURED[1:]),
        ]
    )
    report = asyncio.run(
        run_check(
            tmp_path / "live-09a-fixtureb.json", model_factory=lambda registry, role: next(models)
        )
    )
    assert report["state"] == "failed"
    first = report["checks"][0]
    assert (
        first["candidate_validated"]
        and first["numbers_preserved"]
        and first["no_unsupported_numbers"]
    )
    assert first["expected_orders_count"] == 2 and first["actual_orders_count"] == 1
    assert not first["orders_count_matches"]


def test_budget_reserves_before_network_and_limits_three_requests() -> None:
    raw = ScriptedConverse(response("synthetic"), response("synthetic"), response("synthetic"))
    spend = SpendGuard(cap=0.20)
    client = ThreeRequests(raw, spend)
    for _ in range(3):
        client.converse(modelId=ModelRegistry().worker, inferenceConfig={"maxTokens": 2048})
    with pytest.raises(RuntimeError, match="three_request_limit"):
        client.converse(modelId=ModelRegistry().worker)
    assert len(raw.calls) == 3 and len(spend.calls) == 3 and spend.estimated < 0.20
    refused = ThreeRequests(raw, SpendGuard(cap=0.000001))
    with pytest.raises(RuntimeError):
        refused.converse(modelId=ModelRegistry().worker)
    assert len(raw.calls) == 3


def test_qr_renderer_payload_becomes_a_photo_without_storing_pixels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://sanad.example/p/synthetic-one-use-token"
    calls: list[str] = []

    def render(value: str) -> bytes:
        calls.append(value)
        return b"synthetic-png-pixels"

    def wire(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendPhoto")
        assert request.headers["content-type"].startswith("multipart/form-data")
        assert b"synthetic-png-pixels" in request.content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    monkeypatch.setattr(qr, "render_png", render)
    transport = TelegramTransport(settings(), httpx.Client(transport=httpx.MockTransport(wire)))
    payload: dict[str, JsonValue] = {"qr_payload": url, "text": "صالح 24 ساعة"}
    assert transport.send("20002", payload).status == "accepted"
    assert calls == [url] and payload == {"qr_payload": url, "text": "صالح 24 ساعة"}


def test_real_segno_output_is_png_and_render_failure_is_provably_unsent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert qr.render_png("https://sanad.example/p/synthetic").startswith(b"\x89PNG\r\n\x1a\n")

    def fail(url: str) -> bytes:
        raise RuntimeError("synthetic private URL")

    monkeypatch.setattr(qr, "render_png", fail)
    transport = TelegramTransport(settings(), httpx.Client())
    with pytest.raises(ProvablyUnsent, match="qr_render_failed"):
        transport.send("20002", {"qr_payload": "synthetic", "text": "synthetic"})
