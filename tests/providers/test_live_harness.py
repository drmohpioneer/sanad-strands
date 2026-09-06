import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from live import check08

from .fixtures import ScriptedConverse, ScriptedConverter, candidate, document, png, response


def test_spend_cap_blocks_before_network_and_retains_unknown_cost() -> None:
    cap = check08.SpendGuard(estimated=0.49)
    with pytest.raises(check08.SpendLimit):
        cap.reserve(check08.REGISTRY.cross_check, 2048)
    cap = check08.SpendGuard()
    reservation = cap.reserve(check08.REGISTRY.worker, 2048)
    cap.finish(check08.REGISTRY.worker, reservation, {}, 1)
    assert cap.estimated == reservation and cap.calls[0]["usage_known"] is False
    reservation = cap.reserve(check08.REGISTRY.worker, 2048)
    cap.finish(check08.REGISTRY.worker, reservation, {"inputTokens": 100, "outputTokens": 25}, 1)
    assert cap.estimated < 2 * reservation


@pytest.mark.parametrize(
    "speech_reply,speech_passed,numbers_line",
    [
        (
            "تفريغ خاص مش للنشر 100 جرام وبعد كده 60 جرام\nNUMBERS: 200 جرام، 60 جرام",
            True,
            "parsed",
        ),
        (
            "PRIVATE TRANSCRIPT 100 grams and 60 grams\nNUMBERS: 200 grams, 60 grams",
            False,
            "parsed",
        ),
        ("تفريغ خاص مش للنشر 200 جرام\nNUMBERS: 200 جرام", False, "parsed"),
        ("تفريغ خاص مش للنشر 60 جرام", True, "missing"),
        ("تفريغ خاص مش للنشر 60 جرام\nNUMBERS: unknown", True, "malformed"),
        ("PRIVATE TRANSCRIPT 60 grams", False, "missing"),
        ("تفريغ خاص مش للنشر 200 جرام", False, "missing"),
        ("", False, None),
    ],
)
def test_entire_live_harness_offline_redaction_and_run_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    speech_reply: str,
    speech_passed: bool,
    numbers_line: str | None,
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    monkeypatch.setattr(check08, "ROOT", tmp_path)
    fixtures = tmp_path / "lane/spikes"
    fixtures.mkdir(parents=True)
    (fixtures / "lab_synthetic.png").write_bytes(png())
    (fixtures / "injection_synthetic.png").write_bytes(png(3, 3))

    def answer(kwargs: dict[str, Any]) -> dict[str, Any]:
        tool_specs = kwargs.get("toolConfig", {}).get("tools", [])
        if tool_specs:
            assert [t["toolSpec"]["name"] for t in tool_specs] == ["test_outside_allowlist"]
            return response(calls=[("test_outside_allowlist", {"payload": {"text": "synthetic"}})])
        content = kwargs["messages"][-1]["content"]
        if "audio" in content[0]:
            return response(speech_reply)
        if "text" in content[0]:
            return candidate({"number": "72"})
        if content[0]["image"]["source"]["bytes"] == png(3, 3):
            return response(
                document(items=[{"name": "Potassium", "value": "4.1", "unit": "mmol/L"}])
            )
        return response(document())

    fake = ScriptedConverse(*([answer] * 16))
    monkeypatch.setattr("boto3.client", lambda *a, **kw: fake)
    # BedrockModel obtains its client through boto3.Session as well as boto3.client.
    monkeypatch.setattr("boto3.session.Session.client", lambda *a, **kw: fake)

    def convert(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert Path(argv[argv.index("-i") + 1]).name == "sample_ar_numbers_15s.wav"
        Path(argv[-1]).write_bytes(b"OggSsynthetic")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", convert)
    monkeypatch.setattr(check08, "FFmpegConverter", lambda *a: ScriptedConverter())
    destination = tmp_path / "evidence.json"
    result = asyncio.run(check08.run_check(destination))
    assert result["state"] == ("passed" if speech_passed else "failed")
    assert len(result["checks"]) == 15
    assert len(result["calls"]) == 16
    assert "تفريغ خاص" not in destination.read_text()
    assert "PRIVATE TRANSCRIPT" not in destination.read_text()
    assert "اسم غير موثوق" not in destination.read_text()
    with pytest.raises(FileExistsError):
        asyncio.run(check08.run_check(destination))
    assert len(fake.calls) == 16
    assert json.loads(destination.read_text())["run_count"] == 1
    speech = result["checks"]["voxtral_ogg_to_mp3"]
    assert speech["passed"] is speech_passed
    assert speech["numbers_line"] == numbers_line
    if speech_passed:
        assert speech["arabic_ratio"] > 0.8
        if numbers_line == "parsed":
            assert speech["numbers"] == ["100", "60"]
            assert speech["heard_numbers"] == ["200 جرام", "60 جرام"]
            assert speech["disputed_numbers"] == ["100", "200"]
        else:
            assert speech["numbers"] == speech["disputed_numbers"] == ["60"]
            assert speech["heard_numbers"] == []


def test_live_refuses_without_explicit_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SANAD_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="SANAD_LIVE"):
        asyncio.run(check08.run_check(tmp_path / "evidence.json"))
    assert not (tmp_path / "evidence.json").exists()
