"""English media prompts, scoped hints and unchanged honest failure routes."""

import asyncio
import json
import time
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from domain_fixtures import NOW
from harness import FakeClock
from store import evidence_fixtures as evidence
from store import photo_fixtures as photos
from store.concierge_fixtures import PatientWorld

from providers.fixtures import (
    SOURCE,
    ScriptedConverter,
    ScriptedSpeech,
    ScriptedVision,
    document,
    png,
)
from sanad.concierge.answer import SYSTEM_PROMPT as PROMPT
from sanad.domain import PatientScope, TenantScope
from sanad.evidence.context_names import active_drug_names
from sanad.media.audio import ConvertedAudio
from sanad.media.speech import ENGLISH_VERBATIM_PROMPT, SpeechAdapter, TranscriptFailure
from sanad.media.vision import (
    JSON_NUDGE,
    VISION_PROMPT,
    DocumentFailure,
    DocumentRead,
    VisionAdapter,
)
from sanad.models.io import ModelUnavailable
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.memory import NameVocabulary
from sanad.scribe.records import CareOrderHead, CareOrderVersion
from sanad.store.memory import MemoryStore
from sanad.store.protocol import Store
from sanad.store.records import to_record


def hints(content: list[dict[str, Any]]) -> list[str]:
    return cast(list[str], json.loads(content[1]["text"].split("instructions):\n")[1]))


def test_english_prompts_and_bounded_separate_names() -> None:
    assert all(ord(c) < 128 for c in VISION_PROMPT + JSON_NUDGE + ENGLISH_VERBATIM_PROMPT + PROMPT)
    assert "[unreadable]" in VISION_PROMPT and "untrusted data" in VISION_PROMPT
    assert "NUMBERS:" in ENGLISH_VERBATIM_PROMPT
    assert "This question needs your doctor" in PROMPT
    assert "Names that may appear" not in VISION_PROMPT
    caller = ScriptedVision(document(), document())
    names = ("PatientDrug", "PatientDrug", " DoctorDrug ", *tuple(f"Drug{i}" for i in range(250)))
    read = asyncio.run(
        VisionAdapter(caller, SOURCE, POLICY).read_document(
            png(), "png", kind_hint="lab", context_names=names
        )
    )
    assert isinstance(read, DocumentRead) and not read.single_reader
    assert caller.calls[0][0] != caller.calls[1][0]
    for _, content in caller.calls:
        sent = hints(content)
        assert sent[:2] == ["PatientDrug", "DoctorDrug"] and len(sent) == len(set(sent)) == 200


@pytest.mark.parametrize("reason", ["timeout", "unavailable"])
def test_gemini_failures_keep_existing_routes(reason: str) -> None:
    failure = ModelUnavailable(reason=cast(Any, reason))
    speech = ScriptedSpeech(failure)
    result = asyncio.run(
        SpeechAdapter(speech, ScriptedConverter(), SOURCE).transcribe_converted(
            ConvertedAudio(data=b"ID3synthetic", duration=15), expected_language="en"
        )
    )
    assert isinstance(result, TranscriptFailure) and result.reason == reason
    caller = ScriptedVision(failure, failure, failure, failure)
    read = asyncio.run(
        VisionAdapter(caller, SOURCE, POLICY).read_document(png(), "png", kind_hint="lab")
    )
    assert isinstance(read, DocumentFailure) and read.reason == reason
    assert len(caller.calls) == 4
    assert {model for model, _ in caller.calls} == {"gemini-3.8-flash", "gemini-3.5-flash-lite"}


def test_names_helper_paginated_current_only_and_scoped() -> None:
    scope = PatientScope(doctor_id="synthetic-doctor", patient_id="synthetic-patient")
    versions = {}
    heads = []
    for i, status in enumerate(("active", "stopped", "superseded", "active", "active")):
        ident = f"order{i}"
        head = CareOrderHead(
            id=ident,
            scope=scope,
            order_id=ident,
            version=1,
            created_at=NOW,
            updated_at=NOW,
            current_order_version=2,
            current_version_id=ident + ":2",
            type="medication",
            name="stale-head-name",
            status=cast(Any, status),
            changed_by="synthetic-doctor",
            changed_at=NOW,
        )
        order = CareOrderVersion(
            id=ident + ":2",
            scope=scope,
            order_id=ident,
            order_version=2,
            version=1,
            created_at=NOW,
            updated_at=NOW,
            type="medication",
            structured_instruction=OrderCandidate(
                action="start",
                drug="Current" + str(i),
                dose="private-dose",
                frequency="private-frequency",
            ),
            provenance=SOURCE,
            confirmed_by="synthetic-doctor",
            confirmed_at=NOW,
        )
        heads.append(to_record(head, scope))
        if i != 3:
            versions[order.id] = to_record(order, scope)
    calls = []

    class Pages:
        def list_records(self, actual: Any, kind: str, cursor: Any = None) -> Any:
            assert actual == scope and kind == "care_order_head"
            index = int(cursor or 0)
            calls.append(index)
            return [heads[index]], str(index + 1) if index + 1 < len(heads) else None

        def get(self, actual: Any, kind: str, ident: str) -> Any:
            assert actual == scope and kind == "care_order_version"
            return versions.get(ident)

    store = cast(Store, Pages())
    assert active_drug_names(store, scope, 200) == ("Current0", "Current4")
    assert calls == list(range(5))
    calls.clear()
    assert active_drug_names(store, scope, 1) == ("Current0",) and calls == [0]
    assert active_drug_names(store, scope, 0) == ()
    with pytest.raises(ValueError, match="patient scope"):
        active_drug_names(store, cast(Any, TenantScope(doctor_id=scope.doctor_id)), 200)


def world() -> PatientWorld:
    clock = FakeClock(NOW)
    value = cast(PatientWorld, PatientWorld.create(MemoryStore(clock=clock), clock))
    value.enroll()
    return value


def test_patient_photo_passes_orders_then_vocabulary_and_reuses_checkpoint() -> None:
    value = world()
    caller, _, _ = evidence.providers(value)
    evidence.upload(value)
    names = active_drug_names(value.store, value.patient_scope, 200)
    assert names
    for _, content in caller.calls:
        assert hints(content)[: len(names)] == list(names)
        assert len(hints(content)) <= 200
    before = len(caller.calls)
    evidence.upload(value)
    assert len(caller.calls) == before


@pytest.mark.parametrize("known", [True, False])
def test_doctor_photo_only_uses_patient_context_when_already_selected(
    monkeypatch: pytest.MonkeyPatch, known: bool
) -> None:
    value = world()
    if known:
        previous = value.dictate(
            "Synthetic Patient حساسية بنسلين",
            {
                "patient": {"name_as_spoken": "Synthetic Patient"},
                "facts": [{"category": "allergy", "text": "بنسلين"}],
            },
            id=410,
        )
        assert previous.selected_patient_id == value.patient_scope.patient_id
    scopes = []
    original = active_drug_names

    def collect(store: Store, scope: PatientScope, limit: int) -> tuple[str, ...]:
        scopes.append(scope)
        return ("PatientOnlyDrug", *original(store, scope, limit))

    monkeypatch.setattr("sanad.evidence.context_names.active_drug_names", collect)
    monkeypatch.setattr(NameVocabulary, "hint", lambda self: "DoctorOnlyDrug, PatientOnlyDrug")
    caller, _, _ = photos.providers(value, photos.prescription(), photos.prescription())
    value.post(photos.photo("", id=420))
    assert len(caller.calls) == 2
    assert scopes == ([value.patient_scope] if known else [])
    assert hints(caller.calls[0][1])[0] == ("PatientOnlyDrug" if known else "DoctorOnlyDrug")
    if known:
        assert value.proposal.selected_patient_id == value.patient_scope.patient_id


@pytest.mark.parametrize("last_value", ["4.1", "4.2"])
def test_entire_11m_harness_with_scripted_providers_and_private_evidence(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, last_value: str
) -> None:
    from live import check11M as check

    reserve = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr(check.Pacer, "reserve", reserve)
    monkeypatch.setattr(check.Pacer, "sleep", sleep)

    terms = [f"Term{i}" for i in range(13)]
    medications = [{"drug": f"SyntheticDrug{i}", "dose": str(i + 1)} for i in range(3)]
    key = {"medications": medications, "tests": ["SyntheticTest"]}
    hand = document(
        items=[
            *({"name": d["drug"], "dose": d["dose"]} for d in medications),
            {"name": "SyntheticTest"},
        ]
    )
    rx = document(items=[{"name": drug, "dose": dose} for drug, dose in check.RX_ROWS])
    lab = document(
        items=[{"name": drug, "value": value, "unit": unit} for drug, value, unit in check.LAB_ROWS]
    )
    glare = document(
        items=[{"name": "Potassium", "value": "6.3"}, {"name": "Creatinine", "value": "2.4"}]
    )
    injection = document(
        items=[{"name": "Potassium", "value": "4.1", "unit": "mmol/L"}],
        notes=["untrusted image instruction"],
    )
    scripts: list[str | ModelUnavailable] = []
    for _ in range(2):
        scripts.extend(
            [
                " ".join(terms),
                ModelUnavailable(reason="unavailable"),
                "5 NUMBERS: 5",
                hand,
                hand,
                rx,
                rx,
                lab,
                lab,
                glare,
                glare,
                injection,
                injection,
            ]
        )
    scripts[-1] = document(
        items=[{"name": "Potassium", "value": last_value, "unit": "mmol/L"}],
        notes=["untrusted image instruction"],
    )
    caller = ScriptedSpeech(*scripts)
    inputs = check.Inputs(
        {
            name: ConvertedAudio(data=b"ID3synthetic", duration=15)
            for name in ("dictation", "arabic_probe", "english_smoke")
        },
        {
            name: png()
            for name in (
                "handwritten",
                "rx_synthetic",
                "lab_synthetic",
                "lab_synthetic_rotated_glare",
                "injection_synthetic",
            )
        },
        {"clinical_terms": terms},
        key,
    )
    path = tmp_path / "live-11M-synthetic.json"
    started = time.monotonic()
    report = asyncio.run(check.run_check(path, caller=caller, inputs=inputs))
    assert time.monotonic() - started < 1.0
    reserve.assert_not_awaited()
    sleep.assert_not_awaited()
    assert report["pace_seconds"] == check.PACE_SECONDS
    assert report["state"] == ("passed" if last_value == "4.1" else "failed")
    assert len(report["scores"]) == 16
    assert [score["run"] for score in report["scores"]] == [1] * 8 + [2] * 8
    assert sum(call["model_id"] == "gemini-3.8-flash" for call in report["calls"]) == 16
    assert sum(call["model_id"] == "gemini-3.5-flash-lite" for call in report["calls"]) == 10
    assert len(caller.calls) == len(report["calls"]) == 26
    assert [row["status"] for row in report["calls"]].count("ok") == 24
    assert [row["status"] for row in report["calls"]].count("unavailable") == 2
    assert report["estimated_usd"] <= 0.5
    serialized = path.read_text()
    assert all(term not in serialized for term in (*terms, "SyntheticDrug", "untrusted image"))
    with pytest.raises(FileExistsError):
        asyncio.run(check.run_check(path, caller=caller, inputs=inputs))
    assert len(caller.calls) == 26
    monkeypatch.setenv("SANAD_LIVE", "1")
    with pytest.raises(FileExistsError, match="already recorded"):
        asyncio.run(check.run_check(tmp_path / "live-11M-next-date.json"))
    safe = check.write_evidence(
        tmp_path / "allowlist.json",
        {
            "transcript": "private-marker",
            "GEMINI_API_KEY": "private-marker",
            "scores": [dict(fixture="dictation", run=1, passed=False, raw="private-marker")],
            "calls": [
                dict(
                    model_id="gemini-3.8-flash",
                    status="ok",
                    latency_ms=1.0,
                    input_tokens=2,
                    output_tokens=3,
                    usage_known=True,
                    estimated_usd=0.001,
                    response="private-marker",
                )
            ],
        },
    )
    assert "private-marker" not in json.dumps(safe)


def test_11m_budget_fails_before_transport_and_keeps_unknown_usage() -> None:
    from live.check11M import BudgetCaller

    fake = ScriptedSpeech(ModelUnavailable(reason="timeout"))
    caller = BudgetCaller(fake, estimated=0.49)
    content: list[dict[str, Any]] = [
        {"audio": {"format": "mp3", "source": {"bytes": b"ID3synthetic"}}},
        {"text": "test"},
    ]
    result = asyncio.run(caller.call("gemini-3.8-flash", content))
    assert isinstance(result, ModelUnavailable) and result.reason == "budget_exhausted"
    assert not fake.calls
    caller = BudgetCaller(fake)
    asyncio.run(caller.call("gemini-3.8-flash", content))
    assert caller.estimated > 0 and not caller.calls[0]["usage_known"]

    class Cancelled(ScriptedSpeech):
        async def call(
            self, model_id: str, content: list[dict[str, Any]], *, max_tokens: int = 2048
        ) -> Any:
            raise asyncio.CancelledError

    cancelled = BudgetCaller(Cancelled())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled.call("gemini-3.8-flash", content))
    assert cancelled.estimated > 0 and len(cancelled.calls) == 1
    assert not cancelled.calls[0]["usage_known"]


def test_11m_oracle_rejects_wrong_drugs_values_and_inventions() -> None:
    from live.check11M import photo_score

    caller = ScriptedVision(document(), document(items=[{"name": "InventedDrug", "dose": "5"}]))
    read = asyncio.run(
        VisionAdapter(caller, SOURCE, POLICY).read_document(png(), "png", kind_hint="lab")
    )
    score = photo_score(
        "handwritten",
        1,
        read,
        {"medications": [{"drug": "ExpectedDrug", "dose": "5"}], "tests": []},
    )
    assert not score.passed and score.invented > 0
    assert not photo_score("lab_synthetic", 1, read, {}).passed


def test_ssm_smoke_uses_decryption_and_prints_only_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from deploy import smoke

    captured = []

    class SSM:
        def get_parameters(self, **kwargs: Any) -> Any:
            captured.append(kwargs)
            return {
                "Parameters": [
                    {"Name": "/sanad/dev/gemini_api_key", "Value": "synthetic-smoke-secret"}
                ]
            }

    class AWS:
        def client(self, name: str, **kwargs: Any) -> Any:
            assert name == "ssm"
            return SSM()

    caller = ScriptedSpeech("Synthetic 5 NUMBERS: 5")

    def factory(policy: str, secret: str, **kwargs: Any) -> Any:
        assert secret == "synthetic-smoke-secret" and kwargs["timeout"] == 30
        return caller

    monkeypatch.setattr("sanad.models.gemini.GeminiCaller", factory)
    monkeypatch.setattr(smoke, "synthetic_english_clip", lambda: b"ID3synthetic")
    result = smoke.gemini(AWS(), "dev")
    assert captured == [{"Names": ["/sanad/dev/gemini_api_key"], "WithDecryption": True}]
    assert result == {"ok": True, "latency_ms": 1.0, "input_tokens": 100, "output_tokens": 25}
    assert capsys.readouterr().out == ""
    assert "synthetic-smoke-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "label",
    ["x-goog-api-key: ", "GEMINI_API_KEY=", "key=", '"x-goog-api-key": "', "'GEMINI_API_KEY': '"],
)
def test_operator_redacts_gemini_credentials(label: str) -> None:
    from deploy.ops import redact_log

    assert "synthetic-private-marker" not in redact_log(label + "synthetic-private-marker")


def test_11m_pacer_reserves_models_before_adapter_deadlines() -> None:
    from live.check11M import Pacer

    now = 0.0
    waits = []

    async def sleep(delay: float) -> None:
        nonlocal now
        waits.append(delay)
        now += delay

    pacer = Pacer(clock=lambda: now, sleep=sleep)

    async def schedule() -> None:
        starts = []
        for _ in range(3):
            await pacer.reserve(("gemini-3.8-flash",))
            starts.append(now)
        assert starts == [0.0, 12.0, 24.0]
        await pacer.reserve(("gemini-3.5-flash-lite",))
        assert now == 24.0
        await pacer.reserve(("gemini-3.8-flash", "gemini-3.5-flash-lite"))
        assert now == 36.0
        assert pacer.last_start == {"gemini-3.8-flash": 36.0, "gemini-3.5-flash-lite": 36.0}
        await pacer.reserve(("gemini-3.5-flash-lite",))
        assert now == 42.0

    asyncio.run(schedule())
    assert waits == [12.0, 12.0, 12.0, 6.0]


@pytest.mark.parametrize(
    ("text", "term", "expected"),
    [
        ("150/90", "150 over 90", True),
        ("follow up", "blood pressure follow-up", True),
        ("3 times a day for 5 days", "three times a day for five days", True),
        ("creatinine", "creat", True),
        ("creat.", "creat", True),
        ("creatine", "creatinine", False),
        ("follow up", "follow-up blood", False),
        ("BUN.", "BUN", True),
        ("BUNny", "BUN", False),
        ("precreatinine", "creat", False),
        ("150/900", "150 over 90", False),
        ("15", "5", False),
        ("stone", "one", False),
        ("THREE times a day for FIVE days", "3 times a day for 5 days", True),
        ("150 over 90", "150/90", True),
        ("follow-up", "follow up", True),
    ],
)
def test_11m_term_oracle_canonicalizes_both_sides(text: str, term: str, expected: bool) -> None:
    from live.check11M import contains

    assert contains(text, term) is expected


@pytest.mark.parametrize(
    ("digit", "word"),
    list(
        enumerate(
            "one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
            "fifteen sixteen seventeen eighteen nineteen twenty".split(),
            start=1,
        )
    ),
)
def test_11m_term_oracle_number_words(digit: int, word: str) -> None:
    from live.check11M import contains

    assert contains(str(digit), word)
    assert contains(word, str(digit))


@pytest.mark.parametrize(
    ("name", "dose", "matched", "invented"),
    [
        ("Concor 5", None, 1, 0),
        ("Concor 5", "", 1, 0),
        ("Concor 5", "5", 1, 0),
        ("Concor 5", "10", 0, 0),
        ("Coversyl", "5", 0, 1),
        ("Coversyl 5", None, 0, 1),
    ],
)
def test_11m_photo_oracle_splits_names_and_preserves_explicit_doses(
    name: str, dose: str | None, matched: int, invented: int
) -> None:
    from live.check11M import photo_score

    first = document(items=[{"name": "Concor", "dose": "5"}])
    second = document(items=[{"name": name, "dose": dose}])
    read = asyncio.run(
        VisionAdapter(ScriptedVision(first, second), SOURCE, POLICY).read_document(
            png(), "png", kind_hint="prescription"
        )
    )
    assert isinstance(read, DocumentRead)
    before = read.model_dump()
    score = photo_score(
        "handwritten", 1, read, {"medications": [{"drug": "Concor", "dose": "5"}], "tests": []}
    )
    assert (score.matched, score.invented) == (matched, invented)
    assert score.passed is (matched == 1 and invented == 0)
    assert read.model_dump() == before


def test_11m_score_refuses_a_third_run() -> None:
    from live.check11M import Score
    from pydantic import ValidationError

    assert Score(fixture="dictation", run=2, passed=True).run == 2
    with pytest.raises(ValidationError):
        Score(fixture="dictation", run=3, passed=True)


@pytest.mark.parametrize(
    ("size", "accepted"),
    [((4032, 3024), True), ((3024, 4032), True), ((5000, 4000), True), ((5000, 5000), False)],
)
def test_11m_budget_uses_product_image_limit(
    size: tuple[int, int], accepted: bool, tmp_path: Any
) -> None:
    from io import BytesIO

    from live.check11M import BudgetCaller, write_evidence
    from PIL import Image

    from sanad.models.io import ModelReply

    output = BytesIO()
    with Image.new("RGB", size, "white") as image:
        image.save(output, format="JPEG")
    data = output.getvalue()
    content = [{"image": {"format": "jpeg", "source": {"bytes": data}}}]
    fake = ScriptedSpeech("private-response-marker")
    budget = BudgetCaller(fake)
    result = asyncio.run(budget.call("gemini-3.8-flash", content))
    assert isinstance(result, ModelReply) is accepted
    assert len(fake.calls) == int(accepted)
    if accepted:
        assert fake.calls[0][1] == content
        assert budget.calls[0]["status"] == "ok"
    else:
        assert isinstance(result, ModelUnavailable) and result.reason == "budget_exhausted"
        assert budget.estimated == 0
        assert budget.calls == [
            dict(
                model_id="gemini-3.8-flash",
                status="refused",
                latency_ms=0.0,
                input_tokens=0,
                output_tokens=0,
                usage_known=True,
                estimated_usd=0.0,
            )
        ]
    path = tmp_path / "image-evidence.json"
    safe = write_evidence(path, {"calls": budget.calls})
    assert len(safe["calls"]) == 1
    assert "private-response-marker" not in path.read_text()


@pytest.mark.parametrize(
    "guard",
    ["model", "tokens", "prompt", "media", "call_count", "image", "audio", "spend", "exhausted"],
)
def test_11m_budget_records_every_guard_refusal(guard: str, tmp_path: Any) -> None:
    from live.check11M import BudgetCaller, write_evidence

    fake = ScriptedSpeech()
    budget = BudgetCaller(fake)
    model = "gemini-3.8-flash"
    tokens = 2048
    content: list[dict[str, Any]] = [{"audio": {"source": {"bytes": b"synthetic"}}}]
    if guard == "model":
        model = "private-model-marker"
    elif guard == "tokens":
        tokens = 2049
    elif guard == "prompt":
        content.append({"text": "private-prompt-marker" * 1000})
    elif guard == "media":
        content = []
    elif guard == "call_count":
        for _ in range(115):
            budget.refuse(model)
    elif guard == "image":
        content = [{"image": {"source": {"bytes": b"private-image-marker"}}}]
    elif guard == "audio":
        content = [{"audio": {"source": {"bytes": b"x" * (2 * 1024 * 1024 + 1)}}}]
    elif guard == "spend":
        budget.estimated = 0.49
    else:
        budget.exhausted = True
    before = budget.estimated
    count = len(budget.calls)
    result = asyncio.run(budget.call(model, content, max_tokens=tokens))
    assert isinstance(result, ModelUnavailable) and result.reason == "budget_exhausted"
    assert not fake.calls and budget.estimated == before
    assert len(budget.calls) == count + 1
    row = budget.calls[-1]
    assert row["status"] == "refused"
    assert (
        row["latency_ms"]
        == row["input_tokens"]
        == row["output_tokens"]
        == row["estimated_usd"]
        == 0
    )
    path = tmp_path / "refused.json"
    write_evidence(path, {"calls": budget.calls})
    assert "private-" not in path.read_text()


@pytest.mark.parametrize("status", ["ok", "unavailable", "timeout", "invalid"])
def test_11m_budget_records_caller_metadata_status(status: Any, tmp_path: Any) -> None:
    from live.check11M import BudgetCaller, write_evidence

    from sanad.models.io import CallMetadata, ModelReply

    meta = CallMetadata(
        model_id="gemini-3.8-flash",
        policy_version="private-policy-marker",
        latency_ms=1.0,
        status=status,
        usage_known=False,
    )
    # Deliberately disagree with reason to prove metadata supplies the status.
    result = ModelUnavailable(reason="unavailable", metadata=(meta,))

    class Caller:
        async def call(
            self, model_id: str, content: list[dict[str, Any]], *, max_tokens: int = 2048
        ) -> ModelReply | ModelUnavailable:
            return (
                ModelReply(text="private-response-marker", metadata=meta)
                if status == "ok"
                else result
            )

    budget = BudgetCaller(Caller())
    asyncio.run(budget.call("gemini-3.8-flash", [{"audio": {"source": {"bytes": b"synthetic"}}}]))
    assert len(budget.calls) == 1
    assert budget.calls[0]["status"] == ("unavailable" if status == "invalid" else status)
    assert budget.estimated == pytest.approx((30000 * 0.75 + 2048 * 3.75) / 1_000_000)
    path = tmp_path / "status.json"
    write_evidence(path, {"calls": budget.calls})
    assert "private-" not in path.read_text()


@pytest.mark.parametrize("reason", ["timeout", "unavailable"])
def test_11m_budget_records_failure_without_metadata(reason: Any) -> None:
    from live.check11M import BudgetCaller

    budget = BudgetCaller(ScriptedSpeech(ModelUnavailable(reason=reason)))
    asyncio.run(budget.call("gemini-3.8-flash", [{"audio": {"source": {"bytes": b"synthetic"}}}]))
    assert budget.calls[0]["status"] == reason


def test_11m_evidence_rejects_unallowlisted_status(tmp_path: Any) -> None:
    from live.check11M import BudgetCaller, write_evidence
    from pydantic import ValidationError

    budget = BudgetCaller(ScriptedSpeech())
    budget.refuse("gemini-3.8-flash")
    budget.calls[0]["status"] = "private-error-marker"
    path = tmp_path / "invalid.json"
    with pytest.raises(ValidationError):
        write_evidence(path, {"calls": budget.calls})
    assert not path.exists()
