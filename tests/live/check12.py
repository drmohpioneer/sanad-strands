"""Exactly four real reader pairs; all patient/state/transport services are synthetic."""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from domain_fixtures import NOW
from harness import FakeClock
from store import evidence_fixtures as f

from sanad.contact.delivery import doctor_payload
from sanad.domain import EvidencePredicate, SendRecordsDetails, TestDetails
from sanad.evidence.doctor import decide
from sanad.media.vision import VISION_PROMPT_VERSION, VisionAdapter
from sanad.models.io import BedrockCaller, ModelCaller, private_provider_logs
from sanad.models.registry import ModelRegistry
from sanad.store.memory import MemoryStore
from sanad.store.records import OutboundIntent, Patient, canonical_json, from_record

from .check08 import RATES, SpendGuard
from .check09b import EightRequests

ROOT = Path(__file__).resolve().parents[2]
DATE = datetime.now(ZoneInfo("Africa/Cairo")).date().isoformat()
EVIDENCE = ROOT / "docs/evidence/live-12-2026-09-07b.json"
FIXTURES = (
    "lab_synthetic.png",
    "lab_synthetic_rotated_glare.png",
    "injection_synthetic.png",
    "rx_synthetic.png",
)


def run_check(destination: Path = EVIDENCE, *, caller: ModelCaller | None = None) -> dict[str, Any]:
    if os.environ.get("SANAD_LIVE") != "1":
        raise RuntimeError("SANAD_LIVE=1 is required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if caller is None and destination != EVIDENCE:
        raise RuntimeError("Only the released attempt-4 evidence destination is allowed")
    if destination.exists():
        raise RuntimeError("12 allowance already recorded; do not rerun")
    report: dict[str, Any] = {
        "contract": "12",
        "attempt": 4,
        "date": DATE,
        "run_count": 1,
        "state": "started",
        "started_at": datetime.now(UTC).isoformat(),
        "region": "us-east-1",
        "models": [ModelRegistry().vision, ModelRegistry().cross_check],
        "prompt_version": VISION_PROMPT_VERSION,
        "spend_cap_usd": 0.20,
        "request_limit": 8,
        "checks": [],
        "calls": [],
        "rates_per_million_usd": RATES,
        "scope": (
            "Real read_document through EvidenceTurn; synthetic bound patient, MemoryStore, "
            "private FakeS3, FakeTelegramFiles and captured transport. "
            "No AWS resource or Telegram API changes."
        ),
        "redaction": (
            "Synthetic values only; no printed identities, raw reader bodies, "
            "provider error text or credentials."
        ),
    }
    # Exclusive durable marker BEFORE credential resolution or any provider request.
    with destination.open("x") as stream:
        json.dump(report, stream, indent=2)
    scripted = caller is not None
    spend = SpendGuard(cap=0.20)
    client: EightRequests | None = None
    private_provider_logs()
    try:
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
            from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as policy

            caller = BedrockCaller(client, policy.policy_version)
        for index, filename in enumerate(FIXTURES):
            check: dict[str, Any] = {"fixture": filename, "passed": False}
            cost_before, calls_before = spend.estimated, len(spend.calls)
            try:
                clock = FakeClock(NOW)
                world = f.world(MemoryStore(clock=clock), clock)
                doctor = world.doctor
                world.seed(
                    doctor.model_copy(update={"language": "en", "version": doctor.version + 1})
                )
                patient = from_record(world.rows("patient")[0], Patient)
                # The fixture's printed synthetic name is fixed before any model call.
                world.seed(
                    patient.model_copy(
                        update={
                            "version": patient.version + 1,
                            "record_version": patient.version + 1,
                            "display_name": "Ahmed S.",
                            "language": "en",
                            "updated_at": clock(),
                        }
                    )
                )
                f.stopped_medication(world)
                if filename == "rx_synthetic.png":
                    f.mission(
                        world,
                        kind="SEND_RECORDS",
                        title="old prescription",
                        details=SendRecordsDetails(categories=("prescription",), required_count=1),
                        objective_predicate=EvidencePredicate(evaluator="send_records"),
                    )
                else:
                    f.mission(
                        world,
                        title="Potassium and Creatinine test",
                        details=TestDetails(
                            analytes=("Potassium", "Creatinine"), completeness="all"
                        ),
                    )
                _, _, _ = f.providers(world, data=(ROOT / "tests/data/09b" / filename).read_bytes())
                safety = world.runtime.safety_policy
                world.concierge.vision_factory = lambda source, safety=safety: VisionAdapter(
                    caller, source, safety
                )
                heads_before = canonical_json([r.body for r in world.rows("care_order_head")])
                check["turn_status"] = f.upload(
                    world, 1100, "old prescription" if filename == "rx_synthetic.png" else "lab"
                )
                if world.rows("evidence_head"):
                    e = f.current(world)
                    check.update(
                        category=e.category,
                        reader_categories=[r.document_type for r in e.readers],
                        rows=[
                            [row.item.model_dump(mode="json") for row in reader.items]
                            for reader in e.readers
                        ],
                        disagreements=[d.field for d in e.disagreements],
                        shift_guard=e.shift_guard_fired,
                        association=e.association_state,
                        match_provenance=e.patient_match_provenance,
                        flags=list(e.flags),
                        predicate_results=[
                            r.model_dump(mode="json") for r in e.required_predicate_results
                        ],
                        incidents=[
                            {
                                "severity": r.body["severity"],
                                "verified_status": r.body["verified_status"],
                            }
                            for r in world.rows("incident")
                        ],
                    )
                    check["fulfilled_before_confirmation"] = any(
                        r.body["state"] == "fulfilled" for r in world.rows("mission")
                    )
                    identity_cards = [
                        from_record(r, OutboundIntent)
                        for r in world.rows("outbound_intent")
                        if r.body.get("template_id") == "doctor_evidence_card"
                    ]
                    check["identity_review_cards"] = [
                        {
                            "text": payload["text"],
                            "buttons": [
                                b["text"]
                                for row in payload["reply_markup"]["inline_keyboard"]
                                for b in row
                            ],
                        }
                        for c in identity_cards
                        if c.payload
                        for payload in [json.loads(json.dumps(c.payload))]
                    ]
                    check["identity_review_has_two_choices"] = any(
                        c["buttons"] == ["This patient's paper ✅", "Not this patient ❌"]
                        and "The name on this paper could not be read. Is this Ahmed S.'s paper?"
                        in c["text"]
                        for c in check["identity_review_cards"]
                    )
                    if (
                        filename in {"lab_synthetic.png", "rx_synthetic.png"}
                        and e.association_state == "accepted_pending_identity"
                    ):
                        outcome = decide(
                            world.runtime.steward,
                            world.owner,
                            e,
                            "confirm_identity",
                            "live-confirm:" + e.evidence_id,
                            mission_id=e.mission_id,
                        )
                        check["confirm_status"] = outcome.status
                        check["association_after_confirmation"] = f.current(world).association_state
                else:
                    check.update(
                        category=None,
                        rows=[],
                        disagreements=[],
                        shift_guard=None,
                        association=None,
                        predicate_results=[],
                        incidents=[],
                        media_failure=f.work(world).last_error,
                    )
                fulfilled = any(r.body["state"] == "fulfilled" for r in world.rows("mission"))
                review = any(r.body["review_kind"] == "result_review" for r in world.rows("review"))
                unchanged = (
                    canonical_json([r.body for r in world.rows("care_order_head")]) == heads_before
                )
                check.update(
                    fulfilled=fulfilled, result_review=review, order_heads_byte_identical=unchanged
                )
                check["doctor_cards"] = []
                for r in world.rows("outbound_intent"):
                    intent = from_record(r, OutboundIntent)
                    if intent.audience != "doctor":
                        continue
                    if intent.template_id == "doctor_evidence_card":
                        payload = intent.payload
                    elif intent.notification_purpose == "DANGER":
                        payload = world.runtime.patient_payload(intent)
                    elif intent.notification_purpose == "DONE:FULFILLMENT":
                        payload = doctor_payload(world.store, intent)
                    else:
                        continue
                    if payload:
                        check["doctor_cards"].append(
                            {
                                "template_id": intent.template_id,
                                "purpose": intent.notification_purpose,
                                "text": payload["text"],
                            }
                        )
                check["doctor_cards_english"] = not re.search(
                    r"[\u0600-\u06ff]",
                    json.dumps(
                        [check["doctor_cards"], check.get("identity_review_cards", [])],
                        ensure_ascii=False,
                    ),
                )
                confirm_path = (
                    "identity_unverifiable" in check.get("flags", [])
                    and check.get("association") == "accepted_pending_identity"
                    and not check.get("fulfilled_before_confirmation", True)
                    and any(p["satisfied"] for p in check.get("predicate_results", []))
                    and check.get("identity_review_has_two_choices")
                    and check.get("confirm_status") == "accepted"
                    and check.get("association_after_confirmation") == "accepted"
                )
                if filename == "lab_synthetic.png":
                    danger_cards = [c for c in check["doctor_cards"] if c["purpose"] == "DANGER"]
                    check["passed"] = bool(
                        confirm_path
                        and fulfilled
                        and review
                        and check.get("category") == "lab_result"
                        and len(check["incidents"]) == 1
                        and len(danger_cards) == 1
                        and "The requested evidence was received" in danger_cards[0]["text"]
                        and not any(
                            c["purpose"] == "DONE:FULFILLMENT" for c in check["doctor_cards"]
                        )
                        and not any(d.endswith(".unit") for d in check["disagreements"])
                    )
                elif filename == "lab_synthetic_rotated_glare.png":
                    check["passed"] = not fulfilled and bool(
                        check.get("media_failure")
                        or check.get("disagreements")
                        or check.get("shift_guard")
                        or any(not p["satisfied"] for p in check.get("predicate_results", []))
                    )
                elif filename == "injection_synthetic.png":
                    check["passed"] = not fulfilled and check.get("association") != "accepted"
                else:
                    check["passed"] = (
                        bool(confirm_path)
                        and fulfilled
                        and review
                        and unchanged
                        and check.get("category") == "prescription"
                    )
                check["passed"] = check["passed"] and bool(check["doctor_cards_english"])
            except Exception as error:
                if scripted:
                    raise
                check["error_type"] = type(error).__name__
                check["error"] = "fixture_exception_details_withheld"
            check["estimated_usd"] = round(spend.estimated - cost_before, 8)
            check["calls"] = list(spend.calls[calls_before:])
            report["checks"].append(check)
            report["calls"] = list(spend.calls)
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(f"evidence_{index + 1}", "PASS" if check["passed"] else "FAIL", flush=True)
    except Exception:
        if scripted:
            raise
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
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps({k: report[k] for k in ("state", "provider_requests", "estimated_usd")}),
        flush=True,
    )
    return report
