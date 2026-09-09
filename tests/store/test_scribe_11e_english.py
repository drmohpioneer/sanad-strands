"""The English card, TASK compilation, language scope and correction lifecycle on both stores."""

from datetime import timedelta

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel
from scribe.english_dictations import HISTORY, MEDICATIONS, SOURCE, VALUE

from sanad.scribe.card import render_card
from sanad.store._base import StoreBase
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld


def test_english_card_and_monitoring_confirmation(store: StoreBase, clock: FakeClock) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    proposal = world.dictate(SOURCE, VALUE)
    text = "\n".join(render_card(proposal))
    assert text.splitlines()[0] == "New patient: Ahmed Saad, 53"
    assert all(line in text.splitlines() for line in (*MEDICATIONS, *HISTORY))
    assert (
        "MONITOR: blood pressure, 3 times a day for 5 days (15 readings, first Mon 08:00)" in text
    )
    assert "TEST: CBC, Na, K, lipid profile — due" in text
    assert "What dose of Forxiga did you intend?" in text
    assert "✅ Confirm | ✏️ Edit | ❌ Cancel\nvalid 30 minutes" in text
    assert [i.code for i in proposal.issues] == ["dose_missing"]
    task = next(t.resolved for t in proposal.timings if t.item == "mission:0")
    assert task.timing_anchor.kind == "schedule_end"
    assert task.due_at.date() == (task.timing_anchor.instant + timedelta(days=1)).date()
    assert task.due_source == "default"
    world.tap("✅ Confirm")
    p = world.scribe.repo.load(proposal.scope, "scribe_proposal", proposal.id, type(proposal))
    assert p and p.status == "confirmed"
    patient_id = next(
        r.body["patient_id"]
        for r in store.list_records(proposal.scope, "scribe_invitation_work")[0]
    )
    from sanad.domain import PatientScope

    scope = PatientScope(doctor_id=world.doctor.id, patient_id=str(patient_id))
    rows, _ = store.list_records(scope, "mission")
    tasks = [r for r in rows if r.body["kind"] == "MONITOR"]
    assert len(tasks) == 1
    assert "monitor" in str(tasks[0].body)
    assert "Blood pressure chart, 3 times a day for 5 days" in str(tasks[0].body)
    assert not any(r.body["kind"] == "TASK" for r in rows)
    heads, _ = store.list_records(scope, "care_order_head")
    assert [r.body["name"] for r in heads] == ["Exforge HCT"]


def test_english_reply_updates_the_same_card_and_keeps_its_expiry(
    store: StoreBase, clock: FakeClock
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    original = world.dictate(SOURCE, VALUE)
    old_button = world.button("✅ Confirm", original)
    value = original.candidate.model_dump()
    value["orders"][1]["dose"] = "10"
    clock.advance(timedelta(minutes=2))
    corrected = world.dictate("Forxiga 10", value, id=11)
    assert corrected.id == original.id and corrected.expires_at == original.expires_at
    assert corrected.candidate.orders[0] == original.candidate.orders[0]
    assert corrected.candidate.orders[1].dose == "10"
    assert not corrected.issues
    assert "Card updated from your reply" in "\n".join(render_card(corrected))
    world.tap(raw=old_button)
    assert world.proposal.status == "pending"


@pytest.mark.parametrize("language", ["en", "ar"])
def test_oversized_card_keeps_orders_and_missions_and_edit_reveals_history(
    store: StoreBase, clock: FakeClock, language: str
) -> None:
    from typing import cast

    from sanad.domain.language import Language

    world = ScribeWorld.create(store, clock)
    world.approve(language=cast(Language, language))
    facts = [
        {"category": "history", "text": f"Previous event {i}: " + (f"symptom{i} " * 45).strip()}
        for i in range(1, 10)
    ]
    source = "New patient Synthetic Person. Taking Concor 5. Request CBC. " + " ".join(
        str(f["text"]) for f in facts
    )
    p = world.dictate(
        source,
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "continue", "drug": "Concor", "dose": "5"}],
            "missions": [{"kind": "TEST", "text": "CBC"}],
            "facts": facts,
        },
    )
    parts = render_card(p)
    text = "\n".join(parts)
    assert "Concor 5" in text and "TEST: CBC" in text
    assert "symptom6" in text and "symptom7" not in text
    assert (
        "… (3 more, edit to see)" if language == "en" else "… (3 بنود إضافية، اضغط تعديل لعرضها)"
    ) in text
    assert all(len(part) <= 3500 for part in parts)
    world.tap("✏️ Edit" if language == "en" else "✏️ تعديل")
    replies = "\n".join(
        str((i.payload or {}).get("text", ""))
        for i in world.cards()
        if i.template_id == "scribe_edit"
    )
    assert "symptom7" in replies and "symptom9" in replies
    assert world.proposal.editing and world.proposal.expires_at == p.expires_at


def test_language_command_is_scoped_durable_and_has_no_model_call(
    store: StoreBase, clock: FakeClock
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    other = world.approve("20004", language="en")
    model = ScriptedModel()
    world.scribe.model_factory = lambda *args: model
    original = world.doctor
    for id, language in ((51, "ar"), (52, "en")):
        message = update(APPLICANT, "/lang " + language, id)
        world.post(message)
        assert world.receipt(id).state == "completed"
        changed = world.doctor
        assert changed.language == language
        world.post(message)
        assert world.doctor == changed
        assert world.runtime.accounts.doctor(other.id) == other
    assert world.doctor.version == original.version + 2
    world.post(update(APPLICANT, "/lang null", 53))
    assert world.doctor.language == "en"
    assert len(model.script.calls) == 0


def test_language_permission_cannot_mutate_doctor_authority(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.store.records import CommitRequest, CommitResult, Doctor, from_record, to_record

    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    doctor = world.doctor
    commit = store.commit
    outcomes = []

    def forged(request: CommitRequest) -> CommitResult:
        if request.command.payload.get("type") == "ScribeLanguage":
            changed = from_record(request.puts[0], Doctor)
            changed = changed.model_copy(update={"status": "suspended"})
            request = request.model_copy(update={"puts": (to_record(changed, changed.scope),)})
        result = commit(request)
        outcomes.append(result.status)
        return result

    monkeypatch.setattr(store, "commit", forged)
    world.post(update(APPLICANT, "/lang ar", 54))
    assert "forbidden" in outcomes
    assert world.doctor == doctor


def test_new_record_defaults_and_patient_binding_follow_doctor(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.auth.service import revise
    from store.login_fixtures import LoginWorld, replace_model

    world = LoginWorld.create(store, clock)
    world.approve()
    assert world.doctor.language == "en"
    assert world.stub().language == "en"
    replace_model(world, revise(world.doctor, clock(), language="ar"))
    assert world.bound().language == "ar"


def test_same_family_brand_change_reuses_the_current_head(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.scribe.patients import panel

    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    world.dictate(
        "New patient Synthetic Person taking Exforge 5/160",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "continue", "drug": "Exforge", "dose": "5/160"}],
        },
    )
    world.tap("✅ Confirm")
    patient = panel(store, world.doctor.scope)[0]
    old = store.list_records(patient.scope, "care_order_head")[0][0]
    old_version = store.list_records(patient.scope, "care_order_version")[0][0]
    changed = world.dictate(
        "Synthetic Person: increase Exforge 5/160 to Exforge HCT 10/160/25",
        {"patient": {"name_as_spoken": "Synthetic Person"}, "orders": [VALUE["orders"][0]]},
        id=11,
    )
    assert changed.selected_patient_id == patient.id and not changed.issues
    assert MEDICATIONS[0] in "\n".join(render_card(changed))
    world.tap("✅ Confirm", id=21)
    heads = store.list_records(patient.scope, "care_order_head")[0]
    assert len(heads) == 1 and heads[0].id == old.id
    assert heads[0].body["name"] == "Exforge HCT"
    versions = store.list_records(patient.scope, "care_order_version")[0]
    assert len(versions) == 2
    assert old_version in versions
    new_version = next(version for version in versions if version != old_version)
    instruction = new_version.body["structured_instruction"]
    assert isinstance(instruction, dict)
    assert instruction["drug"] == "Exforge HCT"


@pytest.mark.parametrize(
    "source",
    ["Blood pressure chart, 3 times a day for 5 days", "قياس ضغط الدم 3 مرات في اليوم لمدة 5 ايام"],
)
def test_monitoring_text_has_schedule_end_deadline(
    store: StoreBase, clock: FakeClock, source: str
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="ar" if not source.isascii() else "en")
    proposal = world.dictate(
        "New patient Synthetic Person. " + source,
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "missions": [{"kind": "TEST", "text": source}],
        },
    )
    assert proposal.candidate.missions[0].kind == "MONITOR"
    assert proposal.candidate.missions[0].text == source
    timing = proposal.timings[0].resolved
    assert timing.timing_anchor.kind == "schedule_end"
    assert timing.due_at.date() == (timing.timing_anchor.instant + timedelta(days=1)).date()
    assert not proposal.issues
