import copy
from pathlib import Path
from typing import Any

import httpx
import pytest
from live import check11c
from live.check11b import DictationSpend
from live.check11c import Requests11c, run_check
from providers.fixtures import (
    ScriptedConverse,
    ScriptedConverter,
    ScriptedModel,
    ScriptedSpeech,
    candidate,
    response,
)
from providers.rxnorm_fixture import RxNormFixture
from store.test_scribe_11c import SOURCE, VALUE

from sanad.models.io import ModelUnavailable
from sanad.models.registry import ModelRegistry


@pytest.mark.parametrize("bad_correction", [False, True])
def test_11c_two_part_oracle_and_durable_once_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_correction: bool
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    value = copy.deepcopy(VALUE)
    value["facts"].append(
        {
            "category": "finding",
            "clinical_kind": "ECG",
            "text": "تي ويف انفريجن",
            "clinical_en": "T wave inversion",
            "terms": [{"spoken": "تي ويف انفريجن", "english": "T wave inversion"}],
        }
    )
    corrected = copy.deepcopy(value)
    corrected["orders"][0]["dose"] = "5/160/12.5"
    corrected["orders"][2]["dose"] = "10, 45" if bad_correction else "10"
    corrected["facts"][0]["clinical_en"] = "EF 45%"
    model = ScriptedModel(
        response(calls=[("lookup_drug", {"name": "Bisoprolol"})]),
        candidate(value),
        candidate(value),
        candidate(corrected),
        candidate(corrected),
    )
    speech = ScriptedSpeech(SOURCE + " وتي ويف انفريجن NUMBERS: 53 516 12.5 5 45")
    fixture = RxNormFixture()
    options: dict[str, Any] = {
        "model_factory": lambda registry, role: model,
        "speech_caller": speech,
        "converter": ScriptedConverter(),
        "rxnorm_client": fixture.client,
        "data": b"OggSsynthetic",
    }
    destination = tmp_path / "live-11c-synthetic.json"
    earlier = tmp_path / check11c.EARLIER_EVIDENCE
    earlier.write_text('{"attempt": 1, "state": "failed"}\n')
    original = earlier.read_bytes()
    attempt2 = tmp_path / "live-11c-2026-09-07b.json"
    attempt2.write_text('{"attempt": 2, "state": "failed"}\n')
    attempt2_original = attempt2.read_bytes()
    attempt3 = tmp_path / "live-11c-2026-09-07c.json"
    attempt3.write_text('{"attempt": 3, "state": "failed"}\n')
    attempt3_original = attempt3.read_bytes()
    attempt4 = tmp_path / "live-11c-2026-09-07d.json"
    attempt4.write_text('{"attempt": 4, "state": "failed"}\n')
    attempt4_original = attempt4.read_bytes()
    report = run_check(destination, private_review=tmp_path / "review.json", **options)
    assert earlier.read_bytes() == original
    assert attempt2.read_bytes() == attempt2_original
    assert attempt3.read_bytes() == attempt3_original
    assert attempt4.read_bytes() == attempt4_original
    assert report["attempt"] == 5
    assert report["parts"]["1"]["route_status"] == "proposed"
    assert report["parts"]["1"]["failure_code"] is None
    assert report["parts"]["1"]["state"] == "passed"
    assert report["parts"]["2"]["state"] == ("failed" if bad_correction else "passed")
    assert report["state"] == ("failed" if bad_correction else "passed")
    assert len(speech.calls) == 1 and len(model.script.calls) == 5
    assert len(fixture.calls) <= 6
    assert "سامي" not in destination.read_text() and SOURCE not in destination.read_text()
    assert report["memory_rows_written"] > 0
    with pytest.raises(RuntimeError, match="already recorded"):
        run_check(tmp_path / "live-11c-another-day.json", **options)
    assert len(speech.calls) == 1 and len(model.script.calls) == 5


@pytest.mark.parametrize("restore_labs", [False, True])
def test_attempt5_live_oracle_preserves_labs_or_block_and_does_not_confirm_blocked_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, restore_labs: bool
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    value = copy.deepcopy(VALUE)
    value["missions"] = []
    value["facts"].append(
        {
            "category": "finding",
            "kind": "ECG",
            "text": "تي ويف انفريجن",
            "terms": [{"spoken": "تي ويف انفريجن", "english": "T wave inversion"}],
        }
    )
    retried = copy.deepcopy(value)
    if restore_labs:
        retried["missions"] = copy.deepcopy(VALUE["missions"])
    corrected = copy.deepcopy(retried)
    corrected["orders"][0]["dose"] = "5/160/12.5"
    corrected["orders"][2]["dose"] = "10"
    model = ScriptedModel(
        response(calls=[("lookup_drug", {"name": "Bisoprolol"})]),
        candidate(value),
        candidate(retried),
        *([] if restore_labs else [candidate(retried)]),
        candidate(corrected),
        candidate(corrected),
    )
    speech = ScriptedSpeech(SOURCE + " وتي ويف انفريجن وطلبت تحليل NUMBERS: 53 516 12.5 5 45")
    fixture = RxNormFixture()
    report = run_check(
        tmp_path / "live-11c-synthetic.json",
        model_factory=lambda registry, role: model,
        speech_caller=speech,
        converter=ScriptedConverter(),
        rxnorm_client=fixture.client,
        data=b"OggSsynthetic",
        private_review=tmp_path / "review.json",
    )
    assert report["state"] == "passed"
    assert report["parts"]["1"]["four_english_labs"] == restore_labs
    assert report["parts"]["1"]["blocking_request_present"] != restore_labs
    assert report["parts"]["2"]["confirmation_status"] == (
        "confirmed" if restore_labs else "blocked"
    )
    assert bool(report["memory_rows_written"]) == restore_labs
    assert len(model.script.calls) == (5 if restore_labs else 6)
    assert len(speech.calls) == 1 and len(fixture.calls) <= 6
    assert report["retries"] == (
        []
        if restore_labs
        else [{"provider": "scribe", "part": 0, "attempt": 2, "reason": "request_missing"}]
    )
    assert not model.script.scripts


def test_11c_wire_request_count_order_size_and_cost() -> None:
    raw = ScriptedConverse(*(response("synthetic") for _ in range(29)))
    client = Requests11c(raw, DictationSpend(cap=0.1))
    registry = ModelRegistry()
    with pytest.raises(RuntimeError, match="extraction_allowance"):
        client.converse(modelId=registry.worker, inferenceConfig={"maxTokens": 2048})
    client.converse(modelId=registry.speech, inferenceConfig={"maxTokens": 2048})
    with pytest.raises(RuntimeError, match="speech_allowance"):
        client.converse(modelId=registry.speech, inferenceConfig={"maxTokens": 2048})
    for part in (1, 2):
        for _ in range(14):
            client.converse(modelId=registry.worker, inferenceConfig={"maxTokens": 2048})
        with pytest.raises(RuntimeError, match="extraction_allowance"):
            client.converse(modelId=registry.worker, inferenceConfig={"maxTokens": 2048})
        if part == 1:
            client.correction()
    with pytest.raises(RuntimeError, match="part_allowance"):
        client.correction()
    assert len(raw.calls) == 29
    for kwargs, reason in (({"maxTokens": 2049}, "output_limit"),):
        with pytest.raises(RuntimeError, match=reason):
            Requests11c(raw, DictationSpend(cap=0.1)).converse(
                modelId=registry.speech, inferenceConfig=kwargs
            )
    with pytest.raises(RuntimeError, match="cost_cap"):
        Requests11c(raw, DictationSpend(cap=0.00001)).converse(
            modelId=registry.speech, inferenceConfig={"maxTokens": 2048}
        )
    assert len(raw.calls) == 29


def test_real_provider_composition_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    monkeypatch.setattr(check11c, "ROOT", tmp_path)
    source = tmp_path / "lane/spikes/real_dictation_44s.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"OggSsynthetic")
    value = copy.deepcopy(VALUE)
    value["facts"].append(
        {
            "category": "finding",
            "clinical_kind": "ECG",
            "text": "تي ويف انفريجن",
            "clinical_en": "T wave inversion",
            "terms": [{"spoken": "تي ويف انفريجن", "english": "T wave inversion"}],
        }
    )
    edited = copy.deepcopy(value)
    edited["orders"][0]["dose"] = "5/160/12.5"
    edited["orders"][2]["dose"] = "10"
    edited["facts"][0]["clinical_en"] = "EF 45%"
    raw = ScriptedConverse(
        response(SOURCE + " وتي ويف انفريجن NUMBERS: 53 516 12.5 5 45"),
        response(calls=[("lookup_drug", {"name": "Bisoprolol"})]),
        candidate(value),
        candidate(value),
        candidate(edited),
        candidate(edited),
    )
    fixture = RxNormFixture()
    monkeypatch.setattr(check11c, "FFmpegConverter", ScriptedConverter)
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: raw)
    monkeypatch.setattr("boto3.session.Session.client", lambda *args, **kwargs: raw)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: fixture.client)
    report = run_check(tmp_path / "live-11c-synthetic.json")
    assert report["state"] == "passed" and len(raw.calls) == 6
    assert len(report["calls"]) == 6 and 0 < report["estimated_usd"] < 0.1


@pytest.mark.parametrize("failure", ["schema_validation", "timeout"])
def test_missing_card_keeps_route_and_failure_code_without_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    private_reply = "private malformed model reply"
    model = ScriptedModel(*(response(private_reply) for _ in range(4)))
    speech = ScriptedSpeech(
        ModelUnavailable(reason="timeout")
        if failure == "timeout"
        else SOURCE + " NUMBERS: 53 516 12.5 5 45"
    )
    fixture = RxNormFixture()
    destination = tmp_path / "live-11c-synthetic.json"
    report = run_check(
        destination,
        model_factory=lambda registry, role: model,
        speech_caller=speech,
        converter=ScriptedConverter(),
        rxnorm_client=fixture.client,
        data=b"OggSsynthetic",
    )
    assert report["state"] == "failed" and report["parts"]["2"]["state"] == "not_run"
    assert report["parts"]["1"]["route"] == "doctor"
    assert report["parts"]["1"]["route_status"] == (
        "timeout" if failure == "timeout" else "model_unavailable"
    )
    assert report["parts"]["1"]["failure_code"] == failure
    assert len(speech.calls) == 1
    assert len(model.script.calls) == (0 if failure == "timeout" else 4)
    assert SOURCE not in destination.read_text() and private_reply not in destination.read_text()


def test_rxnorm_403_is_recorded_without_requiring_real_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    value = copy.deepcopy(VALUE)
    value["facts"].append(
        {
            "category": "finding",
            "clinical_kind": "ECG",
            "text": "تي ويف انفريجن",
            "clinical_en": "T wave inversion",
            "terms": [{"spoken": "تي ويف انفريجن", "english": "T wave inversion"}],
        }
    )
    corrected = copy.deepcopy(value)
    corrected["orders"][0]["dose"] = "5/160/12.5"
    corrected["orders"][2]["dose"] = "10"
    corrected["facts"][0]["clinical_en"] = "EF 45%"
    model = ScriptedModel(
        response(calls=[("lookup_drug", {"name": "Bisoprolol"})]),
        candidate(value),
        candidate(value),
        candidate(corrected),
        candidate(corrected),
    )
    speech = ScriptedSpeech(SOURCE + " وتي ويف انفريجن NUMBERS: 53 516 12.5 5 45")
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, text="private upstream HTML")
        )
    ) as client:
        report = run_check(
            tmp_path / "live-11c-synthetic.json",
            model_factory=lambda registry, role: model,
            speech_caller=speech,
            converter=ScriptedConverter(),
            rxnorm_client=client,
            data=b"OggSsynthetic",
        )
    assert report["state"] == "passed"
    assert report["rxnorm_http_calls"] > 0
    assert all(
        r["http_status"] == 403 and r["failure_code"] == "http_status"
        for r in report["rxnorm_outcomes"]
    )
    assert "private upstream" not in str(report)
