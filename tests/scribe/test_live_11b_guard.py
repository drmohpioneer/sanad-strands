import asyncio
import json
import re
from pathlib import Path

import pytest
from live import check11b
from live.check11b import DictationRequests, DictationSpend, run_check
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
from sanad.media.speech import SpeechAdapter
from sanad.models.io import ModelUnavailable
from sanad.models.registry import ModelRegistry
from scribe.dictations import OWNER_SYNTHETIC


def test_11b_live_runner_hermetic_oracle_redaction_and_single_allowance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "live-11b-synthetic.json"
    previous = tmp_path / "live-11b-2026-09-07.json"
    previous.write_text('{"attempt": 2, "state": "failed"}\n')
    previous3 = tmp_path / "live-11b-2026-09-07b.json"
    previous3.write_text('{"attempt": 3, "state": "failed"}\n')
    previous4 = tmp_path / "live-11b-2026-09-07c.json"
    previous4.write_text('{"attempt": 4, "state": "failed"}\n')
    speech = SpeechAdapter(
        ScriptedSpeech(OWNER_SYNTHETIC.input + " NUMBERS: 53 560 12.5 5"),
        ScriptedConverter(),
        SOURCE,
    )
    model = ScriptedModel(candidate(OWNER_SYNTHETIC.candidate.model_dump()))
    monkeypatch.delenv("SANAD_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="SANAD_LIVE"):
        asyncio.run(
            run_check(destination, speech=speech, model_factory=lambda registry, role: model)
        )
    monkeypatch.setenv("SANAD_LIVE", "1")
    result = asyncio.run(
        run_check(destination, speech=speech, model_factory=lambda registry, role: model)
    )
    assert result["state"] == "passed"
    assert result["attempt"] == 5 and result["speech_retry_count"] == 0
    assert result["speech_attempts"] == ["ok"]
    assert previous.read_text() == '{"attempt": 2, "state": "failed"}\n'
    assert previous3.read_text() == '{"attempt": 3, "state": "failed"}\n'
    assert previous4.read_text() == '{"attempt": 4, "state": "failed"}\n'
    assert result["order_count"] == 3
    assert result["resolved_lab_names"] == ["BUN", "creatinine", "Na", "K"]
    assert OWNER_SYNTHETIC.input not in destination.read_text()
    assert OWNER_SYNTHETIC.candidate.patient.name_as_spoken
    assert OWNER_SYNTHETIC.candidate.patient.name_as_spoken not in destination.read_text()
    # Whole number only: a timestamp such as "...:51.375605" contains "560" by chance.
    assert not re.search(r"(?<!\d)560(?!\d)", destination.read_text())
    with pytest.raises(RuntimeError, match="already recorded"):
        asyncio.run(
            run_check(
                tmp_path / "live-11b-other-date.json",
                speech=speech,
                model_factory=lambda registry, role: model,
            )
        )
    assert len(model.script.calls) == 1


def test_11b_request_order_count_output_and_spend_caps() -> None:
    raw = ScriptedConverse(response("synthetic"), response("synthetic"))
    guard = DictationSpend(cap=0.15)
    client = DictationRequests(raw, guard)
    registry = ModelRegistry()
    with pytest.raises(RuntimeError, match="allowance"):
        client.converse(modelId=registry.worker, inferenceConfig={"maxTokens": 2048})
    for model in (registry.speech, registry.worker):
        client.converse(modelId=model, inferenceConfig={"maxTokens": 2048})
    with pytest.raises(RuntimeError, match="allowance"):
        client.converse(modelId=registry.worker, inferenceConfig={"maxTokens": 2048})
    assert len(raw.calls) == 2 and guard.estimated < 0.15
    another = DictationRequests(raw, DictationSpend(cap=0.15))
    with pytest.raises(RuntimeError, match="output_limit"):
        another.converse(modelId=registry.speech, inferenceConfig={"maxTokens": 2049})
    assert len(raw.calls) == 2


@pytest.mark.parametrize("transient_count", [1, 2])
@pytest.mark.parametrize("failure", ["unavailable", "timeout"])
def test_live_provider_path_retries_once_and_accounts_unknown_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transient_count: int,
    failure: str,
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    monkeypatch.setattr(check11b, "ROOT", tmp_path)
    audio = tmp_path / "lane/spikes/real_dictation_44s.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ID3synthetic")
    converter = ScriptedConverter()
    monkeypatch.setattr(check11b, "FFmpegConverter", lambda: converter)
    raw = ScriptedConverse(
        *[
            (TimeoutError if failure == "timeout" else RuntimeError)("PRIVATE provider response")
            for _ in range(transient_count)
        ],
        response(OWNER_SYNTHETIC.input + " NUMBERS: 53: سنة, 45: %, 560: HCT, 12.5: dose, 5: dose"),
        candidate(OWNER_SYNTHETIC.candidate.model_dump()),
    )
    monkeypatch.setattr("boto3.client", lambda *a, **kw: raw)
    monkeypatch.setattr("boto3.session.Session.client", lambda *a, **kw: raw)
    destination = tmp_path / "live-11b-synthetic.json"
    report = asyncio.run(run_check(destination))
    assert report["state"] == ("passed" if transient_count == 1 else "failed")
    assert report["speech_retry_count"] == 1
    assert report["speech_attempts"] == [
        failure,
        "ok" if transient_count == 1 else failure,
    ]
    assert len(raw.calls) == (3 if transient_count == 1 else 2)
    assert len(report["calls"]) == len(raw.calls)
    assert not report["calls"][0]["usage_known"] and report["calls"][0]["estimated_usd"] > 0
    assert report["estimated_usd"] < 0.15
    assert converter.calls == 1
    assert "PRIVATE" not in destination.read_text()
    assert OWNER_SYNTHETIC.input not in destination.read_text()
    with pytest.raises(RuntimeError, match="already recorded"):
        asyncio.run(run_check(destination))
    assert len(raw.calls) == (3 if transient_count == 1 else 2)


@pytest.mark.parametrize("failure", [ModelUnavailable(reason="budget_exhausted"), "", "NUMBERS: 5"])
def test_live_check_does_not_retry_other_speech_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str | ModelUnavailable,
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    caller, model = ScriptedSpeech(failure), ScriptedModel()
    report = asyncio.run(
        run_check(
            tmp_path / "live-11b-synthetic.json",
            speech=SpeechAdapter(caller, ScriptedConverter(), SOURCE),
            model_factory=lambda registry, role: model,
        )
    )
    assert report["state"] == "failed" and report["speech_retry_count"] == 0
    assert len(caller.calls) == 1 and model.script.calls == []


def test_request_retry_must_be_authorized_and_cannot_follow_extraction() -> None:
    raw = ScriptedConverse(response("synthetic"), response("synthetic"), response("synthetic"))
    client = DictationRequests(raw, DictationSpend(cap=0.15))
    speech, worker = ModelRegistry().speech, ModelRegistry().worker
    with pytest.raises(RuntimeError, match="retry_allowance"):
        client.allow_speech_retry()
    client.converse(modelId=speech, inferenceConfig={"maxTokens": 2048})
    with pytest.raises(RuntimeError, match="request_allowance"):
        client.converse(modelId=speech, inferenceConfig={"maxTokens": 2048})
    client.allow_speech_retry()
    with pytest.raises(RuntimeError, match="retry_allowance"):
        client.allow_speech_retry()
    client.converse(modelId=speech, inferenceConfig={"maxTokens": 2048})
    client.converse(modelId=worker, inferenceConfig={"maxTokens": 2048})
    with pytest.raises(RuntimeError, match="request_allowance"):
        client.converse(modelId=speech, inferenceConfig={"maxTokens": 2048})
    assert len(raw.calls) == 3


def test_audio_and_request_bounds_refuse_before_provider_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    caller, model = ScriptedSpeech(), ScriptedModel()
    report = asyncio.run(
        run_check(
            tmp_path / "live-11b-synthetic.json",
            speech=SpeechAdapter(
                caller, ScriptedConverter(ConvertedAudio(data=b"ID3", duration=61)), SOURCE
            ),
            model_factory=lambda registry, role: model,
        )
    )
    assert report["state"] == "failed" and not caller.calls and not model.script.calls
    raw = ScriptedConverse()
    client = DictationRequests(raw, DictationSpend(cap=0.15))
    with pytest.raises(RuntimeError, match="input_limit"):
        client.converse(
            modelId=ModelRegistry().speech, inferenceConfig={"maxTokens": 2048}, text="x" * 50001
        )
    with pytest.raises(RuntimeError, match="audio_limit"):
        client.converse(
            modelId=ModelRegistry().speech,
            inferenceConfig={"maxTokens": 2048},
            messages=[
                {"role": "user", "content": [{"audio": {"source": {"bytes": b"x" * 1000001}}}]}
            ],
        )
    client = DictationRequests(raw, DictationSpend(cap=0.000001))
    with pytest.raises(RuntimeError, match="cost_cap"):
        client.converse(modelId=ModelRegistry().speech, inferenceConfig={"maxTokens": 2048})
    assert raw.calls == []


@pytest.mark.parametrize(
    "variant",
    ["all_agree", "516", "embedded_history", "extra_dispute", "merged", "past", "wrong_generic"],
)
def test_attempt5_oracle_preserves_order_source_and_numeric_requirements(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    variant: str,
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    value = json.loads(OWNER_SYNTHETIC.candidate.model_dump_json())
    source = OWNER_SYNTHETIC.input
    if variant == "516":
        source = source.replace("560", "516")
        value["orders"][0]["dose"] = "516 12.5"
    if variant == "embedded_history":
        value["orders"][0].update(drug="إكس فورش إتش سي تي 560 12.5", dose=None)
        value["orders"][1].update(drug="كونكور 5", dose=None)
        value["facts"].append({"category": "medication_history", "text": "ماشي على كونكور"})
    if variant == "merged":
        value["orders"][0]["drug"] += " وConcor"
        value["orders"].pop(1)
    if variant == "past":
        value["facts"].append({"category": "medication_history", "text": "دواء قديم غير مأمور"})
    if variant == "wrong_generic":
        value["orders"][0]["name_latin"] = "Concor"
    numbers = "53 45 " + ("516" if variant == "516" else "560") + " 12.5 5"
    if variant == "extra_dispute":
        numbers += " 99"
    caller = ScriptedSpeech(source + " NUMBERS: " + numbers)
    model = ScriptedModel(candidate(value))
    report = asyncio.run(
        run_check(
            tmp_path / "live-11b-synthetic.json",
            speech=SpeechAdapter(caller, ScriptedConverter(), SOURCE),
            model_factory=lambda registry, role: model,
        )
    )
    assert report["state"] == (
        "passed" if variant in {"all_agree", "516", "embedded_history"} else "failed"
    )
    assert len(caller.calls) == len(model.script.calls) == 1
