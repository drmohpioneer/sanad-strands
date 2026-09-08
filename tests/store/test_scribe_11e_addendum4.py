"""Source names, real corrections and confirmation on both store implementations."""

import copy
from typing import Any

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate
from scribe.english_dictations import SOURCE, VALUE

from sanad.domain import PatientScope
from sanad.domain.language import Language
from sanad.scribe.card import dictation_questions, render_card
from sanad.scribe.extract import ProposalIssue
from sanad.scribe.proposal import Proposal
from sanad.store._base import StoreBase
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld

HEARD = (
    SOURCE.replace("Increase to", "Increase the dose of Exforge to be")
    .replace("ejection fraction", "function")
    .replace("3 times a day for 5 days", "three times a day for five days")
    .replace("Request tests CBC, sodium", "I ordered the one, create sodium")
)
QUESTION = 'I heard "the one, create" for a test; which test did you mean?'


def garbled_value() -> dict[str, Any]:
    value = copy.deepcopy(VALUE)
    value["orders"][0]["drug"] = "Exforge"
    value["orders"][0]["name_latin"] = "amlodipine/valsartan"
    value["orders"][0]["generic"] = "amlodipine/valsartan"
    value["missions"][0]["text"] = "Blood pressure chart, three times a day for five days"
    value["missions"][0]["timing_expression"] = "for five days"
    value["missions"][1]["text"] = "creatinine, sodium, potassium and lipid profile"
    value["facts"][2]["text"] = "Echo: function 45%"
    return value


@pytest.mark.parametrize("language", ["en", "ar"])
def test_unheard_analyte_is_quoted_once_and_never_committed_or_learned(
    store: StoreBase, clock: FakeClock, language: Language
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language=language)
    p = world.dictate(HEARD, garbled_value())
    text = "\n".join(render_card(p))
    assert "TEST: Na, K, lipid profile" in text
    assert "creatinine" not in text.casefold()
    question = QUESTION if language == "en" else 'سمعت "the one, create" كتحليل، قصدك إيه؟'
    assert text.count(question) == 1
    assert p.blocked("mission:1")
    assert "function 45%" in text and "EF 45%" not in text
    if language == "en":
        assert "Exforge 5/160 → Exforge HCT 10/160/25 (change)" in text
        assert "Please confirm the clinical wording as heard." not in text
    world.tap("✅ Confirm" if language == "en" else "✅ تمام")
    saved = world.scribe.repo.load(p.scope, "scribe_proposal", p.id, Proposal)
    assert saved and saved.status == "confirmed"
    assert "creatinine" in saved.candidate.missions[1].text  # Raw candidate retained, blocked.
    rows, _ = store.list_records(p.scope, "scribe_invitation_work")
    scope = PatientScope(doctor_id=world.doctor.id, patient_id=str(rows[0].body["patient_id"]))
    missions, _ = store.list_records(scope, "mission")
    assert not any(row.body["kind"] == "TEST" for row in missions)
    memory, _ = store.list_records(p.scope, "name_memory")
    assert not any("creatinine" in str(row.body) for row in memory)


def test_quoted_test_correction_updates_same_card_and_unrelated_reply_keeps_block(
    store: StoreBase, clock: FakeClock
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    original = world.dictate(HEARD, garbled_value())
    button = world.button("✅ Confirm", original)
    value = original.candidate.model_dump()
    value["orders"][1]["dose"] = "10"
    unrelated = world.dictate("Forxiga 10", value, id=11)
    assert unrelated.blocked("mission:1") and QUESTION in dictation_questions(unrelated)
    corrected = unrelated.candidate.model_dump()
    corrected["missions"][1]["text"] = "CBC, sodium, potassium and lipid profile"
    corrected["correction_edits"] = [
        {"item": "mission:1", "proposal_index": 1, "source_quote": "I meant CBC for the test"}
    ]
    p = world.dictate("I meant CBC for the test", corrected, id=12)
    assert p.id == original.id and p.expires_at == original.expires_at
    assert not p.blocked("mission:1") and QUESTION not in dictation_questions(p)
    assert "TEST: CBC, Na, K, lipid profile" in "\n".join(render_card(p))
    assert p.candidate.orders == unrelated.candidate.orders
    later = p.candidate.model_dump()
    later["orders"][1]["dose"] = "5"
    settled = world.dictate("Change Forxiga to 5", later, id=13)
    assert settled.id == original.id and settled.version > p.version
    assert not settled.blocked("mission:1") and QUESTION not in dictation_questions(settled)
    assert "TEST: CBC, Na, K, lipid profile" in "\n".join(render_card(settled))
    world.tap(raw=button)
    assert world.proposal.status == "pending"
    world.tap("✅ Confirm", id=14)
    rows, _ = store.list_records(p.scope, "scribe_invitation_work")
    scope = PatientScope(doctor_id=world.doctor.id, patient_id=str(rows[0].body["patient_id"]))
    missions, _ = store.list_records(scope, "mission")
    tests = [row for row in missions if row.body["kind"] == "TEST"]
    assert len(tests) == 1 and "CBC" in str(tests[0].body)
    assert "creatinine" not in str(tests[0].body)


@pytest.mark.parametrize("rewritten", [False, True])
def test_test_deadline_reply_does_not_resolve_its_analyte(
    store: StoreBase, clock: FakeClock, rewritten: bool
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    original = world.dictate(HEARD, garbled_value())
    value = original.candidate.model_dump()
    value["missions"][1]["timing_expression"] = "tomorrow"
    if rewritten:
        value["missions"][1]["text"] = "sodium, potassium and lipid profile"
    value["correction_edits"] = [
        {"item": "mission:1", "proposal_index": 1, "source_quote": "Do the tests tomorrow"}
    ]
    p = world.dictate("Do the tests tomorrow", value, id=11)
    assert p.id == original.id and p.candidate.missions[1].timing_expression == "tomorrow"
    assert p.blocked("mission:1") and QUESTION in dictation_questions(p)


@pytest.mark.parametrize("previous_dose,conflict", [(None, False), ("10/160", True)])
def test_verified_previous_values_remove_only_absent_peer_doubts(
    store: StoreBase, clock: FakeClock, previous_dose: str | None, conflict: bool
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    first, second = garbled_value(), garbled_value()
    second["orders"][0].update(previous_drug=None, previous_dose=previous_dose)
    model = ScriptedModel(candidate(first), candidate(second))
    world.scribe.model_factory = lambda *_: model
    world.post(update(APPLICANT, HEARD, 10))
    p = world.proposal
    text = "\n".join(render_card(p))
    assert "Exforge 5/160 → Exforge HCT 10/160/25 (change)" in text
    assert any(i.field == "previous_dose" for i in p.issues) == conflict
    assert p.blocked("order:0") == conflict


def test_generic_wording_cleanup_preserves_blocked_alert_refusal(
    store: StoreBase, clock: FakeClock
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    p = world.dictate(HEARD, garbled_value())
    specific = "This alert is below the safety floor; please edit the threshold."
    p = p.model_copy(
        update={
            "issues": (
                *p.issues,
                ProposalIssue(item="alert:0", code="clinical_unclear", question=specific),
                ProposalIssue(
                    item="fact:0",
                    code="clinical_unclear",
                    blocked=False,
                    question="Please confirm the clinical wording as heard.",
                ),
            )
        }
    )
    questions = dictation_questions(p)
    assert specific in questions and p.blocked("alert:0")
    assert "Please confirm the clinical wording as heard." not in questions


def test_representations_merge_but_real_dose_disagreement_still_blocks(
    store: StoreBase, clock: FakeClock
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    first, second = garbled_value(), garbled_value()
    second["orders"][0].update(previous_drug="Exforge 5/160", previous_dose=None, dose="10/160")
    second["missions"][1]["text"] = "sodium, potassium and lipid profile"
    model = ScriptedModel(candidate(first), candidate(second))
    world.scribe.model_factory = lambda *_: model
    world.post(update(APPLICANT, HEARD, 10))
    p = world.proposal
    text = "\n".join(render_card(p))
    assert "Exforge 5/160 → Exforge HCT 10/160/25 (change)" in text
    assert "previous_drug" not in text and "previous_dose" not in text
    assert [i.field for i in p.issues if i.code == "extraction_conflict"] == ["dose"]
    assert p.blocked("order:0") and QUESTION in text


def test_mission_word_counts_remove_only_global_questions(
    store: StoreBase, clock: FakeClock
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    p = world.dictate(HEARD, garbled_value())
    p = p.model_copy(
        update={
            "disputed_numbers": ("1", "3", "5", "45"),
            "issues": (
                *p.issues,
                ProposalIssue(
                    item="all", code="unassigned_number", numbers=("3", "5"), blocked=False
                ),
                ProposalIssue(item="order:0", code="disputed_number", numbers=("5",)),
            ),
        }
    )
    questions = dictation_questions(p)
    assert not any('"3"' in q or '"1"' in q or "belong" in q for q in questions)
    assert 'I heard "5"; is that correct?' in questions  # Actual dose dispute is untouched.
    assert 'I heard "45"; is that correct?' in questions
    assert QUESTION in questions and p.blocked("order:0")
