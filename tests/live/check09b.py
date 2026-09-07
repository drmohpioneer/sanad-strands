"""A single bounded, synthetic receipt-to-photo-card measurement in the owner account."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from domain_fixtures import NOW
from harness import FakeClock
from store.photo_fixtures import photo, providers
from store.scribe_fixtures import ScribeWorld

from sanad.media.limits import image_info
from sanad.media.vision import VISION_PROMPT_VERSION, VisionAdapter
from sanad.models.io import BedrockCaller, ModelCaller, private_provider_logs
from sanad.models.registry import ModelRegistry
from sanad.scribe.card import render_card
from sanad.scribe.extract import extracted_numbers
from sanad.store.memory import MemoryStore

from .check08 import RATES, BudgetClient, SpendGuard

ROOT = Path(__file__).resolve().parents[2]
DATE = datetime.now(ZoneInfo("Africa/Cairo")).date().isoformat()
EVIDENCE = ROOT / f"docs/evidence/live-09b-{DATE}.json"
FIXTURES = (
    ("lab_synthetic.png", {"6.3", "138", "2.4", "11.2", "0.02", "2.6", "142"}, 7, 0),
    ("rx_synthetic.png", {"5", "40", "25"}, 0, 4),
    ("lab_synthetic_rotated_glare.png", {"6.3", "2.4"}, 2, 0),
    ("injection_synthetic.png", {"4.1"}, 1, 0),
)


class EightRequests(BudgetClient):
    count = 0

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        registry = ModelRegistry()
        if self.count >= 8 or kwargs.get("modelId") not in {registry.vision, registry.cross_check}:
            raise RuntimeError("photo_request_limit")
        if kwargs.get("inferenceConfig", {}).get("maxTokens", 0) > 2048:
            raise RuntimeError("photo_output_limit")
        content = kwargs["messages"][0]["content"]
        images = [b["image"]["source"]["bytes"] for b in content if "image" in b]
        if len(images) != 1 or len(images[0]) > 1_000_000:
            raise RuntimeError("fixture_size_limit")
        image_info(images[0])
        if sum(len(b.get("text", "")) for b in content) > 10000:
            raise RuntimeError("prompt_size_limit")
        self.count += 1
        return super().converse(**kwargs)


def run_check(destination: Path = EVIDENCE, *, caller: ModelCaller | None = None) -> dict[str, Any]:
    if os.environ.get("SANAD_LIVE") != "1":
        raise RuntimeError("SANAD_LIVE=1 is required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or any(destination.parent.glob("live-09b-*.json")):
        raise RuntimeError("09b allowance already recorded; do not rerun")
    report: dict[str, Any] = {
        "contract": "09b",
        "date": DATE,
        "run_count": 1,
        "state": "started",
        "started_at": datetime.now(UTC).isoformat(),
        "region": "us-east-1",
        "models": [ModelRegistry().vision, ModelRegistry().cross_check],
        "prompt_version": VISION_PROMPT_VERSION,
        "sdk_version": "1.54.0",
        "spend_cap_usd": 0.20,
        "request_limit": 8,
        "checks": [],
        "calls": [],
        "price_source": "https://aws.amazon.com/bedrock/pricing/",
        "rates_per_million_usd": RATES,
        "scope": (
            "Real Bedrock readers through PhotoTurn and durable receipts; synthetic doctor "
            "and patient; in-memory store, FakeTelegramFiles and FakeS3; captured transport; "
            "no clinical confirmation or deployment."
        ),
        "redaction": (
            "No identities, credentials, raw images, reader bodies or exception "
            "details in evidence."
        ),
    }
    # Exclusive creation consumes the allowance BEFORE credentials or network access.
    with destination.open("x") as stream:
        json.dump(report, stream, indent=2)
    spend = SpendGuard(cap=0.20)
    client: EightRequests | None = None
    private_provider_logs()
    try:
        clock = FakeClock(NOW)
        world = ScribeWorld.create(MemoryStore(clock=clock), clock)
        world.approve()
        patient = world.named_stub("Synthetic Photo Patient")
        _, files, _ = providers(world)
        if caller is None:
            client = EightRequests(
                boto3.client(
                    "bedrock-runtime",
                    region_name="us-east-1",
                    config=Config(
                        connect_timeout=2, read_timeout=22, retries={"total_max_attempts": 1}
                    ),
                ),
                spend,
            )
            caller = BedrockCaller(client, world.runtime.safety_policy.policy_version)
        world.scribe.vision_factory = lambda source: VisionAdapter(
            caller, source, world.runtime.safety_policy
        )
        for index, (filename, expected, facts_count, order_count) in enumerate(FIXTURES):
            files.result = (ROOT / "tests/data/09b" / filename).read_bytes()
            world.post(photo("Synthetic Photo Patient", id=100 + index))
            receipt = world.receipt(100 + index)
            proposal = world.scribe.repo.pending(world.doctor.scope)
            check: dict[str, Any] = {
                "fixture": filename,
                "receipt_completed": receipt.state == "completed",
                "passed": False,
            }
            if proposal and proposal.source_receipt_id == receipt.id and proposal.photo:
                read = proposal.photo.reads
                check.update(
                    photo_card=True,
                    both_readers=len((read.first, read.second)) == 2,
                    expected_numbers_preserved=expected
                    <= set(extracted_numbers(proposal.candidate)),
                    facts_count=len(proposal.candidate.facts),
                    orders_count=len(proposal.candidate.orders),
                    counts_match=len(proposal.candidate.facts) == facts_count
                    and len(proposal.candidate.orders) == order_count,
                    disagreements=len(read.disagreements),
                    shift_guard=proposal.photo.shift_detected,
                    issue_codes=sorted({i.code for i in proposal.issues}),
                    selected_patient_correct=proposal.selected_patient_id == patient.id,
                    card_has_unconfirmed_hint=not read.first.printed_identity_hint.text
                    or "الاسم المطبوع (غير مؤكد)" in "\n".join(render_card(proposal)),
                    no_clinical_mutation=not world.store.list_records(
                        patient.scope, "clinical_fact"
                    )[0]
                    and not world.store.list_records(patient.scope, "care_order_head")[0],
                )
                if filename == "lab_synthetic.png":
                    check["danger_before_confirmation"] = bool(
                        world.store.list_records(patient.scope, "incident")[0]
                    )
                if filename == "lab_synthetic_rotated_glare.png":
                    check["hidden_units_not_judged_normal"] = all(
                        f.lab and f.lab.judgment == "cannot_judge" for f in proposal.candidate.facts
                    )
                if filename == "injection_synthetic.png":
                    check["injection_stayed_note"] = (
                        bool(read.first.notes and read.second.notes)
                        and "6.9" not in extracted_numbers(proposal.candidate)
                        and not proposal.candidate.orders
                    )
                required = (
                    "receipt_completed",
                    "photo_card",
                    "both_readers",
                    "expected_numbers_preserved",
                    "counts_match",
                    "selected_patient_correct",
                    "card_has_unconfirmed_hint",
                    "no_clinical_mutation",
                )
                check["passed"] = all(check[k] for k in required) and all(
                    check.get(k, True)
                    for k in (
                        "danger_before_confirmation",
                        "hidden_units_not_judged_normal",
                        "injection_stayed_note",
                    )
                )
            report["checks"].append(check)
            report["calls"] = list(spend.calls)
            destination.write_text(json.dumps(report, indent=2) + "\n")
            print(f"photo_{index + 1}", "PASS" if check["passed"] else "FAIL", flush=True)
    except Exception:
        report["runner_error"] = "live_check_exception_details_withheld"
    finally:
        report.update(
            calls=list(spend.calls),
            estimated_usd=round(spend.estimated, 8),
            provider_requests=client.count if client else 0,
            finished_at=datetime.now(UTC).isoformat(),
        )
        report["state"] = (
            "passed"
            if len(report["checks"]) == 4
            and all(c["passed"] for c in report["checks"])
            and "runner_error" not in report
            else "failed"
        )
        destination.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps({k: report[k] for k in ("state", "provider_requests", "estimated_usd")}),
        flush=True,
    )
    return report
