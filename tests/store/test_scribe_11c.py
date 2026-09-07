"""Contract 11c outcomes through real receipt/confirmation, on both stores."""

from datetime import timedelta
from typing import Any

import pytest
from harness import FakeClock, SimulatedCrash
from providers.fixtures import ScriptedModel, candidate, response
from providers.rxnorm_fixture import RxNormFixture

from sanad.scribe.card import dictation_questions, render_card
from sanad.scribe.memory import NameVocabulary, memory_rows
from sanad.scribe.names import known_names
from sanad.scribe.proposal import ScribeCallback
from sanad.store import keys
from sanad.store._base import Check, StoreBase, Write
from sanad.store.keys import AccountScope
from sanad.store.records import (
    NAME_CACHE_SCOPE,
    NameCache,
    NameMemory,
    from_record,
    record_item,
    to_record,
)
from store.account_fixtures import APPLICANT, update
from store.login_fixtures import browser_login
from store.scribe_fixtures import ScribeWorld
from store.test_scribe_voice_web import providers, voice

SOURCE = (
    "مريض جديد سامي اختبار 53 سنة ماشي على إكس فورش إتش سي تي 516 12.5 وكونكور 5 "
    "وضفت فورسيجا والأكو فانكشن 45 % وتحاليل بانو كريات وسوديوم وبوتاسيوم"
)
VALUE: dict[str, Any] = {
    "patient": {"name_as_spoken": "سامي اختبار", "age": "53"},
    "orders": [
        {
            "action": "continue",
            "drug": "إكس فورش إتش سي تي",
            "name_latin": "Exforge HCT",
            "dose": "516 12.5",
        },
        {"action": "continue", "drug": "كونكور", "name_latin": "Concor", "dose": "5"},
        {"action": "start", "drug": "فورسيجا", "name_latin": "Forxiga"},
    ],
    "facts": [
        {
            "category": "finding",
            "clinical_kind": "Echo",
            "text": "الأكو فانكشن 45 %",
            "clinical_en": "EF 45% %",
            "terms": [{"spoken": "الأكو فانكشن 45 %", "english": "EF 45% %"}],
        }
    ],
    "missions": [
        {
            "kind": "TEST",
            "text": "بانو كريات وسوديوم وبوتاسيوم",
            "clinical_en": "BUN, creatinine, sodium, potassium",
        }
    ],
}


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    result = ScribeWorld.create(store, clock)
    result.approve()
    return result


def test_auto_correction_keeps_card_patient_unanswered_fields_and_slots(world: ScribeWorld) -> None:
    providers(world, SOURCE + " NUMBERS: 53 516 12.5 5")
    world.scribe.model_factory = lambda registry, role: ScriptedModel(candidate(VALUE))
    world.post(voice())
    first = world.proposal
    assert len(dictation_questions(first)) == 3
    old_button = world.button("✅ تمام")
    changed = first.candidate.model_dump()
    changed["patient"] = {"name_as_spoken": "model-selected stranger"}
    changed["orders"][2]["dose"] = "10"
    model = ScriptedModel(candidate(changed))
    world.scribe.model_factory = lambda registry, role: model
    world.post(update(APPLICANT, "فورسيجا 10", 11))
    second = world.proposal
    assert second.id == first.id and second.version == first.version + 1
    assert second.candidate.patient == first.candidate.patient
    assert second.expires_at == first.expires_at and second.timings[0] == first.timings[0]
    assert second.candidate.orders[:2] == first.candidate.orders[:2]
    assert second.candidate.facts == first.candidate.facts
    assert second.candidate.orders[2].dose == "10"
    assert any("Exforge" in q for q in dictation_questions(second))
    assert not any('جرعة "Forxiga"' in q for q in dictation_questions(second))
    assert len(dictation_questions(second)) == 2
    request = str(model.script.calls[0])
    assert all(label in request for label in ("q1:", "q2:", "q3:"))
    assert "عدّلت الكارت حسب كلامك" in render_card(second)[0]
    world.tap(raw=old_button, id=20)
    assert world.proposal.status == "pending"


def test_answered_dose_cannot_consume_an_unanswered_age(world: ScribeWorld) -> None:
    first = world.dictate(
        "مريض جديد سامي اختبار 53 إكسفورج إتش سي تي 516 12.5",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "orders": [
                {
                    "action": "continue",
                    "drug": "إكسفورج إتش سي تي",
                    "name_latin": "Exforge HCT",
                    "dose": "516 12.5",
                }
            ],
        },
    )
    value = first.candidate.model_dump()
    value["orders"][0]["dose"] = "5/160/12.5"
    second = world.dictate("إكسفورج 5/160/12.5", value, id=11)
    assert any(i.code == "unassigned_number" and "53" in i.numbers for i in second.issues)
    assert any('"53"' in q for q in dictation_questions(second))
    assert not any(i.question and "516" in i.question for i in second.issues)


def test_dose_reply_preserves_unanswered_clocks_across_midnight(world: ScribeWorld) -> None:
    world.clock.advance(timedelta(hours=8, minutes=50))
    first = world.dictate(
        "سامي اختبار فورسيجا 5 من بعد يوم ويتابع بعد ساعتين",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "orders": [
                {
                    "action": "start",
                    "drug": "فورسيجا",
                    "dose": "5",
                    "effective_expression": "بعد يوم",
                    "checkin_expression": "بعد ساعتين",
                }
            ],
        },
    )
    world.clock.advance(timedelta(minutes=20))
    value = first.candidate.model_dump()
    value["orders"][0]["dose"] = "10"
    second = world.dictate("فورسيجا 10", value, id=11)
    assert second.timings == first.timings
    assert second.expires_at == first.expires_at


@pytest.mark.parametrize("by_voice", [False, True])
def test_three_answers_merge_without_ef_in_drug_dose(world: ScribeWorld, by_voice: bool) -> None:
    first = world.dictate(SOURCE, VALUE)
    merged = first.candidate.model_dump()
    merged["orders"][0]["dose"] = "5/160/12.5"
    merged["orders"][2]["dose"] = "10"
    merged["facts"][0]["clinical_en"] = "EF 45%"
    text = "إكسفورج 5/160/12.5، فورسيجا 10، والـ 45 ده الـ EF"
    if by_voice:
        providers(world, text + " NUMBERS: 5 160 12.5 10 45")
        world.scribe.model_factory = lambda registry, role: ScriptedModel(candidate(merged))
        world.post(voice(11))
    else:
        world.dictate(text, merged, id=11)
    second = world.proposal
    card = render_card(second)[0]
    assert "Exforge HCT 5/160/12.5" in card and "Forxiga 10" in card and "EF 45%" in card
    assert "% %" not in card and "10, 45" not in card
    assert not any(
        q
        for q in dictation_questions(second)
        if any(n in q for n in ("Exforge", "Forxiga", "EF", "516"))
    )
    assert not any(i.code == "unsupported_number" for i in second.issues)


def test_dose_answer_cannot_borrow_ef_number(world: ScribeWorld) -> None:
    first = world.dictate(SOURCE, VALUE)
    merged = first.candidate.model_dump()
    merged["orders"][2]["dose"] = "10, 45"
    second = world.dictate("فورسيجا 10، EF 45", merged, id=11)
    assert second.candidate.orders[2].dose is None
    assert any(i.code == "correction_unclear" for i in second.issues)
    assert "Forxiga 10, 45" not in render_card(second)[0]


@pytest.mark.parametrize(
    "escape", ["/new مريم تجربة", "مريض جديد مريم تجربة", "مريم تجربة محتاجة CBC"]
)
def test_explicit_new_or_existing_other_patient_supersedes(world: ScribeWorld, escape: str) -> None:
    other = world.named_stub("مريم تجربة")
    first = world.dictate(SOURCE, VALUE)
    if escape.startswith("/new"):
        world.post(update(APPLICANT, escape, 11))
    else:
        world.dictate(
            escape,
            {
                "patient": {"name_as_spoken": "مريم تجربة"},
                "missions": [{"kind": "TEST", "text": "CBC", "clinical_en": "CBC"}],
            },
            id=11,
        )
    assert world.proposal.id != first.id
    old = world.store.get(first.scope, "scribe_proposal", first.id)
    assert old and old.body["status"] == "superseded"
    assert world.proposal.creating_patient or world.proposal.selected_patient_id == other.id


@pytest.mark.parametrize("choice", ["تعديل للكارت", "مريض جديد"])
def test_mid_sentence_new_patient_requires_choice(world: ScribeWorld, choice: str) -> None:
    first = world.dictate(SOURCE, VALUE)
    no_call = ScriptedModel()
    world.scribe.model_factory = lambda registry, role: no_call
    world.post(update(APPLICANT, "فورسيجا 10 وفيه مريض تاني مريم تجربة محتاجة CBC", 11))
    assert not no_call.script.calls and world.proposal.pending_reply
    assert world.proposal.id == first.id
    value: dict[str, Any] = (
        first.candidate.model_dump()
        if choice == "تعديل للكارت"
        else {
            "patient": {"name_as_spoken": "مريم تجربة"},
            "missions": [{"kind": "TEST", "text": "CBC"}],
        }
    )
    if choice == "تعديل للكارت":
        value["orders"][2]["dose"] = "10"
    world.scribe.model_factory = lambda registry, role: ScriptedModel(candidate(value))
    world.tap(choice, id=20)
    assert world.proposal.pending_reply is None
    assert (world.proposal.id == first.id) == (choice == "تعديل للكارت")


def test_continue_without_head_commits_without_day_three(world: ScribeWorld) -> None:
    patient = world.named_stub("مريض اختبار")
    p = world.dictate(
        "مريض اختبار بياخد كونكور 5",
        {
            "patient": {"name_as_spoken": patient.display_name},
            "orders": [{"action": "continue", "drug": "كونكور", "dose": "5"}],
        },
    )
    assert not any(i.code == "order_missing" for i in p.issues)
    world.tap()
    assert len(world.store.list_records(patient.scope, "care_order_head")[0]) == 1
    assert not world.store.list_records(patient.scope, "followup")[0]


@pytest.mark.parametrize("action", ["stop", "change"])
def test_unknown_stop_change_keeps_missing_head_question(world: ScribeWorld, action: str) -> None:
    patient = world.named_stub("مريض اختبار")
    p = world.dictate(
        "مريض اختبار كونكور 5",
        {
            "patient": {"name_as_spoken": patient.display_name},
            "orders": [{"action": action, "drug": "كونكور", "dose": "5"}],
        },
    )
    assert any(i.code == "order_missing" for i in p.issues)
    world.tap()
    assert not world.store.list_records(patient.scope, "care_order_head")[0]


def test_fixture_rxnorm_cache_expiry_budget_and_metadata(world: ScribeWorld) -> None:
    fixture = RxNormFixture()
    world.scribe.rxnorm_client = fixture.client
    service = world.scribe.name_lookup(world.doctor, world.owner)
    assert service.lookup_drug("Exforge HCT").source == "rxnorm"
    assert len(fixture.calls) == 3
    assert service.lookup_drug("Forxiga").canonical == "Farxiga"
    assert len(fixture.calls) == 6
    assert not service.lookup_drug("unknownbrand").found and len(fixture.calls) == 6
    assert all(0 < r.extensions["timeout"]["read"] <= 3 for r in fixture.calls)
    cached = world.store.get(NAME_CACHE_SCOPE, "name_cache", keys.digest("exforge hct"))
    assert cached and from_record(cached, NameCache).expires_at == world.clock() + timedelta(
        days=30
    )
    assert cached.ttl == int((world.clock() + timedelta(days=30)).timestamp())
    again = world.scribe.name_lookup(world.doctor, world.owner)
    assert again.lookup_drug("Exforge HCT").found and len(fixture.calls) == 6
    world.clock.advance(timedelta(days=30))
    again = world.scribe.name_lookup(world.doctor, world.owner)
    assert again.lookup_drug("Exforge HCT").found and len(fixture.calls) == 9


@pytest.mark.parametrize("name", ["bisoprolol", "amlodipine", "Farxiga", "not-a-drug"])
def test_fixture_results(world: ScribeWorld, name: str) -> None:
    fixture = RxNormFixture()
    world.scribe.rxnorm_client = fixture.client
    result = world.scribe.name_lookup(world.doctor, world.owner).lookup_drug(name)
    assert result.found == (name != "not-a-drug")


@pytest.mark.parametrize("failure", ["timeout", "html"])
def test_rxnorm_outage_keeps_unknown_name_confirmable_and_private(
    world: ScribeWorld, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    fixture = RxNormFixture(failure=failure)
    world.scribe.rxnorm_client = fixture.client
    p = world.dictate(
        "سامي اختبار بياخد ريميبروتينيب 5",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "orders": [
                {
                    "action": "continue",
                    "drug": "ريميبروتينيب",
                    "name_latin": "Remibrutinib",
                    "generic": "remibrutinib",
                    "dose": "5",
                }
            ],
        },
    )
    assert "Remibrutinib 5" in render_card(p)[0]
    assert not p.blocked("order:0")
    assert not any(
        word in caplog.text for word in ("سامي", "private provider body", "Remibrutinib")
    )
    world.tap()
    assert memory_rows(world.store, world.doctor.scope)[0].latin == "Remibrutinib"


def test_rxnorm_generic_mismatch_cannot_swap_a_drug(world: ScribeWorld) -> None:
    fixture = RxNormFixture(generic="omeprazole")
    world.scribe.rxnorm_client = fixture.client
    p = world.dictate(
        "سامي اختبار فورسيجا 10",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "orders": [
                {
                    "action": "start",
                    "drug": "فورسيجا",
                    "name_latin": "Forxiga",
                    "generic": "dapagliflozin",
                    "dose": "10",
                }
            ],
        },
    )
    assert p.blocked("order:0")
    assert "omeprazole" not in render_card(p)[0]
    world.tap()
    assert not memory_rows(world.store, world.doctor.scope)


def test_tool_runs_inside_scribe_extraction(world: ScribeWorld) -> None:
    fixture = RxNormFixture()
    world.scribe.rxnorm_client = fixture.client
    model = ScriptedModel(
        response(calls=[("lookup_drug", {"name": "bisoprolol"})]),
        candidate(
            {
                "patient": {"name_as_spoken": "سامي اختبار"},
                "orders": [
                    {
                        "action": "continue",
                        "drug": "بيزوبرولول",
                        "name_latin": "Bisoprolol",
                        "dose": "5",
                    }
                ],
            }
        ),
    )
    world.scribe.model_factory = lambda registry, role: model
    world.post(update(APPLICANT, "سامي اختبار بيزوبرولول 5", 10))
    assert len(model.script.calls) == 4 and len(fixture.calls) == 3
    assert world.proposal.candidate.orders[0].drug == "Bisoprolol"


def test_memory_count_hint_clinic_sharing_and_doctor_api_isolation(world: ScribeWorld) -> None:
    for i in range(3):
        world.dictate(
            "سامي اختبار بياخد كونكور 5",
            {
                "patient": {"name_as_spoken": "سامي اختبار"},
                "orders": [{"action": "continue", "drug": "كونكور", "dose": "5"}],
            },
            id=10 + i,
        )
        world.tap(id=20 + i)
    own = memory_rows(world.store, world.doctor.scope)
    assert own[0].latin == "Concor" and own[0].confirmations == 3
    assert NameVocabulary(world.store, world.doctor).hint().startswith("Concor,")
    assert (
        memory_rows(world.store, AccountScope(bot_id=world.doctor.telegram_bot_id))[0].confirmations
        == 3
    )
    other = world.approve("40004")
    assert not memory_rows(world.store, other.scope)
    assert NameVocabulary(world.store, other).rows[0] == ()
    assert NameVocabulary(world.store, other).rows[1][0].latin == "Concor"
    browser = world.client()
    assert browser_login(browser, world.login_path()).status_code == 303
    assert browser.get("/api/names").json()[0]["confirmations"] == 3
    second = world.client()
    from store.login_fixtures import ORIGIN

    world.post(update("40004", "/login", 101))
    intent = next(
        i
        for i in world.intents()
        if i.template_id == "doctor_login_link" and i.payload and i.recipient_subject == "40004"
    )
    assert intent.payload
    path = str(intent.payload["text"]).splitlines()[-1].removeprefix(ORIGIN)
    assert browser_login(second, path).status_code == 303
    assert second.get("/api/names").json() == []
    assert second.post("/api/names", json={}).status_code == 405


@pytest.mark.parametrize("after_commit", [False, True])
def test_confirmation_and_memory_are_one_atomic_transaction(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch, after_commit: bool
) -> None:
    p = world.dictate(SOURCE, VALUE)
    raw = world.button("✅ تمام")
    row = world.store.get(p.scope, "scribe_callback", keys.digest(raw))
    assert row
    token = from_record(row, ScribeCallback)
    atomic = world.store._atomic
    seen: list[Write] = []

    def crash(writes: list[Write], checks: list[Check]) -> bool:
        if any(w.item.get("entity_type") == "care_plan" for w in writes):
            seen.extend(writes)
            if after_commit:
                assert atomic(writes, checks)
            raise SimulatedCrash()
        return atomic(writes, checks)

    monkeypatch.setattr(world.store, "_atomic", crash)
    with pytest.raises(SimulatedCrash):
        world.scribe.committer.confirm(p, token, world.owner, "atomic-name-confirm")
    monkeypatch.setattr(world.store, "_atomic", atomic)
    assert {"care_plan", "name_memory", "scribe_proposal"} <= {
        w.item.get("entity_type") for w in seen
    }
    assert bool(memory_rows(world.store, world.doctor.scope)) == after_commit
    assert (
        bool(memory_rows(world.store, AccountScope(bot_id=world.doctor.telegram_bot_id)))
        == after_commit
    )


def test_more_than_400_memory_names_are_capped_without_cross_request_cache(
    world: ScribeWorld,
) -> None:
    for i in range(405):
        latin = "Vocabulary" + str(i)
        memory = NameMemory(
            id=keys.digest(keys.partition(world.doctor.scope) + ":finding:" + latin.lower()),
            scope=world.doctor.scope,
            latin=latin,
            kind="finding",
            confirmations=406 - i,
            source="doctor_confirmation",
            created_at=world.clock(),
            updated_at=world.clock(),
            last_confirmed_at=world.clock(),
        )
        assert world.store._atomic([Write(record_item(to_record(memory, memory.scope)), None)], [])
    hint = NameVocabulary(world.store, world.doctor).hint().split(", ")
    assert len(hint) == 400 and hint[0] == "Vocabulary0" and hint[-1] == "Vocabulary399"
    assert len(known_names().split(", ")) <= 400
