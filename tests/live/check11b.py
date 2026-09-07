"""The single 11b allowance: private local MP3, Voxtral then Scribe, redacted evidence."""

import asyncio
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from strands.models import BedrockModel, Model

from sanad.agents.factory import Proposal, make_agent
from sanad.media.audio import ConvertedAudio, FFmpegConverter
from sanad.media.numbers import numbers_in
from sanad.media.speech import PROMPT_VERSION as SPEECH_VERSION
from sanad.media.speech import SpeechAdapter, Transcript, TranscriptFailure
from sanad.models.io import BedrockCaller, private_provider_logs
from sanad.models.registry import ModelRegistry, ModelRole
from sanad.models.timeouts import (
    EXTRACTION_READ_TIMEOUT,
    PROVIDER_CONNECT_TIMEOUT,
    SPEECH_READ_TIMEOUT,
    TRANSCRIPTION_TIMEOUT,
)
from sanad.scribe.extract import PROMPT_VERSION, SYSTEM_PROMPT, DictationCandidate, candidate_issues
from sanad.scribe.names import dictionary, prepare_names
from sanad.scribe.turn import EXTRACTION_TIMEOUT

from .check08 import RATES, BudgetClient, SpendGuard, SpendLimit, scoped

ROOT = Path(__file__).resolve().parents[2]
DATE = datetime.now(ZoneInfo("Africa/Cairo")).date().isoformat()
EVIDENCE = ROOT / "docs/evidence/live-11b-2026-09-07d.json"


class DictationSpend(SpendGuard):
    def reserve(self, model_id: str, max_output: int) -> float:
        # This harness validates <=60 seconds before calling either adapter.
        # Reserve unknown usage too, including an unavailable speech attempt.
        rate_in, rate_out = RATES[model_id]
        audio = 0.006 if model_id == ModelRegistry().speech else 0
        maximum = (50000 * rate_in + max_output * rate_out) / 1_000_000 + audio
        with self.lock:
            if self.estimated + maximum > self.cap:
                raise SpendLimit("live_cost_cap")
            self.estimated += maximum
        return maximum


class DictationClients:
    """Route the shared request allowance through each provider's own read cap."""

    def __init__(self, speech: Any, extraction: Any):
        self.speech, self.extraction, self.meta = speech, extraction, speech.meta

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        client = self.speech if kwargs["modelId"] == ModelRegistry().speech else self.extraction
        return client.converse(**kwargs)  # type: ignore[no-any-return]


class DictationRequests(BudgetClient):
    speech_calls = 0
    extraction_calls = 0
    retry_authorized = False

    def allow_speech_retry(self) -> None:
        if self.speech_calls != 1 or self.extraction_calls or self.retry_authorized:
            raise RuntimeError("11b_retry_allowance")
        self.retry_authorized = True

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        registry = ModelRegistry()
        model = kwargs.get("modelId")
        if (
            self.extraction_calls
            or model not in {registry.speech, registry.worker}
            or (model == registry.worker and not self.speech_calls)
            or (
                model == registry.speech
                and self.speech_calls >= (2 if self.retry_authorized else 1)
            )
        ):
            raise RuntimeError("11b_request_allowance")
        if not 0 < kwargs.get("inferenceConfig", {}).get("maxTokens", 0) <= 2048:
            raise RuntimeError("output_limit")
        # Bytes are bounded separately; never stringify or log the request.
        if len(json.dumps(kwargs, default=lambda value: f"<bytes:{len(value)}>").encode()) > 50000:
            raise RuntimeError("input_limit")
        for message in kwargs.get("messages", []):
            for content in message.get("content", []):
                if "audio" in content and len(content["audio"]["source"]["bytes"]) > 1_000_000:
                    raise RuntimeError("audio_limit")
        if model == registry.speech:
            self.speech_calls += 1
        else:
            self.extraction_calls += 1
        return super().converse(**kwargs)


async def run_check(
    destination: Path = EVIDENCE,
    *,
    speech: SpeechAdapter | None = None,
    model_factory: Callable[[ModelRegistry, ModelRole], Model] | None = None,
) -> dict[str, Any]:
    if os.environ.get("SANAD_LIVE") != "1":
        raise RuntimeError("SANAD_LIVE=1 is required")
    if (speech is None) != (model_factory is None):
        raise ValueError("inject both providers or neither")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if any(
        json.loads(p.read_text()).get("attempt") == 5
        for p in destination.parent.glob("live-11b-*.json")
    ):
        raise RuntimeError("11b allowance already recorded; do not rerun")
    report: dict[str, Any] = {
        "contract": "11b",
        "attempt": 5,
        "date": DATE,
        "run_count": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "state": "started",
        "prompt_version": PROMPT_VERSION,
        "speech_prompt_version": SPEECH_VERSION,
        "spend_cap_usd": 0.05,
        "checks": {},
        "calls": [],
        "speech_attempts": [],
        "speech_retry_count": 0,
        "estimated_usd": 0,
        "redaction": (
            "Template ids, counts, resolved names, operational metadata only; "
            "no source text, patient identities, media, credentials or provider exception bodies."
        ),
        "cost_basis": "Accepted check08 rates and conservative full audio minute; not an invoice.",
    }
    with destination.open("x") as output:
        json.dump(report, output, indent=2)
    spend = DictationSpend(cap=0.05)
    context = scoped()
    private_provider_logs()
    try:
        client: DictationRequests | None = None
        live = speech is None
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
            client = DictationRequests(
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
            speech = SpeechAdapter(
                BedrockCaller(client, context.policy.policy_version, timeout=TRANSCRIPTION_TIMEOUT),
                FFmpegConverter(),
                context.source,
            )

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
        assert speech is not None and model_factory is not None
        data = (ROOT / "lane/spikes/real_dictation_44s.mp3").read_bytes() if live else b"synthetic"
        checks = report["checks"]
        converted = await asyncio.to_thread(speech.converter.convert, data, "mp3")
        if not isinstance(converted, ConvertedAudio) or converted.duration > 60:
            raise RuntimeError("11b_audio_duration_bound")
        transcript = await speech.transcribe_converted(converted)
        report["speech_attempts"].append(
            "ok" if isinstance(transcript, Transcript) else transcript.reason
        )
        if isinstance(transcript, TranscriptFailure) and transcript.reason in {
            "timeout",
            "unavailable",
        }:
            if client is not None:
                client.allow_speech_retry()
            report["speech_retry_count"] = 1
            # The same converted bytes and prompt, one retry within this allowance.
            transcript = await speech.transcribe_converted(converted)
            report["speech_attempts"].append(
                "ok" if isinstance(transcript, Transcript) else transcript.reason
            )
        checks["transcript_validated"] = isinstance(transcript, Transcript)
        if isinstance(transcript, Transcript):
            checks["numbers_line_parsed"] = transcript.numbers_line == "parsed"
            checks["only_expected_dispute"] = set(transcript.disputed_numbers) <= {"45"}
            report["disputed_count"] = len(transcript.disputed_numbers)
            agent = make_agent(
                "scribe",
                scope=context,
                tools=(),
                system_prompt=SYSTEM_PROMPT,
                session_key="live-11b",
                model_factory=model_factory,
            )
            result = await agent.propose(
                DictationCandidate, transcript.text, want_spans=False, timeout=EXTRACTION_TIMEOUT
            )
            checks["candidate_validated"] = isinstance(result, Proposal)
            if isinstance(result, Proposal):
                value, names = prepare_names(result.value, transcript.text)
                issues = (
                    *candidate_issues(value, transcript.text, transcript.disputed_numbers),
                    *names,
                )
                orders = {o.drug: (i, o) for i, o in enumerate(value.orders)}
                report["resolved_drug_names"] = sorted(
                    set(orders) & {e.latin for e in dictionary()}
                )
                report["order_count"] = len(value.orders)
                checks["three_named_orders"] = len(value.orders) == 3 and set(orders) == {
                    "Exforge HCT",
                    "Concor",
                    "Forxiga",
                }
                for name, action in (
                    ("Exforge HCT", "continue"),
                    ("Concor", "continue"),
                    ("Forxiga", "start"),
                ):
                    pair = orders.get(name)
                    checks[name + "_action"] = bool(pair and pair[1].action == action)
                    if pair:
                        i, order = pair
                        dose_question = any(
                            x.item == f"order:{i}" and x.code in {"dose_missing", "dose_unclear"}
                            for x in issues
                        )
                        if name == "Exforge HCT":
                            quoted_question = dose_question and any(
                                x.item == f"order:{i}"
                                and x.code == "dose_unclear"
                                and x.question
                                and order.dose
                                and f'"{order.dose}"' in x.question
                                and order.dose in transcript.text
                                for x in issues
                            )
                            checks[name + "_dose"] = numbers_in(order.dose or "") == (
                                "5",
                                "160",
                                "12.5",
                            ) or bool(quoted_question)
                        elif name == "Concor":
                            checks[name + "_dose"] = numbers_in(order.dose or "") == ("5",)
                        else:
                            checks[name + "_dose"] = bool(order.dose) or dose_question
                labs = " ".join(m.text for m in value.missions if m.kind == "TEST")
                import re

                report["resolved_lab_names"] = [
                    n
                    for n in ("BUN", "creatinine", "Na", "K")
                    if re.search(r"(?<!\w)" + n + r"(?!\w)", labs)
                ]
                report["mission_count"] = len(value.missions)
                checks["four_voice_labs"] = len(report["resolved_lab_names"]) == 4
                checks["no_medication_history"] = not any(
                    f.category == "medication_history" for f in value.facts
                )
                checks["no_unsupported_digits"] = not any(
                    i.code == "unsupported_number" for i in issues
                )
                checks["one_extraction_call"] = len(result.metadata) == 1
                report["issue_codes"] = sorted({i.code for i in issues})
        report["state"] = "passed" if len(checks) >= 13 and all(checks.values()) else "failed"
    except Exception:
        report["state"] = "failed"
        report["runner_error"] = "details_withheld"
    finally:
        report["calls"] = spend.calls
        report["estimated_usd"] = round(spend.estimated, 8)
        report["finished_at"] = datetime.now(UTC).isoformat()
        destination.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)
    return report
