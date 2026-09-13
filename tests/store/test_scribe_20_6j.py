"""6j F25/F23: store outcomes and legacy refusal, not clinical-card impressions."""

import logging
from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest
from providers.fixtures import ScriptedModel, candidate
from scribe.test_walkthrough_20_6d import FIRST
from scribe.test_walkthrough_20_6h import value

from sanad.domain import PatientScope
from sanad.scribe.card import render_card
from sanad.scribe.extract import DictationCandidate, FactCandidate, OrderCandidate, candidate_issues
from sanad.scribe.grounding import instruction_fact_blocks, seal
from sanad.scribe.proposal import Proposal, ScribeCallback, card_actions
from sanad.scribe.records import CareOrderVersion
from sanad.scribe.resolver import Context
from sanad.scribe.terms import (
    INSTRUCTION_EXCLUSIONS,
    drop_instruction_fact,
    instruction_content,
    instruction_tokens,
    instruction_vocabulary,
)
from sanad.store._base import StoreBase
from sanad.store.records import from_record
from store.account_fixtures import APPLICANT, update
from store.conftest import Clock
from store.scribe_fixtures import ScribeWorld

MONA = "Mona Test, start Exforge 5/160 once daily and request CBC and potassium."


def run(world: ScribeWorld, source: str, readings: list[dict[str, Any]], id: int = 10) -> Proposal:
    model = ScriptedModel(*(candidate(reading) for reading in readings))
    world.scribe.model_factory = lambda *_: model
    world.post(update(APPLICANT, source, id))
    assert world.receipt(id).state == "completed"
    return world.proposal


def persisted(world: ScribeWorld, proposal: Proposal) -> tuple[PatientScope, list[Any]]:
    world.tap("✅ Confirm")
    row = world.store.get(world.doctor.scope, "scribe_proposal", proposal.id)
    assert row and from_record(row, Proposal).status == "confirmed"
    patients, _ = world.store.list_patients(world.doctor.scope)
    assert len(patients) == 1
    scope = PatientScope(doctor_id=world.doctor.id, patient_id=patients[0].id)
    facts, _ = world.store.list_records(scope, "clinical_fact")
    return scope, list(facts)


def intact_plan(store: StoreBase, scope: PatientScope, proposal: Proposal) -> None:
    versions, _ = store.list_records(scope, "care_order_version")
    instructions = [from_record(row, CareOrderVersion) for row in versions]
    assert all(isinstance(row.structured_instruction, OrderCandidate) for row in instructions)
    expected: set[tuple[str, str | None, str]] = {("Exforge", "5/160", "start")}
    if proposal.source_text == FIRST:
        expected.add(("Aspirin", None, "stop"))
    assert {
        (
            row.structured_instruction.drug,
            row.structured_instruction.dose,
            row.structured_instruction.action,
        )
        for row in instructions
        if isinstance(row.structured_instruction, OrderCandidate)
    } == expected
    assert all(
        row.provenance.source_observation_id == proposal.source_receipt_id for row in instructions
    )
    missions, _ = store.list_records(scope, "mission")
    tests = [row for row in missions if row.body["kind"] == "TEST"]
    assert len(tests) == 1 and isinstance(tests[0].body["details"], dict)
    assert tests[0].body["details"]["analytes"] == ["CBC", "K"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "source,fact_text,category",
    [
        (MONA, "request CBC and potassium", "condition"),
        (FIRST, "hold the aspirin for now", "history"),
        (FIRST, "HDL, aspirin, for, now", "history"),
        (FIRST, "check, his, pressure, twice, a, day, for, three, days", "history"),
    ],
)
def test_f25_instruction_facts_never_persist(
    store: StoreBase,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    reverse: bool,
    source: str,
    fact_text: str,
    category: str,
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    reading = (
        value("Ahmed Test")
        if source == FIRST
        else {
            "patient": {"name_as_spoken": "Mona Test"},
            "orders": [
                {
                    "action": "start",
                    "action_quote": "start",
                    "drug": "Exforge",
                    "dose": "5/160",
                    "frequency": "once daily",
                }
            ],
            "missions": [{"kind": "TEST", "text": "CBC and potassium"}],
        }
    )
    reading["facts"] = [{"category": category, "text": fact_text}]
    peer = deepcopy(reading)
    peer["facts"] = []
    with caplog.at_level(logging.INFO):
        p = run(world, source, [peer, reading] if reverse else [reading, peer])
    assert not any(f.text == fact_text for f in p.candidate.facts)
    assert f"scribe_turn dropped_facts={0 if reverse else 1}" in caplog.text
    assert "fact_instruction" not in "\n".join(render_card(p))
    scope, facts = persisted(world, p)
    intact_plan(store, scope, p)
    assert all(f.body["payload"]["text"] != fact_text for f in facts)
    assert not [f for f in facts if f.body["category"] in {"condition", "history"}]
    missions, _ = store.list_records(scope, "mission")
    assert any(m.body["kind"] == "MEDICATION" for m in missions)
    assert any(m.body["kind"] == "TEST" for m in missions)
    if source == FIRST:
        assert sorted(str(m.body["kind"]) for m in missions) == [
            "MEDICATION",
            "MEDICATION",
            "MONITOR",
            "TEST",
        ]


@pytest.mark.parametrize(
    "category,text",
    [
        ("condition", "known hypertensive and diabetic"),
        ("history", "history of MI in 2019"),
        ("finding", "potassium 4.2"),
        ("condition", "known hypertensive"),
        ("condition", "known diabetic"),
        ("condition", "عنده سكر"),
        ("condition", "عنده ضغط"),
        ("condition", "known high blood pressure"),
        ("history", "pregnancy"),
        ("history", "ca breast 2019"),
        ("condition", "pressure"),
        ("condition", "عنده ضغط من زمان"),
        ("condition", "ضغطه سكر"),
        ("condition", "ضغط مزمن"),
    ],
)
@pytest.mark.usefixtures("legacy_dictation_schema")
def test_f25_genuine_facts_survive(
    store: StoreBase,
    clock: Clock,
    category: str,
    text: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    source = f"Ahmed Test, {text}, start Exforge 5/160 once daily."
    if text == "known diabetic":
        source = "Ahmed Test, known diabetic, request HbA1c"
    if text in {"ضغطه سكر", "ضغط مزمن"}:
        source = f"Ahmed Test {text} وعايز CBC وابدأ Exforge 5/160"
    reading = {
        "patient": {"name_as_spoken": "Ahmed Test"},
        "facts": [{"category": category, "text": text}],
    }
    if text == "known diabetic":
        reading["missions"] = [{"kind": "TEST", "text": "HbA1c"}]
    else:
        reading["orders"] = [
            {
                "action": "start",
                "action_quote": "start" if source.isascii() else "ابدأ",
                "drug": "Exforge",
                "dose": "5/160",
            }
        ]
        if text in {"ضغطه سكر", "ضغط مزمن"}:
            reading["missions"] = [{"kind": "TEST", "text": "CBC"}]
    with caplog.at_level(logging.INFO):
        p = run(world, source, [reading, reading])
    assert "scribe_turn dropped_facts=0" in caplog.text
    assert not instruction_fact_blocks(p)
    assert any(f.text == text for f in p.candidate.facts)
    _, facts = persisted(world, p)
    assert len(facts) == 1 and facts[0].body["category"] == category
    assert facts[0].body["payload"]["text"] == text


@pytest.mark.parametrize("sealed", [False, True])
def test_f25_legacy_or_stale_fact_refuses_before_reseal_without_writes(
    store: StoreBase,
    clock: Clock,
    sealed: bool,
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    reading = {
        "patient": {"name_as_spoken": "Mona Test"},
        "missions": [{"kind": "TEST", "text": "CBC and potassium"}],
    }
    p = run(world, MONA, [reading, reading])
    bad = p.model_copy(
        update={
            "candidate": p.candidate.model_copy(
                update={
                    "facts": (
                        FactCandidate(category="condition", text="request CBC and potassium"),
                    )
                }
            ),
            "evidence_fingerprint": p.evidence_fingerprint if sealed else "",
        }
    )
    token = ScribeCallback(
        id=p.confirmation_nonce_hash,
        scope=p.scope,
        proposal_id=p.id,
        proposal_version=p.version,
        actor_subject=APPLICANT,
        action="confirm",
        expires_at=p.expires_at,
        created_at=clock(),
        updated_at=clock(),
    )
    actor = world.actor(APPLICANT)
    writes = []
    original = store._atomic

    def capture(*args: Any, **kwargs: Any) -> bool:
        writes.append(True)
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "_atomic", capture)
        result = world.scribe.committer.confirm(bad, token, actor, "bad-confirm")
    assert result.template == "scribe_stale"
    assert writes == []
    assert store.list_patients(world.doctor.scope)[0] == ()


def test_f25_vocabulary_segment_block_and_seal_idempotence(store: StoreBase, clock: Clock) -> None:
    assert not instruction_vocabulary() & instruction_tokens(INSTRUCTION_EXCLUSIONS)
    empty = DictationCandidate()
    for text in ("request, CBC, K", "HDL, aspirin, for, now"):
        assert instruction_content(text, empty)
    for text in ("known hypertensive and diabetic", "history of MI in 2019"):
        assert not instruction_content(text, empty)
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    source = "Ahmed Test start Exforge 5/160 request CBC for his diabetes"
    reading = {
        "patient": {"name_as_spoken": "Ahmed Test"},
        "facts": [{"category": "condition", "text": "request CBC for his diabetes"}],
        "orders": [
            {"action": "start", "action_quote": "start", "drug": "Exforge", "dose": "5/160"}
        ],
        "missions": [{"kind": "TEST", "text": "CBC"}],
    }
    p = run(world, source, [reading, reading])
    assert not drop_instruction_fact(p.candidate.facts[0], p.candidate, source)
    assert instruction_fact_blocks(p) == (0,)
    assert p.blocked("all") and ("confirm", None) not in card_actions(p)
    assert len([i for i in p.issues if i.item == "all" and i.field == "fact_instruction"]) == 1
    sealed = seal(p, Context())
    assert sealed.candidate._dropped_facts == ()
    assert sealed.candidate == p.candidate
    assert store.list_patients(world.doctor.scope)[0] == ()
    bad = p.model_copy(
        update={
            "candidate": p.candidate.model_copy(
                update={
                    "facts": (
                        FactCandidate(category="condition", text="request CBC"),
                        FactCandidate(category="history", text="diabetes"),
                    )
                }
            )
        }
    )
    cleaned = seal(bad, Context())
    assert cleaned.candidate._dropped_facts == ("fact_instruction_content",)
    again = seal(cleaned, Context())
    assert again.candidate._dropped_facts == cleaned.candidate._dropped_facts
    assert again.evidence_fingerprint == cleaned.evidence_fingerprint
    assert all(not n.item.startswith("fact:") or n.item == "fact:0" for n in again.names)


@pytest.mark.parametrize("reverse", [False, True])
def test_f23_after_new_non_name_and_selected_retry_survival(
    store: StoreBase,
    clock: Clock,
    reverse: bool,
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    world.post(update(APPLICANT, "/new Ahmed Test", 8000))
    world.tap("✅ Confirm", id=8001)
    clock.now += timedelta(seconds=5)
    complete = value("Ahmed Test")
    ambiguous = deepcopy(complete)
    ambiguous["ambiguities"] = ["No other demographic information is provided for the patient."]
    p = run(world, FIRST, [ambiguous, complete] if reverse else [complete, ambiguous], 8002)
    assert not any(i.code == "multiple_patients" for i in p.issues)
    text = "\n".join(render_card(p))
    assert "No other demographic" not in text
    assert "Please clarify the patient and instructions." in text
    assert "✅ Confirm" not in text and ("confirm", None) not in card_actions(p)
    world.tap("❌ Cancel", id=8010)
    retry = run(world, FIRST, [complete, complete], 8003)
    assert retry.selected_patient_id
    assert [(o.drug, o.action, o.dose) for o in retry.candidate.orders] == [
        ("Exforge", "start", "5/160"),
        ("Aspirin", "stop", None),
    ]
    assert {m.kind for m in retry.candidate.missions} == {"TEST", "MONITOR"}
    assert "CBC" in "\n".join(render_card(retry)) and "K" in "\n".join(render_card(retry))
    world.tap("✅ Confirm", id=8004)
    scope = PatientScope(doctor_id=world.doctor.id, patient_id=retry.selected_patient_id)
    intact_plan(store, scope, retry)
    missions, _ = store.list_records(scope, "mission")
    assert sorted(str(m.body["kind"]) for m in missions) == [
        "MEDICATION",
        "MEDICATION",
        "MONITOR",
        "TEST",
    ]
    monitor = next(m for m in missions if m.body["kind"] == "MONITOR")
    details = monitor.body["details"]
    assert isinstance(details, dict) and isinstance(details["slots"], list)
    assert len(details["slots"]) == 6


def test_f23_other_patient_requires_anchored_owned_name(store: StoreBase, clock: Clock) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    world.named_stub("Mona Test")
    world.named_stub("Ahmed Test")
    source = "Ahmed Test start Exforge 5/160. Mona Test request CBC."
    reading = {
        "patient": {"name_as_spoken": "Ahmed Test"},
        "orders": [
            {"action": "start", "action_quote": "start", "drug": "Exforge", "dose": "5/160"}
        ],
        "missions": [{"kind": "TEST", "text": "CBC"}],
        "ambiguities": ["Mona Test"],
    }
    p = run(world, source, [reading, reading])
    assert any(i.code == "multiple_patients" for i in p.issues)
    for names, text in [((), source), (("Mona Test",), "Ahmed Test start Exforge 5/160.")]:
        issues = candidate_issues(p.candidate, text, other_patient_names=names)
        assert not any(i.code == "multiple_patients" for i in issues)


def test_f25_missing_number_keeps_blocked_fact_and_commits_other_items(
    store: StoreBase, clock: Clock
) -> None:
    from sanad.channels.telegram import wording

    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    reading = value("Ahmed Test")
    bad_text = "check his pressure 3 times a day"
    reading["facts"] = [{"category": "history", "text": bad_text}]
    peer = deepcopy(reading)
    peer["facts"] = []
    p = run(world, FIRST, [reading, peer])
    assert [(f.category, f.text) for f in p.candidate.facts] == [
        ("history", bad_text),
        (
            "medication_history",
            "Aspirin: doctor instructed hold; no prior order on file; dose unknown",
        ),
    ]
    assert p.candidate._dropped_facts == ()
    issues = [i for i in p.issues if i.item == "fact:0" and i.code == "unsupported_number"]
    assert len(issues) == 1 and issues[0].numbers == ("3",) and issues[0].blocked
    assert p.blocked("fact:0") and not p.blocked("all")
    assert not drop_instruction_fact(p.candidate.facts[0], p.candidate, FIRST)
    card = "\n".join(render_card(p))
    assert "A proposed number was not in your dictation; please edit the item." in card
    actions = card_actions(p)
    assert actions == (("confirm", None), ("edit", None), ("reject", None))
    labels = [wording.button(action, "en") for action, _ in actions]
    assert " | ".join(labels) in card.splitlines()
    intents = [
        intent
        for intent in world.cards()
        if intent.source_versions
        and intent.source_versions[0].id == p.id
        and intent.source_versions[0].version == p.version
    ]
    assert len(intents) == 1
    payload = intents[0].payload
    assert isinstance(payload, dict)
    markup = payload["reply_markup"]
    assert isinstance(markup, dict)
    keyboard = markup["inline_keyboard"]
    assert isinstance(keyboard, list)
    actual = []
    for row in keyboard:
        assert isinstance(row, list)
        for button in row:
            assert isinstance(button, dict)
            actual.append(button["text"])
    assert actual == labels
    scope, facts = persisted(world, p)
    assert [(f.body["category"], f.body["payload"]["text"]) for f in facts] == [
        (
            "medication_history",
            "Aspirin: doctor instructed hold; no prior order on file; dose unknown",
        )
    ]
    intact_plan(store, scope, p)
    missions, _ = store.list_records(scope, "mission")
    assert sorted(str(m.body["kind"]) for m in missions) == [
        "MEDICATION",
        "MEDICATION",
        "MONITOR",
        "TEST",
    ]
    monitor = next(m for m in missions if m.body["kind"] == "MONITOR")
    assert isinstance(monitor.body["details"], dict)
    slots = monitor.body["details"]["slots"]
    assert isinstance(slots, list) and len(slots) == 6
    plans, _ = store.list_records(scope, "care_plan")
    assert len(plans) == 1
    blocked_items = plans[0].body["blocked_items"]
    assert isinstance(blocked_items, list)
    assert issues[0].model_dump(mode="json") in blocked_items
