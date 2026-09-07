"""Prove the live allowance and limits offline before owner-account execution."""

from pathlib import Path

import pytest
from live.check08 import SpendGuard
from live.check10 import EVIDENCE, MESSAGES, FiveRequests, run_check
from providers.fixtures import ScriptedConverse, ScriptedModel, candidate, response

from sanad.concierge.education import retrieve
from sanad.models.registry import ModelRegistry


def test_live_runner_hermetic_five_messages_and_refuses_second_allowance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert EVIDENCE.name == "live-10-2026-09-07b.json"
    target = tmp_path / EVIDENCE.name
    previous = tmp_path / "live-10-2026-09-07.json"
    previous.write_text('{"state": "failed", "attempt": 1}')
    monkeypatch.delenv("SANAD_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="SANAD_LIVE"):
        run_check(target)
    monkeypatch.setenv("SANAD_LIVE", "1")
    lines: list[tuple[str, list[str]]] = [
        ("الدكتور قالك: Atorvastatin، 40 مج، مرة يوميا، بالليل", [])
    ]
    for query in MESSAGES[1:]:
        entry = retrieve(query, synthetic=True)[0]
        lines.append((entry.lines("ar")[0], [entry.source_label]))
    models = [
        ScriptedModel(
            candidate(
                {
                    "reply": line,
                    "kind": "education" if labels else "plan",
                    "needs_doctor": False,
                }
            )
        )
        for line, labels in lines
    ]
    remaining = iter(models)
    report = run_check(target, model_factory=lambda registry, role: next(remaining))
    assert report["state"] == "passed" and len(report["checks"]) == 5
    assert report["attempt"] == 2
    assert all(c["failure_codes"] == [] for c in report["checks"])
    assert previous.read_text() == '{"state": "failed", "attempt": 1}'
    assert all(len(m.script.calls) == 1 for m in models)
    assert not any(message in target.read_text() for message in MESSAGES)
    assert "synthetic-media" not in target.read_text()
    for path in (target, tmp_path / "live-10-another.json"):
        with pytest.raises(RuntimeError, match="already recorded"):
            run_check(path, model_factory=lambda registry, role: next(remaining))


def test_live_gate_results_are_per_turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    replies = ["التحاليل سليمة ومفيش أي خطر عليك"] + [
        retrieve(query, synthetic=True)[0].lines("ar")[0] for query in MESSAGES[1:]
    ]
    models = iter(
        ScriptedModel(candidate({"reply": line, "kind": "plan", "needs_doctor": False}))
        for line in replies
    )
    report = run_check(tmp_path / EVIDENCE.name, model_factory=lambda registry, role: next(models))
    assert report["state"] == "failed"
    assert report["checks"][0]["failure_codes"] == ["concierge_sentence_not_grounded"]
    assert all(c["passed"] and c["failure_codes"] == [] for c in report["checks"][1:])


def test_live_budget_and_five_request_guard_reserve_before_io() -> None:
    raw = ScriptedConverse(*(response("synthetic") for _ in range(5)))
    spend = SpendGuard(cap=0.20)
    client = FiveRequests(raw, spend)
    kwargs = {"modelId": ModelRegistry().worker, "inferenceConfig": {"maxTokens": 2048}}
    for _ in range(5):
        client.converse(**kwargs)
    with pytest.raises(RuntimeError, match="five_worker_request_limit"):
        client.converse(**kwargs)
    assert len(raw.calls) == 5 and len(spend.calls) == 5 and spend.estimated < 0.20
    denied = FiveRequests(raw, SpendGuard(cap=0.000001))
    with pytest.raises(RuntimeError):
        denied.converse(**kwargs)
    assert len(raw.calls) == 5
    for bad in (
        {"modelId": ModelRegistry().cross_check},
        {"modelId": ModelRegistry().worker, "inferenceConfig": {"maxTokens": 2049}},
        {**kwargs, "input": "x" * 50001},
    ):
        with pytest.raises(RuntimeError):
            FiveRequests(raw, SpendGuard(cap=0.20)).converse(**bad)
    assert len(raw.calls) == 5


def test_every_education_sentence_passes_unchanged_kernel_in_both_languages() -> None:
    from sanad.agents.hygiene import patient_failure
    from sanad.concierge.education import source_set
    from sanad.safety.models import OutputContext
    from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT

    for entry in source_set():
        for language in ("ar", "en"):
            for line in entry.lines(language):
                context = OutputContext(
                    active_orders=(),
                    allowed_numbers=(line,),
                    mode="general_education",
                    language=language,
                )
                assert patient_failure(line, context, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT) is None, (
                    entry.id,
                    line,
                )
