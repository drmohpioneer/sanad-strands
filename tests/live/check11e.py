"""One claimed invocation, five full notes, bounded providers and private raw failures."""

import json
import os
import re
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
import httpx
from botocore.config import Config  # type: ignore[import-untyped]
from harness import FakeClock
from providers.fixtures import FakeS3, FakeTelegramFiles
from store.scribe_fixtures import ScribeWorld
from store.test_scribe_voice_web import voice
from strands.models import BedrockModel, Model

from sanad.domain import DRAFT_POLICY_2026_09, Principal, Provenance
from sanad.media.audio import AudioConverter, ConversionFailure, ConvertedAudio, FFmpegConverter
from sanad.media.retrieve import MediaRetriever
from sanad.media.speech import SpeechAdapter
from sanad.models.io import BedrockCaller, ModelCaller, private_provider_logs
from sanad.models.registry import ModelRegistry, ModelRole
from sanad.models.timeouts import (
    EXTRACTION_READ_TIMEOUT,
    PROVIDER_CONNECT_TIMEOUT,
    SPEECH_READ_TIMEOUT,
    TRANSCRIPTION_TIMEOUT,
)
from sanad.scribe.card import render_card
from sanad.scribe.extract import PROMPT_VERSION
from sanad.scribe.proposal import Proposal
from sanad.steward.types import StewardPolicy
from sanad.store import keys
from sanad.store.keys import IntakeScope
from sanad.store.memory import MemoryStore
from sanad.store.records import InboundReceipt

from .check08 import RATES, BudgetClient, SpendLimit
from .check11b import DictationClients, DictationSpend
from .check11c import displayed_numbers_supported

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/evidence/live-11e-2026-09-07c.json"
INVENTED = re.compile(
    r"\b(?:J wave|ST depression|LVH|RBBB|grade|New disease|null|none|daily)\b", re.I
)


def prompt_within_budget(input_tokens: int) -> bool:
    # Bedrock usage measures the complete initial request, including schema,
    # tool declaration and source. No separate token-count network request.
    return 0 < input_tokens < 2800


class EnglishAudio:
    def __init__(self, converter: AudioConverter):
        self.converter = converter

    def convert(self, audio: bytes, fmt: str) -> ConvertedAudio | ConversionFailure:
        converted = self.converter.convert(audio, fmt)
        if isinstance(converted, ConvertedAudio) and (
            converted.duration > 62 or len(converted.data) > 1_000_000
        ):
            return ConversionFailure(reason="too_long" if converted.duration > 62 else "too_large")
        return converted


class EnglishSpend(DictationSpend):
    def reserve(self, model_id: str, max_output: int) -> float:
        rate_in, rate_out = RATES[model_id]
        audio = 0.012 if model_id == ModelRegistry().speech else 0
        maximum = (50000 * rate_in + max_output * rate_out) / 1_000_000 + audio
        with self.lock:
            if self.estimated + maximum > self.cap:
                raise SpendLimit("live_cost_cap")
            self.estimated += maximum
        return maximum

    def finish(self, model_id: str, reserved: float, usage: dict[str, Any], latency: float) -> None:
        rate_in, rate_out = RATES[model_id]
        known = "inputTokens" in usage and "outputTokens" in usage
        actual = (
            usage.get("inputTokens", 0) * rate_in + usage.get("outputTokens", 0) * rate_out
        ) / 1_000_000
        if model_id == ModelRegistry().speech:
            actual += 0.012
        if not known:
            actual = reserved
        with self.lock:
            self.estimated += actual - reserved
            self.calls.append(
                {
                    "model_id": model_id,
                    "policy_version": DRAFT_POLICY_2026_09.policy_version,
                    "latency_ms": round(latency, 2),
                    "input_tokens": usage.get("inputTokens", 0),
                    "output_tokens": usage.get("outputTokens", 0),
                    "usage_known": known,
                    "estimated_usd": round(actual, 8),
                }
            )
            if self.estimated > self.cap:
                raise SpendLimit("live_cost_cap")


class Requests11e(BudgetClient):
    def __init__(self, client: Any, spend: DictationSpend, private: Path):
        super().__init__(client, spend)
        self.private = private
        self.run = 0
        self.counts: dict[int, list[int]] = {}
        self.lock = threading.Lock()
        self.initial_tokens: dict[int, list[int]] = {}

    def begin(self, run: int) -> None:
        with self.lock:
            if run != self.run + 1 or run > 5:
                raise RuntimeError("11e_run_allowance")
            self.run = run
            self.counts[run] = [0, 0]
            self.initial_tokens[run] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        registry = ModelRegistry()
        model = kwargs.get("modelId")
        if model not in {registry.speech, registry.worker}:
            raise RuntimeError("11e_model_allowance")
        if not 0 < kwargs.get("inferenceConfig", {}).get("maxTokens", 0) <= 2048:
            raise RuntimeError("output_limit")
        if len(json.dumps(kwargs, default=lambda v: f"<bytes:{len(v)}>").encode()) > 50000:
            raise RuntimeError("input_limit")
        for message in kwargs.get("messages", []):
            for content in message.get("content", []):
                if "audio" in content and len(content["audio"]["source"]["bytes"]) > 1_000_000:
                    raise RuntimeError("audio_limit")
        with self.lock:
            run = self.run
            if run not in self.counts:
                raise RuntimeError("11e_run_allowance")
            speech, extraction = self.counts[run]
            if model == registry.speech:
                if speech or extraction:
                    raise RuntimeError("11e_speech_allowance")
                index = 0
            else:
                if not speech or extraction >= 28:
                    raise RuntimeError("11e_extraction_allowance")
                if kwargs.get("inferenceConfig", {}).get("temperature") != 0:
                    raise RuntimeError("temperature_limit")
                index = 1
            self.counts[run][index] += 1
            number = self.counts[run][index]
        record: dict[str, Any] = {
            "run": run,
            "provider": "speech" if index == 0 else "scribe",
            "call": number,
        }
        try:
            result = super().converse(**kwargs)
            record["reply"] = result
            if index == 1 and len(kwargs.get("messages", [])) == 1:
                tokens = result.get("usage", {}).get("inputTokens", 0)
                with self.lock:
                    self.initial_tokens[run].append(tokens)
            return result
        except Exception as error:
            record["failure_type"] = type(error).__name__
            record["raw_failure"] = str(error)
            raise
        finally:
            # One private file per call; no raw content in pytest output or public evidence.
            (self.private / f"run-{run}-{index}-{number}.json").write_text(
                json.dumps(record, ensure_ascii=False, default=str, indent=2) + "\n"
            )


def card_parts(p: Proposal) -> dict[str, Any]:
    card = "\n\n".join(render_card(p))
    # Inspect the delivered text. Candidate projections cannot pass a hidden card.
    sections: dict[str, list[str]] = {}
    current = "patient"
    for line in card.splitlines():
        if line in {
            "Medications:",
            "Requested:",
            "History:",
            "Needs confirmation:",
            "Notify me if:",
        }:
            current = line
        elif line.startswith("✅") or line == "valid 30 minutes":
            current = "footer"
        elif line:
            sections.setdefault(current, []).append(line)
    drugs = sections.get("Medications:", [])
    requested = sections.get("Requested:", [])
    tests = [line.split(" — due ")[0] for line in requested if line.startswith("TEST:")]
    tasks = [line for line in requested if line.startswith("TASK:")]
    history = sections.get("History:", [])
    questions = sections.get("Needs confirmation:", [])
    exforge = [line for line in drugs if "Exforge" in line]
    forxiga = [line for line in drugs if "Forxiga" in line]
    return {
        "card": card,
        "drugs": drugs,
        "tests": tests,
        "tasks": tasks,
        "history": history,
        "questions": questions,
        "checks": {
            "patient": sections.get("patient") == ["New patient: Ahmed Saad, 53"],
            "exforge_change": len(exforge) == 1
            and "Exforge 5/160 → Exforge HCT 10/160/25" in exforge[0]
            and "(change)" in exforge[0],
            "forxiga_start": forxiga == ["Forxiga (start)"],
            "forxiga_dose_question": any("Forxiga" in q and "dose" in q for q in questions),
            "no_generic_line": not any(
                re.search(r"amlodipine|valsartan|hydrochlorothiazide|dapagliflozin", line, re.I)
                for line in drugs
            ),
            "four_labs": len(tests) == 1
            and set(t.strip().casefold() for t in tests[0][6:].split(","))
            == {"cbc", "na", "k", "lipid profile"},
            "monitoring_task": len(tasks) == 1
            and "blood pressure" in tasks[0].lower()
            and bool(re.search(r"\b(?:3|three) times a day\b", tasks[0]))
            and "5 days" in tasks[0]
            and not any("blood pressure" in t.lower() for t in tests)
            and "MONITOR:" not in card,
            "no_invented_tokens": INVENTED.search(card) is None,
            "no_question_marks": "(؟)" not in card,
            "at_most_three_questions": len(questions) <= 3,
            "english_only": re.search(r"[\u0600-\u06ff]", card) is None,
            "card_visible": bool(drugs and requested and history),
            "numbers_supported": displayed_numbers_supported(p),
            "at_least_three_history_lines": len(history) >= 3,
            "dx_history": "Dx: diabetes, hypertension" in history,
            "ecg_history": any(
                line.startswith("ECG:") and "T wave inversion" in line and "lateral" in line
                for line in history
            ),
            "echo_history": any(line.startswith("Echo:") and "EF 45%" in line for line in history),
        },
    }


def agreement(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reference = runs[0] if runs else {}
    return [
        {
            "run": i + 1,
            "drug_lines": [
                line == reference.get("drugs", [])[j]
                if j < len(reference.get("drugs", []))
                else False
                for j, line in enumerate(run.get("drugs", []))
            ],
            "all_drugs": bool(run.get("drugs")) and run.get("drugs") == reference.get("drugs"),
            "exforge_line": bool(run.get("drugs"))
            and bool(reference.get("drugs"))
            and run["drugs"][0].startswith("Exforge 5/160")
            and run["drugs"][0] == reference["drugs"][0],
            "test_line": bool(run.get("tests")) and run.get("tests") == reference.get("tests"),
            "task_line": bool(run.get("tasks")) and run.get("tasks") == reference.get("tasks"),
            "history_count": len(run.get("history", [])),
            "same_history_count": bool(run.get("history"))
            and len(run.get("history", [])) == len(reference.get("history", [])),
            "history_lines": [
                line == reference.get("history", [])[j]
                if j < len(reference.get("history", []))
                else False
                for j, line in enumerate(run.get("history", []))
            ],
        }
        for i, run in enumerate(runs)
    ]


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
    if any(v is not None for v in injected) and not all(v is not None for v in injected):
        raise ValueError("inject every provider or none")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or any(
        json.loads(path.read_text()).get("attempt", 1) in {3, 4}
        for path in destination.parent.glob("live-11e-*.json")
    ):
        raise RuntimeError("11e allowance already recorded; do not rerun")
    started = datetime.now(UTC)
    report: dict[str, Any] = {
        "contract": "11e",
        "attempt": 4,
        "allowance": "Binding addenda 2 and 3, English five-run check",
        "state": "started",
        "run_count": 5,
        "started_at": started.isoformat(),
        "prompt_version": PROMPT_VERSION,
        "spend_cap_usd": 0.15,
        "runs": [],
        "calls": [],
        "cost_basis": (
            "Accepted check08 rates; two full audio minutes and unknown usage reserved, "
            "not an invoice."
        ),
        "redaction": (
            "Owner-confirmed synthetic cards only. Raw replies and raw failures retained "
            "privately; no credentials, actor IDs or media."
        ),
    }
    with destination.open("x") as stream:
        json.dump(report, stream, indent=2)
    private = private_review or Path(tempfile.mkdtemp(prefix="sanad-11e-live-", dir="/private/tmp"))
    private.mkdir(parents=True, exist_ok=True)
    report["private_evidence"] = str(private)
    spend = EnglishSpend(cap=0.15)
    client: Requests11e | None = None
    live = model_factory is None
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
            client = Requests11e(
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
                private,
            )
            speech_caller = BedrockCaller(
                client, DRAFT_POLICY_2026_09.policy_version, timeout=TRANSCRIPTION_TIMEOUT
            )
            converter = FFmpegConverter()
            rxnorm_client = httpx.Client(timeout=3, follow_redirects=False, trust_env=False)
            data = (ROOT / "lane/spikes/real_dictation_en_61s.mp3").read_bytes()

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
        for run in range(1, 6):
            details: dict[str, Any] = {"run": run, "state": "started", "checks": {}}
            report["runs"].append(details)
            try:
                if client:
                    client.begin(run)
                # Every run has a fresh doctor, empty memory, receipt and full ASR.
                world = ScribeWorld.create(MemoryStore(), FakeClock(started))
                world.approve(language="en")
                world.scribe.model_factory = model_factory
                world.scribe.rxnorm_client = rxnorm_client
                details["retries"] = []
                world.scribe.observe_retry = details["retries"].append
                files, s3, audio = FakeTelegramFiles(data), FakeS3(), EnglishAudio(converter)

                def media(
                    receipt: InboundReceipt,
                    actor: Principal,
                    *,
                    world: ScribeWorld = world,
                    s3: FakeS3 = s3,
                    files: FakeTelegramFiles = files,
                    audio: EnglishAudio = audio,
                ) -> MediaRetriever:
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

                def speech(source: Provenance, audio: EnglishAudio = audio) -> SpeechAdapter:
                    assert speech_caller is not None
                    return SpeechAdapter(speech_caller, audio, source)

                world.scribe.media_factory, world.scribe.speech_factory = media, speech
                world.post(voice())
                p = world.scribe.repo.pending(world.doctor.scope)
                details["checks"]["card_created"] = p is not None
                if p:
                    details.update(card_parts(p))
                    details["checks"]["card_created"] = True
                    details["rxnorm_http_calls"] = p.rxnorm_calls
                    (private / f"run-{run}-proposal.json").write_text(p.model_dump_json(indent=2))
                if client:
                    details["prompt_input_tokens"] = client.initial_tokens[run]
                    details["provider_calls"] = client.counts[run]
                    details["checks"]["prompt_under_2800"] = bool(
                        client.initial_tokens[run]
                    ) and all(prompt_within_budget(n) for n in client.initial_tokens[run])
            except Exception as error:
                details["failure_code"] = "runner_failure"
                (private / f"run-{run}-failure.json").write_text(
                    json.dumps({"type": type(error).__name__, "raw_failure": str(error)})
                )
            details["state"] = (
                "passed"
                if details["checks"]
                and all(details["checks"].values())
                and "failure_code" not in details
                else "failed"
            )
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except Exception as error:
        report["runner_failure"] = True
        (private / "setup-failure.json").write_text(
            json.dumps({"type": type(error).__name__, "raw_failure": str(error)})
        )
    finally:
        report["agreement"] = agreement(report["runs"])
        report["calls"], report["estimated_usd"] = spend.calls, round(spend.estimated, 8)
        passed = len(report["runs"]) == 5 and all(r["state"] == "passed" for r in report["runs"])
        passed = passed and all(
            r["all_drugs"] and r["exforge_line"] and r["test_line"] and r["task_line"]
            for r in report["agreement"]
        )
        report["state"] = "passed" if passed else "failed"
        report["finished_at"] = datetime.now(UTC).isoformat()
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if live and rxnorm_client:
            rxnorm_client.close()
    print(
        json.dumps(
            {
                "state": report["state"],
                "runs": [r["state"] for r in report["runs"]],
                "provider_calls": len(spend.calls),
                "estimated_usd": report["estimated_usd"],
            }
        ),
        flush=True,
    )
    return report
