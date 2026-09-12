"""Live opt-in plus an entirely scripted injection of the same runner."""

import json
import os
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
import pytest
from providers.fixtures import ScriptedModel, candidate
from scribe.test_natural_verbs_11N import NEGATIVES, PHRASES, value_for
from strands.models import Model

from sanad.models.registry import ModelRegistry, ModelRole

from .check11N import run_check


@pytest.mark.skipif(os.environ.get("SANAD_LIVE") != "1", reason="explicit live account check only")
def test_contract11N_live(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--live"):
        pytest.skip("only the isolated opt-in suite may use the account")
    report = run_check()
    assert report["state"] == "passed", "Read the redacted evidence; do not repeat the live check."


def test_runner_requires_explicit_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANAD_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="SANAD_LIVE=1"):
        run_check(tmp_path / "result.json")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mode", ["good", "wrong", "uncited", "failure"])
def test_runner_scripted_factory_is_all_or_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    live_calls: list[bool] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        live_calls.append(True)
        raise AssertionError("live factory reached in injected run")

    monkeypatch.setattr(boto3, "client", forbidden)
    scripts: list[dict[str, Any]] = []
    for index, row in enumerate((*PHRASES, *NEGATIVES)):
        value = value_for(row)
        if mode == "wrong" and index == 0:
            value["orders"][0]["action"] = "continue"
        if mode == "uncited":
            value["orders"][0].pop("action_quote")
        scripts.extend(candidate(value) for _ in range(4 if mode == "uncited" else 2))
    model = ScriptedModel(*scripts)

    def factory(registry: ModelRegistry, role: ModelRole) -> Model:
        if mode == "failure":
            raise RuntimeError("private provider body must never be written")
        return model

    destination = tmp_path / "result.json"
    report = run_check(destination, model_factory=factory)
    assert live_calls == []
    assert report["state"] == ("passed" if mode == "good" else "failed")
    if mode == "good":
        assert report["passed_count"] == 13
        assert report["positive_citation_count"] == 10
        assert len(model.script.calls) == 26
    if mode == "wrong":
        assert report["wrong_action_count"] >= 1
    if mode == "uncited":
        assert len(model.script.calls) == 52
        assert report["positive_citation_count"] == 0
    content = destination.read_text()
    assert json.loads(content) == report
    assert "private provider body" not in content
    assert "Synthetic Person" not in content
    assert all(row[0] not in content for row in (*PHRASES, *NEGATIVES))
    with pytest.raises(RuntimeError, match="already recorded"):
        run_check(destination, model_factory=factory)


def test_budget_reserves_before_io_and_counts_failed_calls() -> None:
    from providers.fixtures import ScriptedConverse, response

    from .check08 import SpendGuard, SpendLimit
    from .check11N import BoundedRequests

    wire = ScriptedConverse(response(), RuntimeError("withheld"))
    spend = SpendGuard(cap=0.30)
    client = BoundedRequests(wire, spend)
    client.converse(modelId=ModelRegistry().worker)
    assert len(spend.calls) == 1 and spend.calls[0]["usage_known"]
    with pytest.raises(RuntimeError, match="withheld"):
        client.converse(modelId=ModelRegistry().worker)
    assert len(spend.calls) == 2 and not spend.calls[1]["usage_known"]
    spend.estimated = spend.cap
    with pytest.raises(SpendLimit):
        client.converse(modelId=ModelRegistry().worker)
    assert len(wire.calls) == 2
    with pytest.raises(RuntimeError, match="input_size_limit"):
        client.converse(modelId=ModelRegistry().worker, text="a" * 50001)
    assert len(wire.calls) == 2
