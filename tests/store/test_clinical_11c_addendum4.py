"""Missing requests cannot be confirmed away; duplicate labels add no fact."""

import copy
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate, response
from providers.rxnorm_fixture import RxNormFixture

from sanad.scribe.card import dictation_questions, render_card
from sanad.scribe.memory import memory_rows
from sanad.scribe.patients import panel
from sanad.scribe.proposal import ScribeCallback
from sanad.store._base import StoreBase
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld
from store.test_clinical_11c_addendum2 import fact
from store.test_scribe_voice_web import providers, voice

QUESTION = "سمعت إنك طلبت تحليل/فحص بس مش لاقيه في الكارت؛ قول لي إيه هو"
VALUE: dict[str, Any] = {
    "patient": {"name_as_spoken": "سامي اختبار"},
    "facts": [fact("أنجينا", "angina", "Complaint")],
}


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    value = ScribeWorld.create(store, clock)
    value.approve()
    return value


def missing(world: ScribeWorld, text: str, *, by_voice: bool = False) -> ScriptedModel:
    model = ScriptedModel(*(candidate(VALUE) for _ in range(4)))
    world.scribe.model_factory = lambda registry, role: model
    if by_voice:
        providers(world, text + " NUMBERS: 53 560 12.5 5 45")
        world.post(voice())
    else:
        world.post(update(APPLICANT, text, 10))
    return model


@pytest.mark.parametrize("by_voice", [False, True])
def test_real_transcript_missing_missions_retries_and_blocks_confirmation(
    world: ScribeWorld, by_voice: bool, caplog: pytest.LogCaptureFixture
) -> None:
    # The owner explicitly identified this dictation as a synthetic patient.
    path = Path(__file__).resolve().parents[2] / "lane/spikes/real_dictation_44s.transcript.json"
    source = json.loads(path.read_text())["text"].split("NUMBERS:")[0]
    retries: list[str] = []
    world.scribe.observe_retry = retries.append
    caplog.set_level(logging.INFO, logger="sanad.scribe.turn")
    model = missing(world, source, by_voice=by_voice)
    p = world.proposal
    assert len(model.script.calls) == 4
    assert model.script.calls[0] == model.script.calls[1] == model.script.calls[2]
    assert retries == ["request_missing", "request_missing"]
    assert render_card(p)[0].count(QUESTION) == 1
    assert dictation_questions(p).count(QUESTION) == 1 and p.blocked("all")
    with pytest.raises(AssertionError, match="card button missing"):
        world.button("✅ تمام")
    now = world.clock()
    token = ScribeCallback(
        id=p.confirmation_nonce_hash,
        scope=p.scope,
        proposal_id=p.id,
        proposal_version=p.version,
        actor_subject=APPLICANT,
        action="confirm",
        expires_at=p.expires_at,
        created_at=now,
        updated_at=now,
    )
    result = world.scribe.committer.confirm(p, token, world.actor(APPLICANT), "force-confirm")
    assert result.status == "clarification"
    assert not panel(world.store, world.doctor.scope)
    assert not memory_rows(world.store, world.doctor.scope)
    assert world.proposal == p
    assert "reason=request_missing" in caplog.text and source not in caplog.text


@pytest.mark.parametrize("cue", ["طلبت", "اعمل", "يعملوه", "تحليل", "أشعة", "إيكو", "TEST", "lab"])
def test_each_binding_request_cue_retries_and_keeps_one_block(world: ScribeWorld, cue: str) -> None:
    model = missing(world, "سامي اختبار أنجينا " + cue)
    assert len(model.script.calls) == 4
    assert world.proposal.blocked("all")
    assert render_card(world.proposal)[0].count(QUESTION) == 1


@pytest.mark.parametrize("kind", ["TEST", "VISIT", "TASK", "SEND_RECORDS"])
def test_primary_retains_each_supported_mission_kind(world: ScribeWorld, kind: str) -> None:
    complete = copy.deepcopy(VALUE)
    complete["missions"] = [{"kind": kind, "text": "BUN" if kind == "TEST" else "يراجع العيادة"}]
    model = ScriptedModel(candidate(complete), candidate(VALUE), candidate(VALUE))
    world.scribe.model_factory = lambda registry, role: model
    world.post(update(APPLICANT, "سامي اختبار أنجينا طلبت BUN ويراجع العيادة", 10))
    assert len(model.script.calls) == 3 and all(
        call == model.script.calls[0] for call in model.script.calls
    )
    assert len(world.proposal.candidate.missions) == 1
    assert not world.proposal.blocked("all") and QUESTION not in render_card(world.proposal)[0]
    world.tap()
    patient = panel(world.store, world.doctor.scope)[0]
    assert len(world.store.list_records(patient.scope, "mission")[0]) == 1


def test_no_cue_and_existing_mission_need_no_retry(world: ScribeWorld) -> None:
    first = world.dictate("سامي اختبار أنجينا laboratory testament", VALUE)
    assert QUESTION not in render_card(first)[0]
    complete = copy.deepcopy(VALUE)
    complete["missions"] = [{"kind": "TEST", "text": "BUN"}]
    complete["correction_edits"] = [
        {"item": "mission:new", "proposal_index": 0, "source_quote": "طلبت BUN"}
    ]
    second = world.dictate("طلبت BUN", complete, id=11)
    assert len(second.candidate.missions) == 1 and QUESTION not in render_card(second)[0]


def test_unrelated_correction_keeps_block_until_requested_mission_is_supplied(
    world: ScribeWorld,
) -> None:
    missing(world, "سامي اختبار أنجينا طلبت تحليل")
    first = world.proposal
    unchanged = first.candidate.model_dump()
    model = ScriptedModel(candidate(unchanged), candidate(unchanged))
    world.scribe.model_factory = lambda registry, role: model
    world.post(update(APPLICANT, "الأنجينا زي ما هي", 11))
    second = world.proposal
    assert second.id == first.id and second.version == first.version + 1
    assert second.blocked("all") and len(model.script.calls) == 2
    complete = second.candidate.model_dump()
    complete["missions"] = [{"kind": "TEST", "text": "BUN"}]
    complete["correction_edits"] = [
        {"item": "mission:new", "proposal_index": 0, "source_quote": "طلبت BUN"}
    ]
    third = world.dictate("طلبت BUN", complete, id=12)
    assert third.id == first.id and third.expires_at == first.expires_at
    assert not third.blocked("all") and QUESTION not in render_card(third)[0]
    world.tap()
    patient = panel(world.store, world.doctor.scope)[0]
    assert len(world.store.list_records(patient.scope, "mission")[0]) == 1


def test_schema_retry_and_missing_request_share_one_allowance(world: ScribeWorld) -> None:
    models = [
        ScriptedModel(response("malformed")),
        ScriptedModel(candidate(VALUE)),
        ScriptedModel(candidate(VALUE)),
        ScriptedModel(candidate(VALUE)),
    ]
    factories = iter(models)
    world.scribe.model_factory = lambda *_: next(factories)
    retries: list[str] = []
    world.scribe.observe_retry = retries.append
    world.post(update(APPLICANT, "سامي اختبار أنجينا طلبت تحليل", 10))
    assert sum(len(m.script.calls) for m in models) == 4 and retries == [
        "schema_validation",
        "request_missing",
    ]
    assert world.proposal.blocked("all") and QUESTION in render_card(world.proposal)[0]


def test_missing_request_retry_failure_keeps_the_valid_blocked_card(world: ScribeWorld) -> None:
    model = ScriptedModel(
        candidate(VALUE),
        candidate(VALUE),
        RuntimeError("private provider failure"),
        RuntimeError("private provider failure"),
    )
    world.scribe.model_factory = lambda registry, role: model
    world.post(update(APPLICANT, "سامي اختبار أنجينا طلبت تحليل", 10))
    assert len(model.script.calls) == 4
    assert world.proposal.blocked("all") and QUESTION in render_card(world.proposal)[0]


def test_missing_request_retry_does_not_extend_extraction_deadline(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sanad.scribe.turn as turn

    ticks = iter([0.0, 0.0, 15.0])
    monkeypatch.setattr(turn, "monotonic", lambda: next(ticks, 15.0))
    model = missing(world, "سامي اختبار أنجينا طلبت تحليل")
    assert len(model.script.calls) == 1
    assert world.proposal.blocked("all") and QUESTION in render_card(world.proposal)[0]


def test_missing_request_retry_shares_six_http_call_budget(world: ScribeWorld) -> None:
    fixture = RxNormFixture()
    world.scribe.rxnorm_client = fixture.client
    model = ScriptedModel(
        response(calls=[("lookup_drug", {"name": "Bisoprolol"})]),
        candidate(VALUE),
        response(calls=[("lookup_drug", {"name": "Amlodipine"})]),
        candidate(VALUE),
        candidate(VALUE),
        candidate(VALUE),
    )
    world.scribe.model_factory = lambda registry, role: model
    world.post(update(APPLICANT, "سامي اختبار أنجينا طلبت تحليل", 10))
    assert len(model.script.calls) == 6 and len(fixture.calls) == 6
    assert world.proposal.rxnorm_calls == 6 and world.proposal.blocked("all")


def test_unrequested_model_mission_cannot_clear_block_in_a_correction(world: ScribeWorld) -> None:
    missing(world, "سامي اختبار أنجينا طلبت تحليل")
    value = world.proposal.candidate.model_dump()
    value["missions"] = [{"kind": "TEST", "text": "BUN"}]
    model = ScriptedModel(candidate(value), candidate(value))
    world.scribe.model_factory = lambda registry, role: model
    world.post(update(APPLICANT, "الأنجينا زي ما هي", 11))
    assert len(model.script.calls) == 2
    assert not world.proposal.candidate.missions and world.proposal.blocked("all")
    assert QUESTION in render_card(world.proposal)[0]


def test_explicit_new_patient_does_not_inherit_missing_request(world: ScribeWorld) -> None:
    missing(world, "سامي اختبار أنجينا طلبت تحليل")
    first = world.proposal
    world.post(update(APPLICANT, "/new مريض آخر", 11))
    assert world.proposal.id != first.id
    assert QUESTION not in render_card(world.proposal)[0]
    old = world.store.get(first.scope, "scribe_proposal", first.id)
    assert old and old.body["status"] == "superseded"


@pytest.mark.parametrize("bare_first", [False, True])
def test_duplicate_bare_fact_is_removed_and_indices_stay_aligned(
    world: ScribeWorld, bare_first: bool, caplog: pytest.LogCaptureFixture
) -> None:
    bare = fact("ECG", "ECG")
    detail = fact("ECG تي أوف إنفرجين", None, "ECG")
    detail["terms"] = [
        {"spoken": "ECG", "english": "ECG"},
        {"spoken": "تي أوف إنفرجين", "english": "T wave inversion"},
    ]
    value = copy.deepcopy(VALUE)
    value["facts"] = [bare, detail] if bare_first else [detail, bare]
    caplog.set_level(logging.INFO, logger="sanad.scribe.merge")
    p = world.dictate("سامي اختبار ECG تي أوف إنفرجين", value)
    assert len(p.candidate.facts) == 1 and p.candidate.facts[0].text == detail["text"]
    assert "History: ECG" not in render_card(p)[0]
    assert "ECG: T wave inversion" in render_card(p)[0]
    assert {n.item for n in p.names} == {"fact:0"}
    assert sum("scribe_overlapping_facts_dropped count=1" in r.message for r in caplog.records) == 1
    world.tap()
    patient = panel(world.store, world.doctor.scope)[0]
    saved = world.store.list_records(patient.scope, "clinical_fact")[0]
    assert len(saved) == 1
    payload = saved[0].body["payload"]
    assert isinstance(payload, dict) and payload["text"] == detail["text"]


@pytest.mark.parametrize("bare", ["ECG", "ECG طبيعي", "45", "99"])
def test_nonduplicate_substantive_and_numeric_facts_are_never_removed(
    world: ScribeWorld, bare: str
) -> None:
    # A fabricated/unanchored term cannot delete a real separate fact.
    value = copy.deepcopy(VALUE)
    value["facts"] = [fact(bare, None), fact("أنجينا", None)]
    value["facts"][1]["terms"] = [{"spoken": bare, "english": bare}]
    p = world.dictate("سامي اختبار ECG طبيعي 45 أنجينا", value)
    assert len(p.candidate.facts) == 2
    if bare == "99":
        assert any(i.code == "unsupported_number" and i.blocked for i in p.issues)
