"""One allowance, exactly five bounded Nova Lite patient turns, synthetic data only."""

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from domain_fixtures import NOW
from harness import FakeClock
from store.account_fixtures import PATIENT, update
from store.concierge_fixtures import PatientWorld
from strands.models import BedrockModel, Model

from sanad.concierge.answer import PROMPT_VERSION
from sanad.models.io import private_provider_logs
from sanad.models.registry import ModelRegistry, ModelRole
from sanad.store.memory import MemoryStore

from .check08 import RATES, BudgetClient, SpendGuard

ROOT = Path(__file__).resolve().parents[2]
DATE = datetime.now(ZoneInfo("Africa/Cairo")).date().isoformat()
EVIDENCE = ROOT / "docs/evidence/live-10-2026-09-07b.json"
MESSAGES = (
    "هو الدكتور قال 40 ولا 20؟",
    "يعني إيه ارتفاع ضغط الدم؟",
    "ليه أقيس الضغط في البيت؟",
    "يعني إيه LDL؟",
    "إيه دعوة QR؟",
)


class FiveRequests(BudgetClient):
    count = 0

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        if self.count >= 5 or kwargs.get("modelId") != ModelRegistry().worker:
            raise RuntimeError("five_worker_request_limit")
        if not 0 < kwargs.get("inferenceConfig", {}).get("maxTokens", 0) <= 2048:
            raise RuntimeError("output_limit")
        if len(json.dumps(kwargs).encode()) > 50000:
            raise RuntimeError("input_size_limit")
        self.count += 1
        return super().converse(**kwargs)


def run_check(
    destination: Path = EVIDENCE,
    *,
    model_factory: Callable[[ModelRegistry, ModelRole], Model] | None = None,
) -> dict[str, Any]:
    if os.environ.get("SANAD_LIVE") != "1":
        raise RuntimeError("SANAD_LIVE=1 is required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or any(destination.parent.glob("live-10-*b.json")):
        raise RuntimeError("10 attempt-2 allowance already recorded; do not rerun")
    report: dict[str, Any] = {
        "contract": "10",
        "attempt": 2,
        "date": DATE,
        "run_count": 1,
        "state": "started",
        "started_at": datetime.now(UTC).isoformat(),
        "region": "us-east-1",
        "model_id": ModelRegistry().worker,
        "sdk_version": "1.54.0",
        "prompt_version": PROMPT_VERSION,
        "spend_cap_usd": 0.20,
        "request_limit": 5,
        "checks": [],
        "calls": [],
        "estimated_usd": 0,
        "source_review": "pending owner review; synthetic mode only",
        "fixtures": "tests/live/check10.py: MESSAGES, bound synthetic PatientWorld",
        "price_source": "https://aws.amazon.com/bedrock/pricing/",
        "rates_per_million_usd": {
            "input": RATES[ModelRegistry().worker][0],
            "output": RATES[ModelRegistry().worker][1],
        },
        "redaction": "No credentials, identifiers, transcripts, provider bodies or exception text.",
    }
    # Reserve the single allowance BEFORE client creation or network activity.
    with destination.open("x") as stream:
        json.dump(report, stream, indent=2)
    spend = SpendGuard(cap=0.20)
    private_provider_logs()
    try:
        if model_factory is None:
            config = Config(connect_timeout=2, read_timeout=22, retries={"total_max_attempts": 1})
            client = FiveRequests(
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
        clock = FakeClock(NOW)
        world = cast(PatientWorld, PatientWorld.create(MemoryStore(clock=clock), clock))
        world.enroll()
        world.concierge.model_factory = model_factory
        for index, text in enumerate(MESSAGES):
            before = len(spend.calls)
            counters_before = dict(world.runtime.counters)
            response = world.post(update(PATIENT, text, 6000 + index))
            receipt = world.receipt(6000 + index)
            intents = [
                i
                for i in world.patient_intents()
                if "patient-turn:" + receipt.id in i.source_event_ids
            ]
            intent = intents[0] if len(intents) == 1 else None
            check: dict[str, Any] = {
                "fixture": index + 1,
                "http_status": response.status_code,
                "receipt_completed": receipt.state == "completed",
                "one_reply": len(intents) == 1,
                "answer_passed_gates": intent is not None
                and intent.template_id == "patient_answer",
                "template_id": intent.template_id if intent else None,
                "provider_calls": len(spend.calls) - before,
                "failure_codes": sorted(
                    k
                    for k, count in world.runtime.counters.items()
                    if k.startswith("concierge_") and count > counters_before.get(k, 0)
                ),
            }
            check["passed"] = response.status_code == 200 and all(
                check[k] for k in ("receipt_completed", "one_reply", "answer_passed_gates")
            )
            report["checks"].append(check)
            report["calls"], report["estimated_usd"] = list(spend.calls), round(spend.estimated, 8)
            destination.write_text(json.dumps(report, indent=2) + "\n")
            print(f"patient_{index + 1}", "PASS" if check["passed"] else "FAIL", flush=True)
    except Exception:
        report["runner_error"] = "live_check_exception_details_withheld"
    finally:
        report["calls"], report["estimated_usd"] = list(spend.calls), round(spend.estimated, 8)
        report["state"] = (
            "passed"
            if len(report["checks"]) == 5
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
