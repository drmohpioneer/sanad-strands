"""One bounded text-only natural-verb measurement; persist typed scores only.

Architect invocation (the repository's --live isolation guard still applies):
SANAD_LIVE=1 uv run pytest -o 'python_files=test_*.py check11N.py'
    tests/live --live --force-enable-socket -k contract11N_live
"""

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from scribe.test_natural_verbs_11N import NEGATIVES, PHRASES, seeded_world
from store.account_fixtures import APPLICANT, update
from strands.models import BedrockModel, Model

from sanad.models.io import private_provider_logs
from sanad.models.registry import ModelRegistry, ModelRole
from sanad.scribe.extract import PROMPT_VERSION
from sanad.scribe.grounding import Claim, valid_record
from sanad.scribe.resolver import resolve_name

from .check08 import BudgetClient, SpendGuard

EVIDENCE = Path(__file__).resolve().parents[2] / (
    "docs/evidence/live-11N-" + datetime.now(UTC).date().isoformat() + ".json"
)


class BoundedRequests(BudgetClient):
    def converse(self, **kwargs: Any) -> dict[str, Any]:
        if len(json.dumps(kwargs).encode()) > 50000:
            raise RuntimeError("input_size_limit")
        return super().converse(**kwargs)


def run_check(
    destination: Path = EVIDENCE,
    *,
    model_factory: Callable[[ModelRegistry, ModelRole], Model] | None = None,
) -> dict[str, Any]:
    if os.environ.get("SANAD_LIVE") != "1":
        raise RuntimeError("SANAD_LIVE=1 is required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or any(destination.parent.glob("live-11N-*.json")):
        raise RuntimeError("11N allowance already recorded; do not rerun")
    report: dict[str, Any] = {
        "contract": "11N",
        "state": "started",
        "prompt_version": PROMPT_VERSION,
        "spend_cap_usd": 0.30,
        "started_at": datetime.now(UTC).isoformat(),
        "runs": [],
        "calls": [],
        "estimated_usd": 0,
    }
    with destination.open("x") as stream:
        json.dump(report, stream, indent=2)
    spend = SpendGuard(cap=0.30)
    private_provider_logs()
    try:
        if model_factory is None:
            config = Config(connect_timeout=2, read_timeout=22, retries={"total_max_attempts": 1})
            client = BoundedRequests(
                boto3.client("bedrock-runtime", region_name=ModelRegistry().region, config=config),
                spend,
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
        for index, row in enumerate((*PHRASES, *NEGATIVES)):
            positive = index < len(PHRASES)
            world = seeded_world()
            try:
                # Seed only is scripted. The measured text uses the supplied factory,
                # real receipt/router/turn, two readers and any normal retries.
                world.scribe.model_factory = model_factory
                result = world.post(update(APPLICANT, row[0], 10))
                if result.status_code != 200 or world.receipt(10).state != "completed":
                    raise RuntimeError("incomplete_turn")
                p = world.scribe.repo.pending(world.doctor.scope)
                if p and p.choices:
                    world.tap("Synthetic Person", id=11)
                    p = world.proposal
                orders = p.candidate.orders if p else ()
                matches = [
                    (i, o)
                    for i, o in enumerate(orders)
                    if (resolve_name(o.drug, "drug", row[0]).latin or o.drug).casefold()
                    == row[1].casefold()
                ]
                verified = False
                citation_present = False
                recorded_action = None
                if p and len(matches) == 1:
                    i, order = matches[0]
                    recorded_action = order.action
                    citation_present = order.action_quote is not None
                    verified = citation_present and any(
                        e.item == f"order:{i}"
                        and e.field == "action"
                        and e.transformation == "instruction_clause"
                        and valid_record(e, Claim(f"order:{i}", "action", order.action), p)
                        for e in p.evidence
                    )
                confirmable = [
                    (i, o) for i, o in enumerate(orders) if p and not p.blocked(f"order:{i}")
                ]
                wrong = (
                    any(o.action != row[2] or (i, o) not in matches for i, o in enumerate(orders))
                    if positive
                    else bool(confirmable)
                )
                passed = (
                    len(orders) == len(matches) == len(confirmable) == 1
                    and recorded_action == row[2]
                    and verified
                    if positive
                    else not confirmable
                )
                report["runs"].append(
                    {
                        "phrase": index + 1,
                        "intended_action": row[2] if positive else None,
                        "recorded_action": recorded_action,
                        "issue_codes": sorted({issue.code for issue in p.issues}) if p else [],
                        "citation_present": citation_present,
                        "citation_verified": verified,
                        "passed": passed,
                        "wrong_action": wrong,
                    }
                )
            finally:
                if world.scribe.rxnorm_client:
                    world.scribe.rxnorm_client.close()
            report["calls"], report["estimated_usd"] = list(spend.calls), round(spend.estimated, 8)
            destination.write_text(json.dumps(report, indent=2) + "\n")
    except Exception:
        report["runner_error"] = "live_check_exception_details_withheld"
    finally:
        report["calls"], report["estimated_usd"] = list(spend.calls), round(spend.estimated, 8)
        report["passed_count"] = sum(r["passed"] for r in report["runs"])
        report["wrong_action_count"] = sum(r["wrong_action"] for r in report["runs"])
        report["positive_citation_count"] = sum(r["citation_present"] for r in report["runs"][:10])
        report["state"] = (
            "passed"
            if (
                len(report["runs"]) == 13
                and report["passed_count"] >= 12
                and report["wrong_action_count"] == 0
                and report["positive_citation_count"] >= 9
                and "runner_error" not in report
            )
            else "failed"
        )
        report["finished_at"] = datetime.now(UTC).isoformat()
        destination.write_text(json.dumps(report, indent=2) + "\n")
    return report
