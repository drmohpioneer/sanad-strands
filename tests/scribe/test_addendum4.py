"""Verify deployed and live provider wiring, including nested timeout boundaries."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from live import check11b
from providers.fixtures import (
    SOURCE,
    ScriptedConverse,
    ScriptedConverter,
    ScriptedModel,
    ScriptedSpeech,
    candidate,
    response,
)

from sanad.media.audio import ConvertedAudio
from sanad.media.speech import SpeechAdapter, Transcript
from sanad.models.io import CALL_TIMEOUT, BedrockCaller, ModelReply, ModelUnavailable
from sanad.models.registry import ModelRegistry
from sanad.models.timeouts import (
    EXTRACTION_READ_TIMEOUT,
    EXTRACTION_TIMEOUT,
    PROVIDER_CONNECT_TIMEOUT,
    SPEECH_READ_TIMEOUT,
    TRANSCRIPTION_TIMEOUT,
)
from sanad.scribe.turn import dictation_model
from scribe.dictations import OWNER_SYNTHETIC


def test_production_composition_uses_speech_budget_and_preserves_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sanad.api import lambda_entry

    values = {
        "bot-token": "123456:synthetic-token",
        "webhook-secret": "synthetic-webhook",
        "tick-secret": "synthetic-tick",
        "admin-telegram-id": "100",
        "public-base-url": "https://synthetic.invalid",
        "bot-username": "synthetic_bot",
    }
    ssm = SimpleNamespace(
        get_parameters=lambda **kw: {
            "Parameters": [{"Name": "/test/" + k, "Value": v} for k, v in values.items()]
        }
    )
    configs: list[Any] = []

    def client(service: str, **kwargs: Any) -> Any:
        if service == "ssm":
            return ssm
        if service == "bedrock-runtime":
            configs.append(kwargs["config"])
        return ScriptedConverse()

    for name, value in {
        "SANAD_SSM_PREFIX": "/test/",
        "SANAD_TABLE": "synthetic",
        "SANAD_ENV": "test",
        "SANAD_BUCKET": "synthetic",
        "AWS_LAMBDA_FUNCTION_NAME": "synthetic",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("boto3.client", client)
    app = lambda_entry.configure("synthetic")
    doctor = app.state.scribe.speech_factory(SOURCE)
    patient = app.state.concierge.speech_factory(SOURCE)
    vision = app.state.scribe.vision_factory(SOURCE)
    assert doctor.caller.timeout == patient.caller.timeout == TRANSCRIPTION_TIMEOUT == 30
    assert vision.caller.timeout == CALL_TIMEOUT == 25
    assert [c.read_timeout for c in configs] == [28, 28, 22]
    assert all(c.connect_timeout == PROVIDER_CONNECT_TIMEOUT == 2 for c in configs)
    assert all(c.retries["total_max_attempts"] == 1 for c in configs)


def test_production_extraction_client_uses_13_second_read(monkeypatch: pytest.MonkeyPatch) -> None:
    configs: list[Any] = []

    def client(*args: Any, **kwargs: Any) -> ScriptedConverse:
        configs.append(kwargs["config"])
        return ScriptedConverse()

    monkeypatch.setattr("boto3.session.Session.client", client)
    dictation_model(ModelRegistry(), "worker")
    assert len(configs) == 1
    assert configs[0].connect_timeout == PROVIDER_CONNECT_TIMEOUT == 2
    assert configs[0].read_timeout == EXTRACTION_READ_TIMEOUT == 13
    assert configs[0].retries["total_max_attempts"] == 1
    assert EXTRACTION_READ_TIMEOUT + PROVIDER_CONNECT_TIMEOUT == EXTRACTION_TIMEOUT == 15
    assert SPEECH_READ_TIMEOUT + PROVIDER_CONNECT_TIMEOUT == TRANSCRIPTION_TIMEOUT == 30


def test_nested_speech_timeouts_both_allow_30_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    caps: list[float | None] = []
    timeout = asyncio.timeout

    def capture(seconds: float | None) -> asyncio.Timeout:
        caps.append(seconds)
        return timeout(seconds)

    monkeypatch.setattr(asyncio, "timeout", capture)
    caller = BedrockCaller(
        ScriptedConverse(response("synthetic 5 NUMBERS: 5")),
        "synthetic",
        timeout=TRANSCRIPTION_TIMEOUT,
    )
    result = asyncio.run(
        SpeechAdapter(caller, ScriptedConverter(), SOURCE).transcribe_converted(
            ConvertedAudio(data=b"ID3synthetic", duration=44)
        )
    )
    assert isinstance(result, Transcript)
    assert caps == [30, 30]
    with pytest.raises(ValueError, match="30 seconds"):
        BedrockCaller(ScriptedConverse(), "synthetic", timeout=30.1)


def test_live_clients_use_separate_read_caps_with_one_shared_allowance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    monkeypatch.setattr(check11b, "ROOT", tmp_path)
    path = tmp_path / "lane/spikes/real_dictation_44s.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"ID3synthetic")
    monkeypatch.setattr(check11b, "FFmpegConverter", ScriptedConverter)
    speech = ScriptedConverse(response(OWNER_SYNTHETIC.input + " NUMBERS: 53 560 12.5 5"))
    extraction = ScriptedConverse(candidate(OWNER_SYNTHETIC.candidate.model_dump()))
    configs: list[Any] = []

    def client(*args: Any, **kwargs: Any) -> ScriptedConverse:
        config = kwargs["config"]
        configs.append(config)
        return speech if config.read_timeout == SPEECH_READ_TIMEOUT else extraction

    monkeypatch.setattr("boto3.client", client)
    monkeypatch.setattr("boto3.session.Session.client", client)
    caps: list[float | None] = []
    timeout = asyncio.timeout

    def capture(seconds: float | None) -> asyncio.Timeout:
        caps.append(seconds)
        return timeout(seconds)

    monkeypatch.setattr(asyncio, "timeout", capture)
    result = asyncio.run(check11b.run_check(tmp_path / "live-11b-synthetic.json"))
    assert result["state"] == "passed"
    assert len(speech.calls) == len(extraction.calls) == 1
    assert [c.read_timeout for c in configs] == [28, 13, 13]
    assert all(c.connect_timeout == 2 and c.retries["total_max_attempts"] == 1 for c in configs)
    assert caps == [30, 30, 15]
    assert check11b.EVIDENCE.name == "live-11b-2026-09-07d.json"


def test_live_retries_adapter_timeout_once_with_identical_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class SlowFirstSpeech(ScriptedSpeech):
        attempts = 0
        contents: list[list[dict[str, Any]]]

        def __init__(self) -> None:
            super().__init__(OWNER_SYNTHETIC.input + " NUMBERS: 53 560 12.5 5")
            self.contents = []

        async def call(
            self, model_id: str, content: list[dict[str, Any]], *, max_tokens: int = 2048
        ) -> ModelReply | ModelUnavailable:
            self.attempts += 1
            self.contents.append(content)
            if self.attempts == 1:
                await asyncio.sleep(1)
            return await super().call(model_id, content, max_tokens=max_tokens)

    monkeypatch.setenv("SANAD_LIVE", "1")
    monkeypatch.setattr("sanad.media.speech.TRANSCRIPTION_TIMEOUT", 0.01)
    caller, converter = SlowFirstSpeech(), ScriptedConverter()
    model = ScriptedModel(candidate(OWNER_SYNTHETIC.candidate.model_dump()))
    report = asyncio.run(
        check11b.run_check(
            tmp_path / "live-11b-synthetic.json",
            speech=SpeechAdapter(caller, converter, SOURCE),
            model_factory=lambda registry, role: model,
        )
    )
    assert report["state"] == "passed"
    assert report["speech_attempts"] == ["timeout", "ok"]
    assert report["speech_retry_count"] == 1
    assert caller.attempts == 2 and caller.contents[0] == caller.contents[1]
    assert converter.calls == len(model.script.calls) == 1
