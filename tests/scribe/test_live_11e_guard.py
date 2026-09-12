"""Exercise the complete five-run harness hermetically, including its one-shot claim."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from live.check11b import DictationSpend
from live.check11e import (
    EnglishAudio,
    EnglishSpend,
    Requests11e,
    card_parts,
    prompt_within_budget,
    run_check,
)
from providers.fixtures import (
    ScriptedConverse,
    ScriptedConverter,
    ScriptedModel,
    ScriptedSpeech,
    candidate,
    response,
)
from providers.rxnorm_fixture import RxNormFixture

from sanad.models.registry import ModelRegistry
from scribe.english_dictations import SOURCE, VALUE


@pytest.mark.parametrize("defect", [None, "dose", "history", "echo"])
@pytest.mark.usefixtures("legacy_dictation_schema")
def test_five_full_runs_and_cross_run_agreement_are_not_self_confirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, defect: str | None
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    scripts: list[dict[str, Any]] = []
    for run in range(5):
        value = copy.deepcopy(VALUE)
        if run == 4:
            if defect == "dose":
                value["orders"][0]["dose"] = "5"
            elif defect == "history":
                value["facts"] = [value["facts"][0]]
            elif defect == "echo":
                value["facts"][2]["category"] = "history"
        scripts.extend((candidate(value), candidate(copy.deepcopy(value))))
    model = ScriptedModel(*scripts)
    speech = ScriptedSpeech(*(SOURCE + " NUMBERS: 53 5 160 10 160 25 45 3 5" for _ in range(5)))
    options: dict[str, Any] = dict(
        model_factory=lambda *args: model,
        speech_caller=speech,
        converter=ScriptedConverter(),
        rxnorm_client=RxNormFixture().client,
        data=b"OggSsynthetic",
        private_review=tmp_path / "private",
    )
    path = tmp_path / "live-11e-synthetic.json"
    earlier = tmp_path / "live-11e-2026-09-07.json"
    earlier.write_text('{"state": "failed", "runs": []}\n')
    original = earlier.read_bytes()
    report = run_check(path, **options)
    assert earlier.read_bytes() == original and report["attempt"] == 8
    # 6d keeps the undosed Forxiga under Needs confirmation; this old
    # complete-card harness must not certify it as an accepted plain line.
    assert report["state"] == "failed"
    assert all(not run["checks"]["forxiga_start"] for run in report["runs"])
    assert len(report["runs"]) == len(speech.calls) == 5
    assert len(model.script.calls) == 10
    assert all(r["test_line"] for r in report["agreement"])
    final = report["agreement"][4]
    assert final["same_history_count"] == (defect != "history")
    assert final["all_drugs"] == final["exforge_line"] == (defect != "dose")
    assert report["runs"][4]["checks"]["echo_history"] == (defect not in {"history", "echo"})
    assert len(list((tmp_path / "private").glob("run-*-proposal.json"))) == 5
    with pytest.raises(RuntimeError, match="already recorded"):
        run_check(tmp_path / "live-11e-another.json", **options)
    assert len(speech.calls) == 5 and len(model.script.calls) == 10


def test_requests_bound_models_temperature_runs_and_keep_raw_failure_privately(
    tmp_path: Path,
) -> None:
    registry = ModelRegistry()
    raw = ScriptedConverse(response("synthetic"), RuntimeError("private provider failure"))
    wire = Requests11e(raw, DictationSpend(cap=0.15), tmp_path)
    with pytest.raises(RuntimeError, match="run_allowance"):
        wire.converse(modelId=registry.speech, inferenceConfig={"maxTokens": 2048})
    wire.begin(1)
    wire.converse(modelId=registry.speech, inferenceConfig={"maxTokens": 2048})
    with pytest.raises(RuntimeError, match="speech_allowance"):
        wire.converse(modelId=registry.speech, inferenceConfig={"maxTokens": 2048})
    with pytest.raises(RuntimeError, match="temperature_limit"):
        wire.converse(
            modelId=registry.worker, inferenceConfig={"maxTokens": 2048, "temperature": 1}
        )
    with pytest.raises(RuntimeError, match="private provider failure"):
        wire.converse(
            modelId=registry.worker, inferenceConfig={"maxTokens": 2048, "temperature": 0}
        )
    assert (
        json.loads((tmp_path / "run-1-1-1.json").read_text())["raw_failure"]
        == "private provider failure"
    )
    assert "private provider failure" not in str(wire.spend.calls)
    for run in range(2, 6):
        wire.begin(run)
    with pytest.raises(RuntimeError, match="run_allowance"):
        wire.begin(6)
    assert len(raw.calls) == 2
    assert wire.spend.estimated < 0.15


@pytest.mark.parametrize("language,measured,ceiling", [("ar", 2655, 2800), ("en", 2878, 3000)])
def test_prompt_token_budget_rejects_missing_or_over_budget_usage(
    language: str, measured: int, ceiling: int
) -> None:
    assert prompt_within_budget(measured, language)
    assert prompt_within_budget(ceiling - 1, language)
    assert not prompt_within_budget(ceiling, language)
    assert not prompt_within_budget(4500, language)
    assert not prompt_within_budget(0, language)
    assert not prompt_within_budget(measured, "unknown")


def test_english_audio_allowance_and_unknown_usage_reserve_two_minutes() -> None:
    from sanad.media.audio import ConversionFailure, ConvertedAudio

    class Converter:
        def convert(self, audio: bytes, fmt: str) -> ConvertedAudio:
            return ConvertedAudio(data=audio, duration=float(fmt))

    bounded = EnglishAudio(Converter())
    assert isinstance(bounded.convert(b"audio", "61.5535"), ConvertedAudio)
    assert isinstance(bounded.convert(b"audio", "62.1"), ConversionFailure)
    spend = EnglishSpend(cap=0.15)
    model = ModelRegistry().speech
    reserved = spend.reserve(model, 2048)
    assert reserved > 0.012
    spend.finish(model, reserved, {}, 1)
    assert spend.estimated == reserved and not spend.calls[0]["usage_known"]
    reserve2 = spend.reserve(model, 2048)
    spend.finish(model, reserve2, {"inputTokens": 100, "outputTokens": 100}, 1)
    assert spend.calls[1]["estimated_usd"] == pytest.approx(0.01245)


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_hidden_card_cannot_pass_from_candidate_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from store.conftest import Clock
    from store.scribe_fixtures import ScribeWorld

    from sanad.store.memory import MemoryStore

    clock = Clock()
    world = ScribeWorld.create(MemoryStore(clock=clock), clock)
    world.approve(language="en")
    p = world.dictate(SOURCE, VALUE)
    checks = card_parts(p)["checks"]
    assert checks["card_visible"] and checks["exforge_change"]
    assert not checks["forxiga_start"]
    assert "Forxiga (start)" in card_parts(p)["card"].split("Needs confirmation:")[1]
    monkeypatch.setattr(
        "live.check11e.render_card",
        lambda p: (
            "New patient: Ahmed Saad, 53\nNeeds confirmation:\nToo large\nvalid 30 minutes",
        ),
    )
    parts = card_parts(p)
    assert not parts["drugs"] and not parts["tests"] and not parts["tasks"]
    assert not parts["checks"]["card_visible"] and not parts["checks"]["exforge_change"]


def test_recorded_prompt_budget_when_live_evidence_exists() -> None:
    for file in Path("docs/evidence").glob("live-11e-*.json"):
        report = json.loads(file.read_text())
        for run in report["runs"]:
            # Historical evidence predates the language field; its recorded allowance
            # explicitly identifies English. Do not rewrite an earlier failed report.
            language = run.get("language", report.get("language")) or (
                "en" if "English" in report.get("allowance", "") else "ar"
            )
            for count in run.get("prompt_input_tokens", []):
                assert prompt_within_budget(count, language), (file, run["run"], count, language)


@pytest.mark.parametrize(
    "age,accepted",
    [
        ("53", True),
        ("53, male", True),
        ("53 years old, male", True),
        ("53 years", True),
        ("53 yrs, male", True),
        ("54 years old, male", False),
        ("53 years old, female", False),
    ],
)
@pytest.mark.usefixtures("legacy_dictation_schema")
def test_patient_oracle_allows_age_word_without_relaxing_identity(
    monkeypatch: pytest.MonkeyPatch, age: str, accepted: bool
) -> None:
    from store.conftest import Clock
    from store.scribe_fixtures import ScribeWorld

    from sanad.store.memory import MemoryStore

    clock = Clock()
    world = ScribeWorld.create(MemoryStore(clock=clock), clock)
    world.approve(language="en")
    p = world.dictate(SOURCE, VALUE)
    card = card_parts(p)["card"].replace("Ahmed Saad, 53", "Ahmed Saad, " + age)
    monkeypatch.setattr("live.check11e.render_card", lambda p: (card,))
    assert card_parts(p)["checks"]["patient"] == accepted


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_oracle_rejects_duplicate_drug_and_observation_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from store.conftest import Clock
    from store.scribe_fixtures import ScribeWorld

    from sanad.store.memory import MemoryStore

    clock = Clock()
    world = ScribeWorld.create(MemoryStore(clock=clock), clock)
    world.approve(language="en")
    p = world.dictate(SOURCE, VALUE)
    card = (
        card_parts(p)["card"]
        .replace("Requested:", "Exforge\nRequested:")
        .replace(
            "Needs confirmation:",
            "Notify me if:\nblood pressure is high, 150/90\nNeeds confirmation:",
        )
    )
    monkeypatch.setattr("live.check11e.render_card", lambda p: (card,))
    checks = card_parts(p)["checks"]
    assert not checks["one_line_per_drug"] and not checks["no_unrequested_alerts"]
