"""Once-only, capped readers through the real photo pipeline; no live care services."""

import asyncio
import json
import os
import re
import tempfile
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from domain_fixtures import NOW
from harness import FakeClock
from PIL import Image, ImageOps
from providers.fixtures import SOURCE
from providers.photo11d_fixtures import CONTROL_PHRASES, control_image
from store.photo_fixtures import photo, providers
from store.scribe_fixtures import ScribeWorld

from sanad.agents.hygiene import clean_values, json_object
from sanad.media.agreement import normalized_name as normalized_name
from sanad.media.agreement import within_two_edits as within_two_edits
from sanad.media.images import normalize_document
from sanad.media.limits import image_info
from sanad.media.vision import VISION_PROMPT_VERSION, DocumentRead, VisionAdapter
from sanad.models.io import (
    BedrockCaller,
    ModelCaller,
    ModelReply,
    ModelUnavailable,
    private_provider_logs,
)
from sanad.models.registry import ModelRegistry
from sanad.store.memory import MemoryStore
from sanad.store.records import IntakeDraft, from_record

from .check08 import RATES, SpendGuard

ROOT = Path(__file__).resolve().parents[2]
DATE = datetime.now(ZoneInfo("Africa/Cairo")).date().isoformat()
EVIDENCE = ROOT / "docs/evidence/live-11d-2026-09-07b.json"
GROUND_TRUTH = ROOT / "lane/spikes/ground-truth-11d.json"
INPUTS = ("handwritten_note", "opd_form")


class BoundedClient:
    def __init__(self, client: Any, spend: SpendGuard):
        self.client, self.spend = client, spend
        self.count = 0
        self.lock = threading.Lock()

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        registry = ModelRegistry()
        model = kwargs.get("modelId")
        if model not in {registry.vision, registry.cross_check}:
            raise ValueError("reader_not_permitted")
        content = kwargs["messages"][0]["content"]
        images = [c["image"]["source"]["bytes"] for c in content if "image" in c]
        if len(images) != 1 or image_info(images[0]).format != "jpeg":
            raise ValueError("normalized_image_required")
        output_limit = kwargs.get("inferenceConfig", {}).get("maxTokens", 0)
        if not 0 < output_limit <= 2048 or sum(len(c.get("text", "")) for c in content) > 10000:
            raise ValueError("request_bounds")
        with self.lock:
            if self.count >= 12:
                raise ValueError("request_limit")
            reserved = self.spend.reserve(model, output_limit)
            self.count += 1
        started = monotonic()
        response: dict[str, Any] = {}
        try:
            response = self.client.converse(**kwargs)
            return response
        finally:
            self.spend.finish(
                model, reserved, response.get("usage", {}), (monotonic() - started) * 1000
            )


class ObservedCaller:
    """Raw fields never leave private local storage; control scoring precedes the rail."""

    def __init__(self, caller: ModelCaller):
        self.caller = caller
        self.replies: list[tuple[str, dict[str, Any]]] = []
        self.image_inputs: list[tuple[int, int, int]] = []

    async def call(
        self, model_id: str, content: list[dict[str, Any]], *, max_tokens: int = 2048
    ) -> ModelReply | ModelUnavailable:
        for block in content:
            if "image" in block:
                data = block["image"]["source"]["bytes"]
                info = image_info(data)
                self.image_inputs.append((info.width, info.height, len(data)))
        result = await self.caller.call(model_id, content, max_tokens=max_tokens)
        if isinstance(result, ModelReply):
            try:
                self.replies.append((model_id, clean_values(json_object(result.text))))
            except (ValueError, TypeError):
                pass
        return result


def telegram_copy(data: bytes) -> bytes:
    with Image.open(BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        size = tuple(round(d * 1280 / max(image.size)) for d in image.size)
        image = image.resize(size, Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="JPEG", quality=70)
        return output.getvalue()


def oracle_rows(oracle: dict[str, Any], family: str) -> list[list[str]]:
    rows = oracle.get(family, [])
    if family == "medications" and "architect_firm_rows" in oracle:
        firm, uncertain = oracle["architect_firm_rows"], oracle["architect_uncertain_rows"]
        if (
            not isinstance(firm, dict)
            or not isinstance(uncertain, dict)
            or firm.keys() & uncertain.keys()
        ):
            raise ValueError("invalid_ground_truth")
        rows = list((firm | uncertain).values())
        if len(rows) != oracle["rows_present"]:
            raise ValueError("incomplete_ground_truth")
    if not isinstance(rows, list):
        raise ValueError("invalid_ground_truth")
    result = []
    for row in rows:
        alternatives = [row] if isinstance(row, str) else row
        if (
            not isinstance(alternatives, list)
            or not alternatives
            or any(not isinstance(n, str) or not normalized_name(n) for n in alternatives)
        ):
            raise ValueError("invalid_ground_truth")
        if family == "medications":
            # The private reference contains whole rows: name, then numeric
            # strength/regimen, with occasional parenthetical adjudicator notes.
            # Compare the name field to that name prefix, never to the dose.
            # An unnamed ('??') row stays present but has no scorable name.
            alternatives = [re.split(r"\s+(?=\d)|\s*\(", n, maxsplit=1)[0] for n in alternatives]
        result.append([value for n in alternatives if (value := normalized_name(n))])
    return result


def recovered_rows(names: set[str], rows: list[list[str]]) -> int:
    # One returned name cannot recover several similarly spelled reference rows.
    assigned: dict[str, int] = {}

    def match(index: int, seen: set[str]) -> bool:
        for name in sorted(names):
            if name in seen or not any(within_two_edits(name, n) for n in rows[index]):
                continue
            seen.add(name)
            if name not in assigned or match(assigned[name], seen):
                assigned[name] = index
                return True
        return False

    return sum(match(i, set()) for i in range(len(rows)))


def score_names(
    read: DocumentRead, oracle: dict[str, Any] | None, *, card_text: str = ""
) -> dict[str, Any]:
    return score_name_fields(
        [[row.item.name for row in reader.items] for reader in read.readers],
        oracle,
        card_text=card_text,
    )


def score_name_fields(
    reader_names: Sequence[Sequence[str | None]],
    oracle: dict[str, Any] | None,
    *,
    card_text: str = "",
) -> dict[str, Any]:
    # These are measurements of raw reader names, before any name resolver.
    names = {
        value
        for reader in reader_names
        for name in reader
        if name and (value := normalized_name(name))
    }
    result: dict[str, Any] = {
        "reader_row_counts": [len(r) for r in reader_names],
        "unique_names_returned": len(names),
        "medication_rows_recovered": None,
        "medication_reference_rows_scorable": None,
        "lab_names_recovered": None,
        "invented_items": None,
        "invented_items_reaching_card": None if card_text else 0,
        "ground_truth_available": oracle is not None,
    }
    if oracle is None:
        return result
    medications, labs = oracle_rows(oracle, "medications"), oracle_rows(oracle, "labs")
    expected = [
        *medications,
        *labs,
        *oracle_rows(oracle, "followup"),
        *oracle_rows(oracle, "other_items"),
    ]
    allowed = {n for alternatives in expected for n in alternatives}
    invented = {n for n in names if not any(within_two_edits(n, row) for row in allowed)}
    shown = " " + normalized_name(card_text) + " "
    result.update(
        medication_rows_recovered=recovered_rows(names, medications),
        medication_reference_rows_scorable=sum(bool(row) for row in medications),
        lab_names_recovered=recovered_rows(names, labs),
        invented_items=len(invented),
        invented_items_reaching_card=sum(" " + n + " " in shown for n in invented),
    )
    return result


def run_check(
    destination: Path = EVIDENCE,
    *,
    caller: ModelCaller | None = None,
    fixtures: dict[str, bytes] | None = None,
    oracle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if os.environ.get("SANAD_LIVE") != "1":
        raise RuntimeError("SANAD_LIVE=1 is required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError("11d attempt-3 allowance already recorded; do not rerun")
    report: dict[str, Any] = {
        "contract": "11d",
        "attempt": 3,
        "date": DATE,
        "run_count": 1,
        "state": "started",
        "started_at": datetime.now(UTC).isoformat(),
        "spend_cap_usd": 0.20,
        "request_limit": 12,
        "prompt_version": VISION_PROMPT_VERSION,
        "normalization_version": "document-image-v1",
        "scoring": (
            "Unique pre-resolution names; reference medication name prefixes exclude "
            "numeric strengths/regimens and parenthetical notes; normalized edit distance <= 2; "
            "card exposure scored separately"
        ),
        "models": [ModelRegistry().vision, ModelRegistry().cross_check],
        "scope": (
            "Real Bedrock only; local receipts, private media and captured delivery; "
            "no AWS storage or Telegram."
        ),
        "redaction": (
            "Counts, template and model IDs only. "
            "Raw Latin reader names are private local artifacts."
        ),
        "rates_per_million_usd": RATES,
        "papers": [],
        "control": {},
    }
    with destination.open("x") as stream:
        json.dump(report, stream, indent=2)
    spend = SpendGuard(cap=0.20)
    client: BoundedClient | None = None
    # The local private artifact is outside every Git checkout and never printed.
    private_root = Path(tempfile.mkdtemp(prefix="sanad-11d-private-"))
    private_path = private_root / "latin-readings.json"
    report["private_readings_path"] = str(private_path)
    private: dict[str, Any] = {}
    private_provider_logs()
    try:
        if oracle is None and GROUND_TRUTH.is_file():
            oracle = json.loads(GROUND_TRUTH.read_text())
        # Validate the private reference before any provider call, without
        # copying patient names, reference rows or annotations into evidence.
        for paper_oracle in (oracle or {}).values():
            if isinstance(paper_oracle, dict):
                for family in ("medications", "labs", "followup", "other_items"):
                    oracle_rows(paper_oracle, family)
        clock = FakeClock(NOW)
        world = ScribeWorld.create(MemoryStore(clock=clock), clock)
        world.approve()
        world.named_stub("Synthetic Photo Patient")
        _, files, _ = providers(world)
        if caller is None:
            client = BoundedClient(
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
        observed = ObservedCaller(caller)
        world.scribe.vision_factory = lambda source: VisionAdapter(
            observed, source, world.runtime.safety_policy
        )
        if fixtures is None:
            fixtures = {
                "handwritten_note": telegram_copy(
                    (ROOT / "lane/spikes/real_handwritten_rx.jpg").read_bytes()
                ),
                "opd_form": (ROOT / "lane/spikes/real_opd_form_screenshot.png").read_bytes(),
            }
        for i, input_id in enumerate(INPUTS):
            files.result = fixtures[input_id]
            before = spend.estimated
            observed.replies.clear()
            observed.image_inputs.clear()
            started = monotonic()
            world.post(photo("Synthetic Photo Patient", id=100 + i))
            receipt = world.receipt(100 + i)
            drafts = world.store.list_records(world.doctor.scope, "intake_draft")[0]
            draft = next(
                (
                    candidate
                    for r in drafts
                    if receipt.id in (candidate := from_record(r, IntakeDraft)).source_receipt_ids
                ),
                None,
            )
            proposal = world.scribe.repo.pending(world.doctor.scope)
            card = bool(proposal and proposal.source_receipt_id == receipt.id and proposal.photo)
            card_intents = [
                intent
                for intent in world.cards()
                if card and proposal and any(v.id == proposal.id for v in intent.source_versions)
            ]
            card_text = "\n".join(
                str((intent.payload or {}).get("text", "")) for intent in card_intents
            )
            from sanad.store import keys
            from sanad.store.keys import IntakeScope
            from sanad.store.records import OutboundIntent

            failures = [
                i
                for i in world.cards()
                if receipt.id in i.source_event_ids and i.template_id == "doctor_photo_unreadable"
            ] + [
                from_record(r, OutboundIntent)
                for r in world.store.list_records(
                    IntakeScope(doctor_id=world.doctor.id, intake_id=keys.digest(receipt.id)),
                    "outbound_intent",
                )[0]
                if r.body.get("template_id") == "doctor_photo_unreadable"
            ]
            honest_card = bool(failures) and not card
            check: dict[str, Any] = {
                "input_id": input_id,
                "medication_rows_present": 3 if i == 0 else 11,
                "lab_names_present": 5 if i == 0 else 0,
                "receipt_completed": receipt.state == "completed",
                "card": card,
                "honest_card": honest_card,
                "template_id": "scribe_card"
                if card
                else "doctor_photo_unreadable"
                if honest_card
                else None,
                "proposed_orders": len(proposal.candidate.orders) if card and proposal else 0,
                "proposed_facts": len(proposal.candidate.facts) if card and proposal else 0,
                "normalized_inputs": [
                    {"width": w, "height": h, "bytes": size}
                    for w, h, size in sorted(set(observed.image_inputs))
                ],
                "latency_ms": round((monotonic() - started) * 1000, 2),
                "estimated_usd": round(spend.estimated - before, 8),
                "medication_rows_recovered": None,
                "lab_names_recovered": None,
                "invented_items": None,
                "invented_items_reaching_card": 0 if honest_card else None,
                "arabic_fields_dropped": None,
                "single_reader_degradations": None,
            }
            if draft:
                read = draft.reads
                check.update(score_names(read, (oracle or {}).get(input_id), card_text=card_text))
                check.update(
                    arabic_fields_dropped=sum(len(r.dropped_fields) for r in read.readers),
                    single_reader_degradations=int(read.single_reader),
                    column_photo=read.instruction_crop is not None,
                )
                private[input_id] = {
                    "names_before_resolution": [
                        [row.item.name for row in r.items] for r in read.readers
                    ],
                }
            check["passed"] = bool(
                check["receipt_completed"]
                and (card or honest_card)
                and check["invented_items_reaching_card"] == 0
            )
            report["papers"].append(check)
            destination.write_text(json.dumps(report, indent=2) + "\n")
        observed.replies.clear()
        started = monotonic()
        before = spend.estimated
        control = asyncio.run(
            VisionAdapter(observed, SOURCE, world.runtime.safety_policy).read_document(
                normalize_document(control_image()), "jpeg", kind_hint="other"
            )
        )
        scores = {}
        for model, reply in observed.replies:
            strings: list[str] = []

            def collect(value: Any, strings: list[str] = strings) -> None:
                if isinstance(value, str):
                    strings.append(normalized_name(value))
                elif isinstance(value, dict):
                    for v in value.values():
                        collect(v)
                elif isinstance(value, list):
                    for v in value:
                        collect(v)

            collect(reply)
            scores[model] = sum(normalized_name(p) in strings for p in CONTROL_PHRASES)
        report["control"] = {
            "scores_out_of": 6,
            "reader_scores": scores,
            "informational": True,
            "arabic_fields_dropped": sum(len(r.dropped_fields) for r in control.readers)
            if isinstance(control, DocumentRead)
            else None,
            "latency_ms": round((monotonic() - started) * 1000, 2),
            "estimated_usd": round(spend.estimated - before, 8),
        }
        with private_path.open("x") as stream:
            os.chmod(private_path, 0o600)
            json.dump(private, stream, ensure_ascii=False, indent=2)
    except Exception:
        report["runner_error"] = "live_check_exception_details_withheld"
    finally:
        report.update(
            calls=list(spend.calls),
            provider_requests=client.count if client else 0,
            estimated_usd=round(spend.estimated, 8),
            finished_at=datetime.now(UTC).isoformat(),
        )
        report["state"] = (
            "passed"
            if len(report["papers"]) == 2
            and all(p["passed"] for p in report["papers"])
            and report["control"]
            and "runner_error" not in report
            else "failed"
        )
        if any(p.get("ground_truth_available") is False for p in report["papers"]):
            report["scoring_limit"] = (
                "No independent per-row ground truth supplied; returned names and counts "
                "are not recovery or zero-invention proof."
            )
        destination.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return report
