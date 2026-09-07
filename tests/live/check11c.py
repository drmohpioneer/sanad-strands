"""One two-part allowance, real providers and only synthetic local care state."""

import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
import httpx
from botocore.config import Config  # type: ignore[import-untyped]
from harness import FakeClock
from providers.fixtures import FakeS3, FakeTelegramFiles
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld
from store.test_scribe_voice_web import voice
from strands.models import BedrockModel, Model

from sanad.agents.factory import ProposalFailure
from sanad.channels.telegram.router import RouteResult
from sanad.domain import DRAFT_POLICY_2026_09, Principal, Provenance
from sanad.media.audio import AudioConverter, ConversionFailure, ConvertedAudio, FFmpegConverter
from sanad.media.numbers import numbers_in
from sanad.media.retrieve import MediaRetriever
from sanad.media.speech import PROMPT_VERSION as SPEECH_VERSION
from sanad.media.speech import SpeechAdapter, Transcript, TranscriptFailure
from sanad.models.io import BedrockCaller, ModelCaller, ModelUnavailable, private_provider_logs
from sanad.models.registry import ModelRegistry, ModelRole
from sanad.models.timeouts import (
    EXTRACTION_READ_TIMEOUT,
    PROVIDER_CONNECT_TIMEOUT,
    SPEECH_READ_TIMEOUT,
    TRANSCRIPTION_TIMEOUT,
)
from sanad.scribe.card import clinical_line, dictation_questions, medication_line, render_card
from sanad.scribe.clinical import TERM_QUESTION
from sanad.scribe.extract import PROMPT_VERSION, REQUEST_MISSING_QUESTION
from sanad.scribe.memory import memory_rows
from sanad.scribe.proposal import Proposal
from sanad.steward.types import StewardPolicy
from sanad.store import keys
from sanad.store.keys import AccountScope, IntakeScope
from sanad.store.memory import MemoryStore
from sanad.store.records import InboundReceipt

from .check08 import BudgetClient
from .check11b import DictationClients, DictationSpend

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/evidence/live-11c-2026-09-07e.json"
EARLIER_EVIDENCE = "live-11c-2026-09-07.json"
EARLIER_EVIDENCES = {
    EARLIER_EVIDENCE,
    "live-11c-2026-09-07b.json",
    "live-11c-2026-09-07c.json",
    "live-11c-2026-09-07d.json",
}
CORRECTION = "إكسفورج 5/160/12.5، فورسيجا 10، والـ 45 ده الـ EF"


class Requests11c(BudgetClient):
    """One speech call and two Scribe turns, at most two seven-call extraction attempts per turn."""

    def __init__(self, client: Any, spend: DictationSpend):
        super().__init__(client, spend)
        self.speech_calls = 0
        self.speech_retry_allowed = False
        self.part = 1
        self.extractions = {1: 0, 2: 0}

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        registry = ModelRegistry()
        model = kwargs.get("modelId")
        if model not in {registry.speech, registry.worker}:
            raise RuntimeError("11c_model_allowance")
        if model == registry.speech:
            if (
                (self.speech_calls and not self.speech_retry_allowed)
                or self.speech_calls >= 2
                or any(self.extractions.values())
            ):
                raise RuntimeError("11c_speech_allowance")
        elif not self.speech_calls or self.extractions[self.part] >= 14:
            raise RuntimeError("11c_extraction_allowance")
        if not 0 < kwargs.get("inferenceConfig", {}).get("maxTokens", 0) <= 2048:
            raise RuntimeError("output_limit")
        if len(json.dumps(kwargs, default=lambda v: f"<bytes:{len(v)}>").encode()) > 50000:
            raise RuntimeError("input_limit")
        for message in kwargs.get("messages", []):
            for content in message.get("content", []):
                if "audio" in content and len(content["audio"]["source"]["bytes"]) > 1_000_000:
                    raise RuntimeError("audio_limit")
        if model == registry.speech:
            self.speech_calls += 1
        else:
            self.extractions[self.part] += 1
        return super().converse(**kwargs)

    def correction(self) -> None:
        if self.part != 1 or not self.extractions[1]:
            raise RuntimeError("11c_part_allowance")
        self.part = 2


class BoundedAudio:
    def __init__(self, converter: AudioConverter):
        self.converter = converter

    def convert(self, audio: bytes, fmt: str) -> ConvertedAudio | ConversionFailure:
        converted = self.converter.convert(audio, fmt)
        if isinstance(converted, ConvertedAudio) and (
            converted.duration > 60 or len(converted.data) > 1_000_000
        ):
            return ConversionFailure(reason="too_long" if converted.duration > 60 else "too_large")
        return converted


def displayed_numbers_supported(proposal: Proposal) -> bool:
    """Check clinical display, excluding code-built dates and explicit doubt quotes.

    Unsupported model tokens remain blocked candidates under addendum 3. They
    may appear in clarification questions, never as a displayed instruction.
    """
    rendered = "\n".join(
        (
            *(medication_line(proposal, i) for i in range(len(proposal.candidate.orders))),
            *(
                clinical_line(proposal, f"fact:{i}", f.text)
                for i, f in enumerate(proposal.candidate.facts)
            ),
            *(
                clinical_line(proposal, f"mission:{i}", m.text)
                for i, m in enumerate(proposal.candidate.missions)
            ),
        )
    )
    return set(numbers_in(rendered)) <= set(numbers_in(proposal.source_text))


def run_check(
    destination: Path = EVIDENCE,
    *,
    model_factory: Callable[[ModelRegistry, ModelRole], Model] | None = None,
    speech_caller: ModelCaller | None = None,
    converter: AudioConverter | None = None,
    rxnorm_client: httpx.Client | None = None,
    data: bytes | None = None,
    private_review: Path | None = None,
) -> dict[str, Any]:
    if os.environ.get("SANAD_LIVE") != "1":
        raise RuntimeError("SANAD_LIVE=1 is required")
    injected = (model_factory, speech_caller, converter, rxnorm_client, data)
    if any(x is not None for x in injected) and not all(x is not None for x in injected):
        raise ValueError("inject every provider or none")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if any(p.name not in EARLIER_EVIDENCES for p in destination.parent.glob("live-11c-*.json")):
        raise RuntimeError("11c allowance already recorded; do not rerun")
    report: dict[str, Any] = {
        "contract": "11c",
        "attempt": 5,
        "run_count": 1,
        "state": "started",
        "started_at": datetime.now(UTC).isoformat(),
        "prompt_version": PROMPT_VERSION,
        "speech_prompt_version": SPEECH_VERSION,
        "spend_cap_usd": 0.10,
        "parts": {"1": {"state": "not_run", "checks": {}}, "2": {"state": "not_run", "checks": {}}},
        "calls": [],
        "retries": [],
        "estimated_usd": 0,
        "redaction": (
            "No replies, transcript, source values, identities, media, credentials or provider "
            "exceptions. Rendered card and vocabulary review kept privately."
        ),
        "cost_basis": (
            "Accepted check08 rates; full audio minute and unknown usage reserved, not an invoice."
        ),
    }
    # Durable one-shot claim precedes credential/client creation and all provider IO.
    with destination.open("x") as stream:
        json.dump(report, stream, indent=2)
    spend = DictationSpend(cap=0.10)
    client: Requests11c | None = None
    services: list[Any] = []
    transcripts: list[Transcript | TranscriptFailure] = []
    merged_card: str | None = None
    memories: list[dict[str, Any]] = []
    live = model_factory is None
    routes: list[RouteResult] = []
    failures: list[str] = []
    private_provider_logs()
    try:
        if live:
            speech_config = Config(
                connect_timeout=PROVIDER_CONNECT_TIMEOUT,
                read_timeout=SPEECH_READ_TIMEOUT,
                retries={"total_max_attempts": 1},
            )
            extraction_config = Config(
                connect_timeout=PROVIDER_CONNECT_TIMEOUT,
                read_timeout=EXTRACTION_READ_TIMEOUT,
                retries={"total_max_attempts": 1},
            )
            client = Requests11c(
                DictationClients(
                    boto3.client(
                        "bedrock-runtime", region_name=ModelRegistry().region, config=speech_config
                    ),
                    boto3.client(
                        "bedrock-runtime",
                        region_name=ModelRegistry().region,
                        config=extraction_config,
                    ),
                ),
                spend,
            )
            speech_caller = BedrockCaller(
                client, DRAFT_POLICY_2026_09.policy_version, timeout=TRANSCRIPTION_TIMEOUT
            )
            converter = FFmpegConverter()
            rxnorm_client = httpx.Client(timeout=3, follow_redirects=False, trust_env=False)
            data = (ROOT / "lane/spikes/real_dictation_44s.mp3").read_bytes()

            def factory(registry: ModelRegistry, role: ModelRole) -> Model:
                model = BedrockModel(
                    model_id=registry.model_id(role),
                    region_name=registry.region,
                    temperature=0,
                    streaming=False,
                    max_tokens=2048,
                    boto_client_config=extraction_config,
                )
                model.client = client
                return model

            model_factory = factory
        assert model_factory and speech_caller and converter and rxnorm_client and data
        world = ScribeWorld.create(MemoryStore(), FakeClock(datetime.now(UTC)))
        world.approve()
        world.scribe.model_factory = model_factory
        world.scribe.observe_retry = lambda reason: report["retries"].append(
            {
                "provider": "scribe",
                "part": client.part if client else 0,
                "attempt": 2,
                "reason": reason,
            }
        )
        world.scribe.rxnorm_client = rxnorm_client
        original_lookup = world.scribe.name_lookup

        def lookup(*args: Any, **kwargs: Any) -> Any:
            service = original_lookup(*args, **kwargs)
            services.append(service)
            return service

        world.scribe.name_lookup = lookup  # type: ignore[method-assign]
        original_run = world.scribe.run
        original_extract = world.scribe.extract_candidate

        def observed_run(*args: Any, **kwargs: Any) -> RouteResult:
            result = original_run(*args, **kwargs)
            routes.append(result)
            return result

        def observed_extract(*args: Any, **kwargs: Any) -> object:
            result = original_extract(*args, **kwargs)
            if isinstance(result, (ProposalFailure, ModelUnavailable)):
                # These are application-generated codes, never the provider exception/reply.
                failures.append(result.reason)
            return result

        def route_evidence(part: str, *, missing: bool) -> None:
            result = routes[-1] if routes else None
            report["parts"][part]["route"] = result.route if result else "not_run"
            report["parts"][part]["route_status"] = result.status if result else "not_run"
            speech_failure = next(
                (t.reason for t in transcripts if isinstance(t, TranscriptFailure)),
                None,
            )
            report["parts"][part]["failure_code"] = (
                (
                    failures[-1]
                    if failures
                    else speech_failure or (result.status if result else "route_not_run")
                )
                if missing
                else None
            )

        world.scribe.run = observed_run  # type: ignore[method-assign]
        world.scribe.extract_candidate = observed_extract  # type: ignore[method-assign]
        files, s3, audio = FakeTelegramFiles(data), FakeS3(), BoundedAudio(converter)

        def media(receipt: InboundReceipt, actor: Principal) -> MediaRetriever:
            return MediaRetriever(
                world.runtime.steward,
                s3,
                files,
                audio,
                IntakeScope(doctor_id=world.doctor.id, intake_id=keys.digest(receipt.id)),
                actor,
                lambda: world.claims.doctor(actor) is not None,
                StewardPolicy(DRAFT_POLICY_2026_09),
            )

        class ObservedSpeech(SpeechAdapter):
            async def transcribe_converted(
                self, converted: ConvertedAudio, *, expected_language: str = "ar"
            ) -> Transcript | TranscriptFailure:
                result = await super().transcribe_converted(
                    converted, expected_language=expected_language
                )
                transcripts.append(result)
                if isinstance(result, TranscriptFailure) and result.reason in {
                    "schema_validation",
                    "unavailable",
                    "model_unavailable",
                }:
                    report["retries"].append(
                        {
                            "provider": "speech",
                            "part": 1,
                            "attempt": 2,
                            "reason": result.reason,
                        }
                    )
                    if client:
                        client.speech_retry_allowed = True
                    result = await super().transcribe_converted(
                        converted, expected_language=expected_language
                    )
                    transcripts.append(result)
                return result

        def speech(source: Provenance) -> SpeechAdapter:
            assert speech_caller is not None
            return ObservedSpeech(speech_caller, audio, source)

        world.scribe.media_factory, world.scribe.speech_factory = media, speech
        world.post(voice())
        first = world.scribe.repo.pending(world.doctor.scope)
        route_evidence("1", missing=first is None)
        checks = report["parts"]["1"]["checks"]
        checks["speech_v4_bounded"] = 1 <= len(transcripts) <= 2 and isinstance(
            transcripts[-1], Transcript
        )
        checks["card_created"] = first is not None
        if first:
            first_card = "\n\n".join(render_card(first))
            checks["scribe_prompt_current"] = first.prompt_version == PROMPT_VERSION
            checks["three_names"] = {o.drug for o in first.candidate.orders} == {
                "Exforge HCT",
                "Concor",
                "Forxiga",
            } and len(first.candidate.orders) == 3
            tests = " ".join(n.latin for n in first.names if n.kind == "test")
            labs_present = (
                all(
                    re.search(r"(?<!\w)" + n + r"(?!\w)", tests)
                    for n in ("BUN", "creatinine", "Na", "K")
                )
                and tests.isascii()
            )
            request_blocked = (
                first_card.count(REQUEST_MISSING_QUESTION) == 1
                and any(
                    i.code == "request_missing" and i.item == "all" and i.blocked
                    for i in first.issues
                )
                and first.blocked("all")
            )
            report["parts"]["1"]["four_english_labs"] = labs_present
            report["parts"]["1"]["blocking_request_present"] = request_blocked
            checks["four_english_labs_or_blocking_request"] = labs_present or request_blocked
            checks["ecg_english"] = any(
                line.startswith("ECG:") and "T wave inversion" in line
                for line in first_card.splitlines()
            )
            checks["echo_english"] = any(
                line.startswith("Echo:") and "EF 45%" in line for line in first_card.splitlines()
            )
            checks["separate_fact_lines"] = sum(
                line.startswith(("ECG:", "Echo:", "Complaint:", "History:", "Dx:"))
                for line in first_card.splitlines()
            ) == len(first.candidate.facts)
            checks["no_invented_findings"] = (
                re.search(
                    r"\b(?:J wave|ST depression|LVH|RBBB|grade|New disease)\b", first_card, re.I
                )
                is None
            )
            checks["no_null"] = re.search(r"\b(?:null|none)\b", first_card, re.I) is None
            checks["no_unsupported_digits"] = displayed_numbers_supported(first)
            checks["at_most_one_term_question"] = first_card.count(TERM_QUESTION) <= 1 and not any(
                "صياغة" in q for q in dictation_questions(first)
            )
            report["parts"]["1"]["blocked_number_items"] = sum(
                i.code == "unsupported_number" for i in first.issues
            )
            checks["real_rxnorm_attempted"] = first.rxnorm_calls > 0
        report["parts"]["1"]["state"] = "passed" if all(checks.values()) else "failed"
        destination.write_text(json.dumps(report, indent=2) + "\n")
        if first:
            if client:
                client.correction()
            world.post(update(APPLICANT, CORRECTION, 11))
            second = world.scribe.repo.pending(world.doctor.scope)
            route_evidence("2", missing=second is None or second.version == first.version)
            checks = report["parts"]["2"]["checks"]
            checks["same_card_new_version"] = bool(
                second and second.id == first.id and second.version > first.version
            )
            if second:
                merged_card = "\n\n".join(render_card(second))
                orders = {o.drug: o for o in second.candidate.orders}
                checks["exforge_answer"] = (
                    "Exforge HCT" in orders and orders["Exforge HCT"].dose == "5/160/12.5"
                )
                checks["forxiga_answer"] = "Forxiga" in orders and orders["Forxiga"].dose == "10"
                checks["echo_answer"] = (
                    any(
                        line.startswith("Echo:") and "EF 45%" in line
                        for line in merged_card.splitlines()
                    )
                    and "% %" not in merged_card
                )
                checks["exforge_stays_verified"] = (
                    any(
                        line.startswith("Exforge HCT 5/160/12.5")
                        for line in merged_card.splitlines()
                    )
                    and "Exforge HCT (؟)" not in merged_card
                )
                checks["no_1x2"] = "1x2" not in merged_card
                checks["no_560_question"] = not any("560" in q for q in dictation_questions(second))
                checks["no_null"] = re.search(r"\b(?:null|none)\b", merged_card, re.I) is None
                checks["no_answered_questions"] = not any(
                    any(label in q for label in ("Exforge", "Forxiga", "EF"))
                    for q in dictation_questions(second)
                )
                checks["six_http_calls_per_card"] = second.rxnorm_calls <= 6
                checks["no_unsupported_digits"] = displayed_numbers_supported(second)
                report["parts"]["2"]["blocked_number_items"] = sum(
                    i.code == "unsupported_number" for i in second.issues
                )
                if second.blocked("all") or second.blocked("patient"):
                    report["parts"]["2"]["confirmation_status"] = "blocked"
                else:
                    world.tap()
                    report["parts"]["2"]["confirmation_status"] = (
                        "confirmed"
                        if world.scribe.repo.pending(world.doctor.scope) is None
                        else "not_confirmed"
                    )
                for scope in (
                    world.doctor.scope,
                    AccountScope(bot_id=world.doctor.telegram_bot_id),
                ):
                    for row in memory_rows(world.store, scope):
                        memories.append(
                            {
                                "scope_kind": "doctor" if scope == world.doctor.scope else "clinic",
                                **row.model_dump(
                                    mode="json", exclude={"scope", "id", "entity_type"}
                                ),
                            }
                        )
            report["parts"]["2"]["state"] = (
                "passed" if checks and all(checks.values()) else "failed"
            )
    except Exception:
        report["runner_error"] = "details_withheld"
    finally:
        report["calls"] = spend.calls
        report["estimated_usd"] = round(spend.estimated, 8)
        report["rxnorm_http_calls"] = max((s.budget.calls for s in services), default=0)
        report["real_rxnorm_attempted"] = live and report["rxnorm_http_calls"] > 0
        report["rxnorm_outcomes"] = [
            {k: row[k] for k in ("outcome", "failure_code", "http_status", "source", "found", "ms")}
            for service in services
            for row in service.table
        ]
        report["memory_rows_written"] = len(memories)
        report["state"] = (
            "passed"
            if all(p["state"] == "passed" for p in report["parts"].values())
            and "runner_error" not in report
            else "failed"
        )
        report["finished_at"] = datetime.now(UTC).isoformat()
        destination.write_text(json.dumps(report, indent=2) + "\n")
        if live or private_review:
            review = private_review or ROOT / "lane/spikes/live-11c-review-2026-09-07-attempt5.json"
            review.parent.mkdir(parents=True, exist_ok=True)
            review.write_text(
                json.dumps(
                    {
                        "merged_card": merged_card,
                        "rxnorm": [r for s in services for r in s.table],
                        "first_10_memory_rows": memories[:10],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        if live and rxnorm_client:
            rxnorm_client.close()
    print(
        json.dumps(
            {
                "state": report["state"],
                "parts": {n: p["state"] for n, p in report["parts"].items()},
                "provider_calls": len(spend.calls),
                "estimated_usd": report["estimated_usd"],
            }
        ),
        flush=True,
    )
    return report
