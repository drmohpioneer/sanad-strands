"""Exactly one three-request Nova Lite probe; evidence contains no provider bodies."""

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from scribe.dictations import MEASURED
from strands.models import BedrockModel, Model

from sanad.agents.factory import Proposal, make_agent, propose
from sanad.media.numbers import numbers_in
from sanad.models.io import private_provider_logs
from sanad.models.registry import ModelRegistry, ModelRole
from sanad.scribe.extract import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    DictationCandidate,
    candidate_issues,
    extracted_numbers,
)

from .check08 import BudgetClient, SpendGuard, scoped

ROOT = Path(__file__).resolve().parents[2]
DATE = datetime.now(ZoneInfo("Africa/Cairo")).date().isoformat()
EVIDENCE = ROOT / "docs/evidence/live-09a-2026-09-07b.json"


class ThreeRequests(BudgetClient):
    count = 0

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        if self.count >= 3:
            raise RuntimeError("three_request_limit")
        if kwargs.get("modelId") != ModelRegistry().worker:
            raise RuntimeError("worker_only")
        if len(json.dumps(kwargs).encode()) > 50000:
            raise RuntimeError("input_size_limit")
        self.count += 1
        return super().converse(**kwargs)


async def run_check(
    destination: Path = EVIDENCE,
    *,
    model_factory: Callable[[ModelRegistry, ModelRole], Model] | None = None,
) -> dict[str, Any]:
    if os.environ.get("SANAD_LIVE") != "1":
        raise RuntimeError("SANAD_LIVE=1 is required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or any(destination.parent.glob("live-09a-*b.json")):
        raise RuntimeError("09a attempt-2 allowance already recorded; do not rerun")
    report: dict[str, Any] = {
        "contract": "09a",
        "attempt": 2,
        "date": DATE,
        "run_count": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "region": "us-east-1",
        "model_id": ModelRegistry().worker,
        "prompt_version": PROMPT_VERSION,
        "sdk_version": "1.54.0",
        "state": "started",
        "spend_cap_usd": 0.20,
        "estimated_usd": 0,
        "checks": [],
        "calls": [],
        "price_source": "https://aws.amazon.com/bedrock/pricing/",
        "rates_per_million_usd": {"input": 0.06, "output": 0.24},
        "fixtures": "tests/scribe/dictations.py: MEASURED; exact three 08 measurement inputs",
        "redaction": "No transcripts, identities, credentials, provider bodies or exception text.",
    }
    # Create the allowance record before any network call. Failure never authorizes a retry.
    with destination.open("x") as output:
        json.dump(report, output, indent=2)
    spend = SpendGuard(cap=0.20)
    private_provider_logs()
    try:
        if model_factory is None:
            config = Config(connect_timeout=2, read_timeout=22, retries={"total_max_attempts": 1})
            client = ThreeRequests(
                boto3.client("bedrock-runtime", region_name="us-east-1", config=config), spend
            )

            def factory(registry: ModelRegistry, role: ModelRole) -> Model:
                model = BedrockModel(
                    model_id=registry.model_id(role),
                    region_name=registry.region,
                    temperature=0,
                    streaming=False,
                    max_tokens=2048,
                    boto_client_config=config,
                )
                model.client = client
                return model

            model_factory = factory
        for index, example in enumerate(MEASURED):
            agent = make_agent(
                "scribe",
                scope=scoped(),
                tools=(),
                system_prompt=SYSTEM_PROMPT,
                session_key=f"live-09a-{index}",
                model_factory=model_factory,
            )
            result = await propose(
                "scribe", DictationCandidate, example.input, agent=agent, want_spans=False
            )
            check: dict[str, Any] = {
                "fixture": index + 1,
                "candidate_validated": isinstance(result, Proposal),
                "passed": False,
            }
            if isinstance(result, Proposal):
                value = result.value
                actual = set(extracted_numbers(value))
                expected = set(numbers_in(example.input))
                issues = candidate_issues(value, example.input)
                check.update(
                    numbers_preserved=expected <= actual,
                    no_unsupported_numbers=not any(i.code == "unsupported_number" for i in issues),
                    orders_count_matches=len(value.orders) == len(example.candidate.orders),
                    expected_orders_count=len(example.candidate.orders),
                    actual_orders_count=len(value.orders),
                    model_patient_id_absent="patient_id" not in value.patient.model_dump(),
                    one_provider_call=len(result.metadata) == 1,
                    issue_codes=sorted({i.code for i in issues}),
                )
                check["passed"] = all(
                    v for k, v in check.items() if isinstance(v, bool) and k != "passed"
                )
            else:
                check["failure_reason"] = getattr(result, "reason", "typed_proposal_failure")
            report["checks"].append(check)
            report["calls"], report["estimated_usd"] = list(spend.calls), round(spend.estimated, 8)
            destination.write_text(json.dumps(report, indent=2) + "\n")
            print(f"dictation_{index + 1}", "PASS" if check["passed"] else "FAIL", flush=True)
    except Exception:
        report["runner_error"] = "live_check_exception_details_withheld"
    finally:
        report["calls"], report["estimated_usd"] = list(spend.calls), round(spend.estimated, 8)
        report["state"] = (
            "passed"
            if len(report["checks"]) == 3
            and all(c["passed"] for c in report["checks"])
            and "runner_error" not in report
            else "failed"
        )
        report["finished_at"] = datetime.now(UTC).isoformat()
        destination.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "state": report["state"],
                "provider_calls": len(spend.calls),
                "estimated_usd": report["estimated_usd"],
            }
        ),
        flush=True,
    )
    return report
