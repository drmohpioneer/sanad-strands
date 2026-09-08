"""Receipt, rendering and confirmation outcomes for attempt 7 on both stores."""

import copy
import logging
from datetime import UTC, datetime

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate
from scribe.english_dictations import SOURCE, VALUE

from sanad.domain.language import Language
from sanad.scribe.card import render_card
from sanad.scribe.proposal import Proposal
from sanad.store._base import StoreBase
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld

# Slice 13 compiles "blood pressure chart, three times a day for five days" into a
# MONITOR mission, so its deadline is the accepted schedule_end default: the day
# after the last slot, at 10:00 local. Dictated 2026-09-06 15:00 Cairo, the slots
# run Mon 09-07 08:00 to Fri 09-11 20:00 local, so the mission is due Sat 09-12
# 10:00 Cairo. The old expectation of dictation plus five days fell before the
# patient's own last reading was due.
SCHEDULE_DUE = datetime(2026, 9, 12, 7, 0, tzinfo=UTC)


def test_current_medication_fact_cannot_restore_bare_previous_drug(
    store: StoreBase, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    value = copy.deepcopy(VALUE)
    value["facts"].append({"category": "medication_history", "text": "Taking Exforge 5/160"})
    caplog.set_level(logging.INFO, logger="sanad.scribe.changes")
    p = world.dictate(SOURCE, value)
    text = "\n".join(render_card(p))
    assert len(p.candidate.orders) == 2
    assert "Should I record the current medication as continue?" not in text
    assert "What dose of Forxiga" in text
    assert "scribe_bare_continue_dropped count=1" in caplog.text
    assert "Exforge" not in caplog.text
    assert not any(i.code == "fact_medication" for i in p.issues)


@pytest.mark.parametrize("language", ["en", "ar"])
@pytest.mark.parametrize("reverse", [False, True])
def test_combined_ecg_echo_fact_renders_separate_lines_without_rewriting_the_fact(
    store: StoreBase, clock: FakeClock, language: Language, reverse: bool
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language=language)
    value = copy.deepcopy(VALUE)
    findings = [value["facts"][1]["text"], "Echo: function 45%"]
    if reverse:
        findings.reverse()
    phrase = ", ".join(findings)
    value["facts"][1:] = [{"category": "finding", "text": phrase}]
    source = SOURCE.replace(
        "ECG: T-wave inversion in lateral leads. Echo: ejection fraction 45%", phrase
    )
    p = world.dictate(source, value)
    before = p.model_dump()
    lines = "\n".join(render_card(p)).splitlines()
    ecg = [line for line in lines if line.startswith("ECG:")]
    echo = [line for line in lines if line.startswith("Echo:")]
    assert len(ecg) == len(echo) == 1
    assert "T wave inversion" in ecg[0] and "45%" not in ecg[0]
    assert "function 45%" in echo[0] and "EF" not in echo[0]
    assert p.model_dump() == before and len(p.candidate.facts) == 2


@pytest.mark.parametrize("explicit", [False, True])
def test_only_explicit_source_instruction_retains_alert(
    store: StoreBase, clock: FakeClock, explicit: bool
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    condition = "blood pressure is high, 150/90"
    value: dict[str, object] = {
        "patient": {"name_as_spoken": "Synthetic Person"},
        "facts": [{"category": "finding", "text": condition}],
        "alerts": [condition],
    }
    p = world.dictate(
        "Synthetic Person. " + ("Notify me if " if explicit else "") + condition, value
    )
    text = "\n".join(render_card(p))
    assert bool(p.candidate.alerts) == explicit
    assert ("Notify me if:" in text) == explicit
    assert "150/90" in text and p.candidate.facts


@pytest.mark.parametrize("recovered", [False, True])
def test_missing_task_uses_one_identical_primary_retry_and_never_the_secondary_task(
    store: StoreBase, clock: FakeClock, recovered: bool
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    incomplete = copy.deepcopy(VALUE)
    incomplete["missions"] = [incomplete["missions"][1]]
    models = [
        ScriptedModel(candidate(v)) for v in (incomplete, VALUE, VALUE if recovered else incomplete)
    ]
    factories = iter(models)
    world.scribe.model_factory = lambda *_: next(factories)
    retries: list[str] = []
    world.scribe.observe_retry = retries.append
    world.post(update(APPLICANT, SOURCE, 10))
    p = world.proposal
    assert retries == ["request_missing"]
    assert all(len(m.script.calls) == 1 for m in models)
    assert models[0].script.calls[0] == models[2].script.calls[0]
    assert p.blocked("all") != recovered
    tasks = [m for m in p.candidate.missions if m.kind in {"TASK", "MONITOR"}]
    assert bool(tasks) == recovered
    if recovered:
        timing = next(t for t in p.timings if t.item == "mission:0")
        assert timing.resolved.due_at == SCHEDULE_DUE
    else:
        with pytest.raises(AssertionError, match="card button missing"):
            world.button("✅ Confirm")


def test_observation_confirmation_saves_fact_without_alert_order(
    store: StoreBase, clock: FakeClock
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    patient = world.named_stub("Synthetic Person")
    condition = "blood pressure is high, 150/90"
    value: dict[str, object] = {
        "patient": {"name_as_spoken": "Synthetic Person"},
        "facts": [{"category": "finding", "text": condition}],
        "alerts": [condition],
    }
    p = world.dictate("Synthetic Person. " + condition, value)
    world.tap("✅ Confirm")
    saved = world.scribe.repo.load(p.scope, "scribe_proposal", p.id, Proposal)
    assert saved and saved.status == "confirmed"
    facts, _ = store.list_records(patient.scope, "clinical_fact")
    orders, _ = store.list_records(patient.scope, "care_order_version")
    assert len(facts) == 1 and "150/90" in str(facts[0].body)
    assert not orders


@pytest.mark.parametrize("timing", ["five days", "for five days", None])
def test_task_duration_uses_receipt_anchor_without_extra_deadline_question(
    store: StoreBase, clock: FakeClock, timing: str | None
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    value = copy.deepcopy(VALUE)
    value["missions"][0].update(
        text="Blood pressure chart, three times a day for five days", timing_expression=timing
    )
    p = world.dictate(
        SOURCE.replace("3 times a day for 5 days", "three times a day for five days"), value
    )
    due = next(t.resolved.due_at for t in p.timings if t.item == "mission:0")
    assert due == SCHEDULE_DUE
    assert not any(i.item == "mission:0" and i.code == "timing_unclear" for i in p.issues)


def test_missing_request_retry_cannot_erase_an_unsupported_number(
    store: StoreBase, clock: FakeClock
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    incomplete = copy.deepcopy(VALUE)
    incomplete["missions"] = [incomplete["missions"][1]]
    incomplete["orders"][1]["dose"] = "999"
    models = [ScriptedModel(candidate(v)) for v in (incomplete, VALUE, VALUE)]
    factories = iter(models)
    world.scribe.model_factory = lambda *_: next(factories)
    world.post(update(APPLICANT, SOURCE, 10))
    p = world.proposal
    assert any(i.code == "unsupported_number" and "999" in i.numbers for i in p.issues)
    assert any(m.kind in {"TASK", "MONITOR"} for m in p.candidate.missions)


def test_dropping_bare_continue_keeps_new_order_correction_on_the_same_card(
    store: StoreBase, clock: FakeClock
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    original = world.dictate(
        "Synthetic Person. Start Concor 5.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "start", "drug": "Concor", "dose": "5"}],
        },
    )
    stale = world.button("✅ Confirm", original)
    value = original.candidate.model_dump(mode="json")
    value["orders"].insert(0, {"action": "continue", "drug": "Concor"})
    value["orders"].append({"action": "start", "drug": "Forxiga", "dose": "10"})
    value["correction_edits"] = [
        {
            "item": "order:new",
            "proposal_index": 2,
            "source_quote": "Add Forxiga 10",
        }
    ]
    p = world.dictate("Add Forxiga 10", value, id=11)
    assert p.id == original.id and p.expires_at == original.expires_at
    assert [(o.drug, o.dose) for o in p.candidate.orders] == [("Concor", "5"), ("Forxiga", "10")]
    world.tap(raw=stale)
    assert world.proposal.status == "pending"


def test_request_retry_setup_failure_keeps_valid_blocked_card(
    store: StoreBase, clock: FakeClock
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    incomplete = copy.deepcopy(VALUE)
    incomplete["missions"] = [incomplete["missions"][1]]
    model = ScriptedModel(candidate(incomplete), candidate(incomplete))
    calls = 0

    def factory(*_: object) -> ScriptedModel:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("private setup failure")
        return model

    world.scribe.model_factory = factory
    world.post(update(APPLICANT, SOURCE, 10))
    p = world.proposal
    assert calls == 3 and world.receipt(10).state == "completed"
    assert p.blocked("all") and len(p.candidate.orders) == 2
    text = "\n".join(render_card(p))
    assert "monitoring request missing" in text and "private setup failure" not in text
