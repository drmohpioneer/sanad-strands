"""Anchoring negatives and the bounded transport allowance, without provider IO."""

import json
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import (  # type: ignore[import-untyped]
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from live.check11e import EnglishSpend, Requests11e, agreement
from providers.fixtures import ScriptedConverse, response

from sanad.models.registry import ModelRegistry
from sanad.scribe.changes import combine_changes
from sanad.scribe.extract import DictationCandidate, OrderCandidate
from sanad.scribe.resolver import Context, resolve_tests, source_anchored
from sanad.scribe.resolver import test_reply_resolves as reply_resolves


@pytest.mark.parametrize(
    "name,source,anchored",
    [
        ("creatinine", "I ordered the one, create sodium and potassium", False),
        ("creatinine", "Request creatinin", True),
        ("creatinine", "Request creatinine", True),
        ("creatinine", "Request xcreatininez", False),
        ("Na", "Request sodium", True),
        ("K", "Request potassium", True),
        ("Na", "Request sudium", True),
        ("Na", "Request soxiumx", False),
        ("Na", "طلبت سوديوم", True),
        ("K", "طلبت بوتاسيوم", True),
        ("CBC", "Request XCBCY", False),
        ("Noveltest", "Request Noveltest", True),
    ],
)
def test_test_identity_needs_whole_source_token_or_one_edit(
    name: str, source: str, anchored: bool
) -> None:
    assert source_anchored(name, source) == anchored


@pytest.mark.parametrize(
    "proposed",
    [
        "create sodium and potassium and lipid profile",
        "creatinine, sodium, potassium, lipid profile",
        "sodium, potassium and lipid profile",
    ],
)
def test_guessed_or_omitted_garble_has_one_literal_quote(proposed: str) -> None:
    source = (
        "I ordered the one, create sodium and potassium and lipid profile and I can add Forxiga."
    )
    resolved, fragments = resolve_tests(proposed, source)
    assert [r.latin for r in resolved] == ["Na", "K", "lipid profile"]
    assert fragments == ("the one, create",)


@pytest.mark.parametrize(
    "source",
    [
        "I requested tests: sodium and potassium in five days and review tomorrow.",
        "طلبت منه سوديوم وبوتاسيوم يعملوه وبيراجع بكرة",
        "طلبت سوديوم وبوتاسيوم وبلغني لو البوتاسيوم فوق 5.5",
    ],
)
def test_request_grammar_deadlines_and_alerts_are_not_test_fragments(source: str) -> None:
    resolved, fragments = resolve_tests("sodium, potassium", source)
    assert [r.latin for r in resolved] == ["Na", "K"]
    assert not fragments


@pytest.mark.parametrize(
    "reply,proposed,settled",
    [
        ("Do the tests tomorrow", "sodium, potassium, lipid profile", False),
        ("Do sodium tomorrow", "sodium, potassium, lipid profile", False),
        ("I meant CBC", "CBC, sodium, potassium, lipid profile", True),
        ("Just sodium, potassium and lipid profile", "sodium, potassium, lipid profile", True),
    ],
)
def test_only_an_analyte_answer_settles_the_quoted_test(
    reply: str, proposed: str, settled: bool
) -> None:
    source = "I ordered the one, create sodium and potassium and lipid profile.\n" + reply
    assert (
        reply_resolves(proposed, ("Na", "K", "lipid profile"), reply, source, Context()) == settled
    )


@pytest.mark.parametrize(
    "source",
    [
        "Exforge 5/160. Increase to Imaginary HCT 10/160/25.",
        "Exforge 5/160. Increase to Exforge HCT. Concor 10/160/25.",
        "Exforge 5/160. Discuss Exforge HCT 10/160/25.",
    ],
)
def test_brand_recovery_requires_named_family_and_dose_at_change_target(source: str) -> None:
    order = OrderCandidate(action="change", drug="Exforge", dose="10/160/25")
    result = combine_changes(DictationCandidate(orders=(order,)), source, Context())
    assert result.orders == (order,)


@pytest.mark.parametrize(
    "error_type",
    [ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError],
)
def test_one_identical_transport_retry_counts_cost_and_preserves_failure(
    tmp_path: Path, error_type: Any
) -> None:
    raw = ScriptedConverse(error_type(endpoint_url="https://synthetic.invalid"), response("ok"))
    spend = EnglishSpend(cap=0.15)
    wire = Requests11e(raw, spend, tmp_path)
    wire.begin(1)
    wire.converse(modelId=ModelRegistry().speech, inferenceConfig={"maxTokens": 2048})
    assert len(raw.calls) == len(spend.calls) == 2
    assert raw.calls[0] == raw.calls[1]
    assert not spend.calls[0]["usage_known"]
    assert wire.counts[1] == [2, 0] and len(wire.transport_retries[1]) == 1
    failure = json.loads((tmp_path / "run-1-0-1.json").read_text())
    assert failure["failure_type"] == error_type.__name__ and "raw_failure" in failure
    assert "synthetic.invalid" not in str(spend.calls)


def test_transport_retry_is_shared_by_both_providers_and_then_run_fails(tmp_path: Path) -> None:
    closed = ConnectionClosedError(endpoint_url="https://synthetic.invalid")
    raw = ScriptedConverse(closed, response("ok"), closed)
    wire = Requests11e(raw, EnglishSpend(cap=0.15), tmp_path)
    wire.begin(1)
    wire.converse(modelId=ModelRegistry().speech, inferenceConfig={"maxTokens": 2048})
    with pytest.raises(ConnectionClosedError):
        wire.converse(
            modelId=ModelRegistry().worker, inferenceConfig={"maxTokens": 2048, "temperature": 0}
        )
    assert wire.transport_exhausted == {1} and len(raw.calls) == 3
    assert len(wire.transport_retries[1]) == 1
    with pytest.raises(RuntimeError, match="transport_retry_exhausted"):
        wire.converse(
            modelId=ModelRegistry().worker, inferenceConfig={"maxTokens": 2048, "temperature": 0}
        )
    assert len(raw.calls) == len(wire.spend.calls) == 3


def test_semantic_or_provider_rejection_has_no_transport_retry(tmp_path: Path) -> None:
    raw = ScriptedConverse(ClientError({"Error": {"Code": "ValidationException"}}, "Converse"))
    wire = Requests11e(raw, EnglishSpend(cap=0.15), tmp_path)
    wire.begin(1)
    with pytest.raises(ClientError):
        wire.converse(modelId=ModelRegistry().speech, inferenceConfig={"maxTokens": 2048})
    assert len(raw.calls) == 1 and not wire.transport_retries[1]


def test_live_agreement_compares_test_set_and_still_rejects_missing_analyte() -> None:
    runs = [
        {"tests": ["TEST: Na, K, lipid profile"], "test_analytes": ["k", "lipid profile", "na"]},
        {"tests": ["TEST: K, lipid profile, Na"], "test_analytes": ["k", "lipid profile", "na"]},
        {"tests": ["TEST: Na, K"], "test_analytes": ["k", "na"]},
    ]
    compared = agreement(runs)
    assert compared[1]["test_analytes"] and not compared[1]["test_line"]
    assert not compared[2]["test_analytes"]
