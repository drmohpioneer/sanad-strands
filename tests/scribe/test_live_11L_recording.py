"""Private diagnostic capture follows reader identity, not completion order."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from live.check11e import record_stages

from sanad.agents.factory import Proposal as ModelProposal
from sanad.domain import CandidateRef
from sanad.scribe import merge, turn
from sanad.scribe.extract import DictationCandidate, OrderCandidate, ProposalIssue


def test_concurrent_readers_and_retry_keep_identity_and_private_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    value = DictationCandidate(orders=(OrderCandidate(drug="Forxiga", action="start"),))
    value._merge_issues = (ProposalIssue(item="order:0", code="clarification"),)
    value._single_source = ("order:0",)
    value._dropped_numbers = ("10",)
    value._malformed_items = True

    async def propose(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0)
        return ModelProposal(
            candidate=CandidateRef(candidate_id="synthetic-reading", version=1),
            value=value,
            provenance=(),
            unsupported_spans=(),
            metadata=(),
        )

    monkeypatch.setattr(turn, "make_agent", lambda: SimpleNamespace(propose=propose))
    monkeypatch.setattr(merge, "merge_candidates", lambda *args: SimpleNamespace(candidate=value))
    details: dict[str, Any] = {}

    async def reading(index: int, attempt: int = 0) -> None:
        factory: Any = turn.make_agent  # type: ignore[attr-defined]
        agent = factory()
        await agent.propose()

    async def run() -> None:
        await asyncio.gather(reading(1), reading(0))
        await reading(0, 1)
        merge.merge_candidates(value, value, "Start Forxiga.")

    with record_stages(tmp_path, 1, details):
        asyncio.run(run())
    stages = details["extraction_stages"]
    assert [(s["stage"], s.get("reader"), s.get("attempt")) for s in stages] == [
        ("reading", 2, 1),
        ("reading", 1, 1),
        ("retry", 1, 2),
        ("merge_before_sealing", None, None),
    ]
    assert all(s["forxiga_start"] for s in stages)
    assert "candidate" not in json.dumps(stages).replace("candidate_returned", "")
    for stage in stages:
        saved = json.loads((tmp_path / stage["private_file"]).read_text())
        assert saved["candidate"] == value.model_dump(mode="json")
        assert saved["private_metadata"] == {
            "_merge_issues": [value._merge_issues[0].model_dump(mode="json")],
            "_single_source": ["order:0"],
            "_dropped_numbers": ["10"],
            "_malformed_items": True,
        }


def test_attempt_seven_is_preserved_and_attempt_eight_cannot_repeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from live.check11e import run_check
    from providers.fixtures import ScriptedConverter, ScriptedModel, ScriptedSpeech
    from providers.rxnorm_fixture import RxNormFixture
    from store.scribe_fixtures import ScribeWorld

    monkeypatch.setenv("SANAD_LIVE", "1")

    def fail_before_providers(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic setup failure")

    monkeypatch.setattr(ScribeWorld, "post", fail_before_providers)
    earlier = tmp_path / "live-11e-attempt7.json"
    earlier.write_text('{"implementation_attempt": 7}\n')
    original = earlier.read_bytes()
    options: dict[str, Any] = {
        "model_factory": lambda *args: ScriptedModel(),
        "speech_caller": ScriptedSpeech(),
        "converter": ScriptedConverter(),
        "rxnorm_client": RxNormFixture().client,
        "data": b"synthetic audio",
        "private_review": tmp_path / "private",
    }
    report = run_check(tmp_path / "live-11e-attempt8.json", **options)
    assert report["implementation_attempt"] == 8
    assert report["state"] == "failed"
    assert earlier.read_bytes() == original
    assert report["calls"] == []
    with pytest.raises(RuntimeError, match="already recorded"):
        run_check(tmp_path / "live-11e-another.json", **options)
    assert not (tmp_path / "live-11e-another.json").exists()


@pytest.mark.parametrize("action,present", [("start", True), ("add", False)])
def test_saved_failure_is_reproduced_at_candidate_validation(action: str, present: bool) -> None:
    """Reproduce the saved pre-citation failure through its legacy schema."""

    raw = {"orders": [{"drug": "Forxiga", "action": action}]}
    first = DictationCandidate.model_validate(raw)
    second = DictationCandidate.model_validate(raw)
    assert bool(first.orders) == bool(second.orders) == present
    assert first._malformed_items == second._malformed_items == (not present)
    merged = merge.merge_candidates(first, second, "I can add Forxiga.")
    assert merged is not None
    assert any(o.drug == "Forxiga" for o in merged.candidate.orders) == present
