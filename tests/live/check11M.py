"""Two-run, USD 0.50 media measurement; only scores and usage leave memory.

Two passes: 2 * (3 speech + 5 photos * 2 readers) = 26 logical calls,
2 * (3 speech + 5 photos) = 16 scores, before retries.

Architect invocation (the repository's --live isolation guard still applies):
SANAD_LIVE=1 uv run pytest -o 'python_files=test_*.py check11M.py'
    tests/live --live --force-enable-socket -k contract11M_live
"""

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from deploy.common import env_values
from deploy.smoke import synthetic_english_clip
from sanad.domain import Provenance
from sanad.media.audio import ConvertedAudio, FFmpegConverter
from sanad.media.images import normalize_document
from sanad.media.limits import MAX_PIXELS, MediaInvalid, image_info
from sanad.media.speech import SpeechAdapter, Transcript, TranscriptFailure
from sanad.media.vision import DocumentRead, ReaderResult, VisionAdapter
from sanad.models.gemini import CONFIGS, GeminiCaller
from sanad.models.io import ModelCaller, ModelReply, ModelUnavailable
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY
from sanad.scribe.merge import split_drug_dose  # type: ignore[attr-defined]

RUNS = 2

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / f"docs/evidence/live-11M-{datetime.now(UTC).date().isoformat()}.json"
type FixtureId = Literal[
    "dictation",
    "handwritten",
    "arabic_probe",
    "english_smoke",
    "rx_synthetic",
    "lab_synthetic",
    "lab_synthetic_rotated_glare",
    "injection_synthetic",
]
type GeminiId = Literal["gemini-3.8-flash", "gemini-3.5-flash-lite"]
PACE_SECONDS: dict[GeminiId, float] = {"gemini-3.8-flash": 12.0, "gemini-3.5-flash-lite": 6.0}
# Paid rates even on free tier; 3.8 introductory rates through 2026-12-31.
# https://ai.google.dev/gemini-api/docs/pricing
RATES = {"gemini-3.8-flash": (0.75, 3.75), "gemini-3.5-flash-lite": (0.30, 2.50)}


class SafeRow(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)


class Usage(SafeRow):
    model_id: GeminiId | None
    status: Literal["ok", "unavailable", "timeout", "refused"]
    latency_ms: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    usage_known: bool
    estimated_usd: float = Field(ge=0)


class Score(SafeRow):
    fixture: FixtureId
    run: int = Field(ge=1, le=2)
    passed: bool
    matched: int = Field(default=0, ge=0)
    expected: int = Field(default=0, ge=0)
    invented: int = Field(default=0, ge=0)
    tests_matched: int = Field(default=0, ge=0)
    tests_expected: int = Field(default=0, ge=0)
    failure_route: bool = False
    readers: int = Field(default=0, ge=0, le=2)


class Evidence(SafeRow):
    contract: Literal["11M"] = "11M"
    state: Literal["started", "passed", "failed"] = "started"
    spend_cap_usd: float = Field(default=0.5, ge=0.5, le=0.5)
    estimated_usd: float = Field(default=0.0, ge=0)
    temperature_25: Literal[0] = 0
    temperature_3: Literal[1] = 1
    scores: list[Score] = Field(default_factory=list)
    calls: list[Usage] = Field(default_factory=list)
    pace_seconds: dict[GeminiId, float] = Field(default_factory=lambda: PACE_SECONDS.copy())


@dataclass
class Pacer:
    """Reserve adapter starts in the sequential check, outside adapter deadlines."""

    clock: Callable[[], float] = monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    last_start: dict[GeminiId, float] = field(default_factory=dict)

    async def reserve(self, model_ids: tuple[GeminiId, ...]) -> None:
        while True:
            now = self.clock()
            wait = max(
                (
                    self.last_start[model] + PACE_SECONDS[model] - now
                    for model in model_ids
                    if model in self.last_start
                ),
                default=0.0,
            )
            if wait <= 0:
                break
            await self.sleep(wait)
        for model in model_ids:
            self.last_start[model] = now


def write_evidence(
    destination: Path, report: dict[str, Any], *, exclusive: bool = False
) -> dict[str, Any]:
    """Only typed, allowlisted numeric scores/known IDs can reach the evidence file."""
    safe = Evidence.model_validate(report).model_dump(mode="json")
    with destination.open("x" if exclusive else "w") as output:
        json.dump(safe, output, indent=2)
        output.write("\n")
    return safe


@dataclass
class LiveMediaCaller:
    api_key: str = field(repr=False)

    async def call(
        self, model_id: str, content: list[dict[str, Any]], *, max_tokens: int = 2048
    ) -> ModelReply | ModelUnavailable:
        timeout = 30 if any("audio" in block for block in content) else 25
        return await GeminiCaller(POLICY.policy_version, self.api_key, timeout=timeout).call(
            model_id, content, max_tokens=max_tokens
        )


@dataclass
class BudgetCaller:
    caller: ModelCaller = field(repr=False)
    estimated: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)
    exhausted: bool = False

    def refuse(self, model_id: str) -> ModelUnavailable:
        self.calls.append(
            dict(
                model_id=model_id if model_id in RATES else None,
                status="refused",
                latency_ms=0.0,
                input_tokens=0,
                output_tokens=0,
                usage_known=True,
                estimated_usd=0.0,
            )
        )
        return ModelUnavailable(reason="budget_exhausted")

    async def call(
        self, model_id: str, content: list[dict[str, Any]], *, max_tokens: int = 2048
    ) -> ModelReply | ModelUnavailable:
        # Fixed fixtures: <=62 s audio, product-accepted <=20 MP photos and <=10k
        # UTF-8 prompt bytes. 30k input tokens conservatively covers these inputs.
        # Each logical call (including a vision nudge) reserves once; the internal
        # 429 retry adds no reservation. Unknown usage keeps the reservation in full.
        if model_id not in RATES or not 0 < max_tokens <= 2048:
            return self.refuse(model_id)
        if sum(len(c.get("text", "").encode()) for c in content) > 10000:
            return self.refuse(model_id)
        media = [c for c in content if "image" in c or "audio" in c]
        if len(media) != 1 or len(self.calls) >= 115:
            return self.refuse(model_id)
        if "image" in media[0]:
            try:
                info = image_info(media[0]["image"]["source"]["bytes"])
            except MediaInvalid:
                return self.refuse(model_id)
            if info.width * info.height > MAX_PIXELS:
                return self.refuse(model_id)
        elif len(media[0]["audio"]["source"]["bytes"]) > 2 * 1024 * 1024:
            return self.refuse(model_id)
        rate_in, rate_out = RATES[model_id]
        reserve = (30000 * rate_in + max_tokens * rate_out) / 1_000_000
        # No await between checking and reserving: two concurrent readers share it.
        if self.exhausted or self.estimated + reserve > 0.5:
            self.exhausted = True
            return self.refuse(model_id)
        self.estimated += reserve
        started = asyncio.get_running_loop().time()
        result: ModelReply | ModelUnavailable = ModelUnavailable(reason="unavailable")
        try:
            result = await self.caller.call(model_id, content, max_tokens=max_tokens)
        except Exception:
            result = ModelUnavailable(reason="unavailable")
        finally:
            meta = result.metadata if isinstance(result, ModelReply) else None
            status_meta = (
                result.metadata
                if isinstance(result, ModelReply)
                else (result.metadata[-1] if result.metadata else None)
            )
            status = (
                status_meta.status
                if status_meta
                else (
                    "timeout"
                    if isinstance(result, ModelUnavailable) and result.reason == "timeout"
                    else "unavailable"
                )
            )
            known = bool(meta and meta.usage_known)
            actual = (
                (meta.input_tokens * rate_in + meta.output_tokens * rate_out) / 1_000_000
                if meta and known
                else reserve
            )
            self.estimated += actual - reserve
            self.exhausted |= self.estimated >= 0.5
            self.calls.append(
                dict(
                    model_id=model_id,
                    status="unavailable" if status == "invalid" else status,
                    latency_ms=(asyncio.get_running_loop().time() - started) * 1000,
                    input_tokens=meta.input_tokens if meta else 0,
                    output_tokens=meta.output_tokens if meta else 0,
                    usage_known=known,
                    estimated_usd=actual,
                )
            )
        return result


@dataclass
class Inputs:
    audio: dict[str, ConvertedAudio] = field(repr=False)
    images: dict[str, bytes] = field(repr=False)
    dictation_key: dict[str, Any] = field(repr=False)
    prescription_key: dict[str, Any] = field(repr=False)


def live_inputs() -> Inputs:
    """Read private sources only after the architect opts in; never print paths/bodies."""
    folder = ROOT / "lane/spikes"
    converter = FFmpegConverter()
    audio = {}
    for name, filename in (
        ("dictation", "real_dictation_en_61s.mp3"),
        ("arabic_probe", "sample_ar_15s.wav"),
    ):
        path = folder / filename
        converted = converter.convert(path.read_bytes(), path.suffix[1:])
        if not isinstance(converted, ConvertedAudio) or converted.duration > 62:
            raise ValueError("invalid audio fixture")
        audio[name] = converted
    audio["english_smoke"] = ConvertedAudio(data=synthetic_english_clip(), duration=15)
    images = {"handwritten": (folder / "real_handwritten_rx.jpg").read_bytes()}
    for name in (
        "rx_synthetic",
        "lab_synthetic",
        "lab_synthetic_rotated_glare",
        "injection_synthetic",
    ):
        images[name] = (ROOT / "tests/data/09b" / (name + ".png")).read_bytes()
    return Inputs(
        audio,
        images,
        json.loads((folder / "real_dictation_en_61s.answer.json").read_text()),
        json.loads((folder / "real_handwritten_rx.answer.json").read_text()),
    )


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


NUMBER_WORDS = dict(
    zip(
        "one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen twenty".split(),
        map(str, range(1, 21)),
        strict=True,
    )
)


def canonicalized(text: str) -> str:
    text = normalized(text.replace("-", " "))
    text = re.sub(
        r"\b(?:" + "|".join(NUMBER_WORDS) + r")\b",
        lambda match: NUMBER_WORDS[match[0]],
        text,
    )
    text = re.sub(r"(?<=\d)\s+over\s+(?=\d)", "/", text)
    # Addendum 6 explicitly equates this key phrase with the heard "follow up".
    return re.sub(r"\bblood pressure follow up\b", "follow up", text)


def contains(text: str, term: str) -> bool:
    key = canonicalized(term)
    # BUN is the explicitly bounded abbreviation in addendum 6.
    prefix = key.isalpha() and len(key) <= 5 and key != "bun"
    return bool(
        re.search(r"(?<!\w)" + re.escape(key) + ("" if prefix else r"(?!\w)"), canonicalized(text))
    )


LAB_ROWS = (
    ("Potassium", "6.3", "mmol/L"),
    ("Sodium", "138", "mmol/L"),
    ("Creatinine", "2.4", "mg/dL"),
    ("Hemoglobin", "11.2", "g/dL"),
    ("Troponin I", "0.02", "ng/mL"),
    ("INR", "2.6", None),
    ("Glucose", "142", "mg/dL"),
)
RX_ROWS = (("Bisoprolol", "5"), ("Atorvastatin", "40"), ("Apixaban", "5"), ("Spironolactone", "25"))


def only_test_names(text: str, names: list[str]) -> bool:
    remainder = normalized(text)
    for name in sorted(names, key=len, reverse=True):
        remainder = re.sub(r"(?<!\w)" + re.escape(normalized(name)) + r"(?!\w)", "", remainder)
    remainder = re.sub(r"\band\b", "", remainder)
    return not re.search(r"\w", remainder)


def photo_score(name: FixtureId, run: int, result: object, key: dict[str, Any]) -> Score:
    if not isinstance(result, DocumentRead):
        return Score(fixture=name, run=run, passed=False)
    readers = result.readers
    if len(readers) != 2:
        return Score(fixture=name, run=run, passed=False, readers=len(readers))

    def rows(reader: ReaderResult) -> tuple[int, int, int, int, int]:
        items = []
        for row in reader.items:
            drug, suffix = split_drug_dose(row.item.name or "")
            items.append(
                row.item.model_copy(update={"name": drug, "dose": row.item.dose or suffix})
            )
        if name == "handwritten":
            drugs = key["medications"]
            matched = sum(
                sum(normalized(i.name or "") == normalized(d["drug"]) for i in items) == 1
                and any(
                    normalized(i.name or "") == normalized(d["drug"])
                    and contains(i.dose or "", d["dose"])
                    for i in items
                )
                for d in drugs
            )
            drug_names = {normalized(d["drug"]) for d in drugs}
            invented = sum(
                bool(i.name)
                and normalized(i.name or "") not in drug_names
                and not only_test_names(i.name or "", key["tests"])
                for i in items
            )
            text = " ".join([*(i.name or "" for i in items), *reader.notes])
            tests = sum(contains(text, term) for term in key["tests"])
            return matched, len(drugs), invented, tests, len(key["tests"])
        if name == "rx_synthetic":
            matched = sum(
                sum(normalized(i.name or "") == normalized(drug) for i in items) == 1
                and any(
                    normalized(i.name or "") == normalized(drug) and contains(i.dose or "", dose)
                    for i in items
                )
                for drug, dose in RX_ROWS
            )
            invented = sum(
                bool(i.name)
                and normalized(i.name or "") not in {normalized(d) for d, _ in RX_ROWS}
                and not only_test_names(i.name or "", ["echo", "labs", "K", "creatinine"])
                for i in items
            )
            return matched, 4, invented, 0, 0
        expected = (
            LAB_ROWS
            if name == "lab_synthetic"
            else (
                (("Potassium", "4.1", "mmol/L"),)
                if name == "injection_synthetic"
                else (("Potassium", "6.3", None), ("Creatinine", "2.4", None))
            )
        )
        matched = sum(
            any(
                normalized(i.name or "") == normalized(drug)
                and i.value == value
                and (unit is None or normalized(i.unit or "") == normalized(unit))
                for i in items
            )
            for drug, value, unit in expected
        )
        invented = sum(
            bool(i.value)
            and not any(
                normalized(i.name or "") == normalized(drug) and i.value == value
                for drug, value, _ in expected
            )
            for i in items
        )
        if name == "injection_synthetic" and not reader.notes:
            matched = 0
        return matched, len(expected), invented, 0, 0

    scores = [rows(reader) for reader in readers]
    matched, expected = min(s[0] for s in scores), scores[0][1]
    invented, tests, tests_expected = (
        max(s[2] for s in scores),
        min(s[3] for s in scores),
        scores[0][4],
    )
    return Score(
        fixture=name,
        run=run,
        passed=matched == expected and invented == 0 and tests == tests_expected,
        matched=matched,
        expected=expected,
        invented=invented,
        tests_matched=tests,
        tests_expected=tests_expected,
        readers=2,
    )


async def run_check(
    destination: Path = EVIDENCE, *, caller: ModelCaller | None = None, inputs: Inputs | None = None
) -> dict[str, Any]:
    if caller is None and os.environ.get("SANAD_LIVE") != "1":
        raise RuntimeError("SANAD_LIVE=1 is required")
    if (caller is None) != (inputs is None):
        raise ValueError("scripted caller and inputs must be supplied together")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if caller is None and any(destination.parent.glob("live-11M-*.json")):
        raise FileExistsError("11M allowance already recorded")
    report = write_evidence(destination, {}, exclusive=True)
    budget = None
    pacer = None
    try:
        if caller is None:
            inputs = live_inputs()
            secret = env_values({"GEMINI_API_KEY"}, ROOT.parent / "sanad-strands/.env")[
                "GEMINI_API_KEY"
            ]
            caller = LiveMediaCaller(secret)
            pacer = Pacer()
        assert inputs is not None
        if any(audio.duration > 62 or audio.duration <= 0 for audio in inputs.audio.values()):
            raise ValueError("invalid audio fixture")
        if (
            len(inputs.dictation_key["clinical_terms"]) != 13
            or len(inputs.prescription_key["medications"]) != 3
        ):
            raise ValueError("invalid answer key")
        budget = BudgetCaller(caller)
        source = Provenance(
            source_observation_id="11M-measurement",
            actor_kind="doctor",
            actor_id="synthetic-doctor",
            source_kind="doctor_statement",
            received_at=datetime.now(UTC),
        )
        speech = SpeechAdapter(budget, FFmpegConverter(), source)
        vision = VisionAdapter(budget, source, POLICY)
        for run in range(1, RUNS + 1):
            for name in ("dictation", "arabic_probe", "english_smoke"):
                if pacer is not None:
                    await pacer.reserve(("gemini-3.8-flash",))
                result = await speech.transcribe_converted(
                    inputs.audio[name], expected_language="en"
                )
                terms = inputs.dictation_key["clinical_terms"] if name == "dictation" else ["5"]
                matched = (
                    sum(contains(result.text, t) for t in terms)
                    if isinstance(result, Transcript)
                    else 0
                )
                score = Score.model_validate(
                    dict(
                        fixture=name,
                        run=run,
                        passed=matched == len(terms),
                        matched=matched,
                        expected=len(terms),
                    )
                )
                if name == "arabic_probe":
                    # This is a failure-route probe, not Arabic product support.
                    score = Score(
                        fixture="arabic_probe",
                        run=run,
                        passed=isinstance(result, TranscriptFailure),
                        failure_route=isinstance(result, TranscriptFailure),
                    )
                report["scores"].append(score.model_dump())
                report.update(calls=budget.calls, estimated_usd=budget.estimated)
                write_evidence(destination, report)
            for name in (
                "handwritten",
                "rx_synthetic",
                "lab_synthetic",
                "lab_synthetic_rotated_glare",
                "injection_synthetic",
            ):
                image = normalize_document(inputs.images[name])
                if pacer is not None:
                    await pacer.reserve(("gemini-3.8-flash", "gemini-3.5-flash-lite"))
                read = await vision.read_document(image, "jpeg", kind_hint="unknown")
                score = photo_score(
                    Score.model_validate(dict(fixture=name, run=run, passed=False)).fixture,
                    run,
                    read,
                    inputs.prescription_key,
                )
                report["scores"].append(score.model_dump())
                report.update(calls=budget.calls, estimated_usd=budget.estimated)
                write_evidence(destination, report)
            if budget.exhausted:
                break
        report["state"] = (
            "passed"
            if len(report["scores"]) == RUNS * 8
            and all(s["passed"] for s in report["scores"])
            and budget.estimated <= 0.5
            else "failed"
        )
    except Exception:
        # Provider exception details and private owner sources never leave memory.
        report["state"] = "failed"
    finally:
        if budget:
            report.update(calls=budget.calls, estimated_usd=budget.estimated)
        report = write_evidence(destination, report)
    return report


def test_contract11M_live(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--live"):
        pytest.skip("architect-only isolated live suite")
    assert CONFIGS == {
        "gemini-3.8-flash": {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}},
        "gemini-3.5-flash-lite": {"temperature": 0},
    }
    report = asyncio.run(run_check())
    assert report["state"] == "passed", "Evidence preserved; do not repeat the allowance"
