from datetime import timedelta
from typing import Any

import pytest
from harness import FakeClock, SimulatedCrash

from sanad.domain import Mission
from sanad.scribe.card import render_card
from sanad.scribe.proposal import Proposal
from sanad.store._base import StoreBase, Write
from sanad.store.records import from_record, record_item
from store.account_fixtures import APPLICANT, update
from store.photo_fixtures import photo, prescription, providers
from store.scribe_fixtures import ScribeWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    w = ScribeWorld.create(store, clock)
    w.approve()
    w.named_stub("أحمد رضا")
    w.dictate(
        "أحمد رضا ابدأ أتورفاستاتين 20 مج بالليل",
        {
            "patient": {"name_as_spoken": "أحمد رضا"},
            "orders": [
                {"action": "start", "drug": "أتورفاستاتين", "dose": "20 مج", "timing": "بالليل"}
            ],
        },
    )
    w.tap()
    return w


def amendment(world: ScribeWorld, action: str = "change", **changes: Any) -> Proposal:
    return world.dictate(
        f"أحمد رضا {action} أتورفاستاتين 40 مج بالليل بعد 3 أيام بعد 4 ساعات",
        {
            "patient": {"name_as_spoken": "أحمد رضا"},
            "orders": [
                {
                    "action": action,
                    "drug": "أتورفاستاتين",
                    "dose": "40 مج" if action != "stop" else None,
                    **changes,
                }
            ],
        },
        id=11,
    )


@pytest.mark.parametrize("action", ["change", "stop"])
def test_versions_missions_epochs_and_explicit_followup(world: ScribeWorld, action: str) -> None:
    p = amendment(
        world, action, checkin_expression="بعد 3 أيام", effective_expression="بعد 4 ساعات"
    )
    scope = world.claims.patient(world.doctor.id, p.selected_patient_id or "").scope  # type: ignore[union-attr]
    old_patient = world.claims.patient(world.doctor.id, scope.patient_id)
    assert old_patient is not None
    assert (
        "Atorvastatin: 20 مج بالليل ← " + ("40 مج بالليل" if action == "change" else "إيقاف")
    ) in "\n".join(render_card(p))
    world.tap(id=21)
    versions = world.store.list_records(scope, "care_order_version")[0]
    assert len(versions) == 2 and versions[0].body["supersedes_version"] is None
    assert versions[1].body["supersedes_version"] == 1
    assert versions[1].body["effective_from"] == (
        world.clock() + timedelta(hours=4)
    ).isoformat().replace("+00:00", "Z")
    patient = world.claims.patient(world.doctor.id, scope.patient_id)
    profile = world.store.get_patient_profile(scope)
    assert patient is not None and profile is not None
    assert patient.delivery_epoch == old_patient.delivery_epoch + 1 == profile.delivery_epoch
    assert (
        sum(
            getattr(from_record(r, Mission).details, "action", None) == action.upper()
            for r in world.store.list_records(scope, "mission")[0]
        )
        == 1
    )
    followups = world.store.list_records(scope, "followup")[0]
    assert len(followups) == 2
    assert {r.body["kind"] for r in followups} == {"MEDICATION_DAY3", "CLINICAL_CHECKIN"}


@pytest.mark.parametrize("source", ["dictation", "photo"])
def test_identical_continue_has_no_clinical_writes(world: ScribeWorld, source: str) -> None:
    if source == "dictation":
        p = amendment(world, "continue", dose="20 مج", timing="بالليل")
    else:
        providers(
            world, prescription("20 mg", "Atorvastatin"), prescription("20 mg", "Atorvastatin")
        )
        world.post(photo(id=11))
        world.tap("✏️ تعديل", id=21)
        world.post(update(APPLICANT, "صف 1: الإجراء=continue؛ الجرعة=20 مج؛ التوقيت=بالليل", 12))
        p = world.proposal
    assert p.amendments[0].noop and "زي ما هو" in "\n".join(render_card(p))
    patient = world.claims.patient(world.doctor.id, p.selected_patient_id or "")
    assert patient is not None
    snapshot = {
        kind: world.store.list_records(patient.scope, kind)[0]
        for kind in (
            "patient",
            "care_order_head",
            "care_order_version",
            "mission",
            "followup",
            "care_plan",
        )
    }
    world.tap(id=22)
    assert snapshot == {kind: world.store.list_records(patient.scope, kind)[0] for kind in snapshot}
    saved = world.scribe.repo.load(world.doctor.scope, "scribe_proposal", p.id, Proposal)
    assert saved and saved.status == "confirmed"
    if source == "photo":
        assert len(world.store.list_records(patient.scope, "patient_media")[0]) == 1


def test_photo_existing_drug_becomes_change(world: ScribeWorld) -> None:
    providers(world, prescription("40 mg", "Atorvastatin"), prescription("40 mg", "Atorvastatin"))
    world.post(photo(id=11))
    p = world.proposal
    assert p.candidate.orders[0].action == "change"
    world.tap(id=21)
    patient = world.claims.patient(world.doctor.id, p.selected_patient_id or "")
    assert patient and len(world.store.list_records(patient.scope, "followup")[0]) == 1


def test_concurrent_head_change_stales_card(world: ScribeWorld) -> None:
    p = amendment(world)
    patient = world.claims.patient(world.doctor.id, p.selected_patient_id or "")
    assert patient is not None
    head = world.store.list_records(patient.scope, "care_order_head")[0][0]
    changed = world.store._revision(
        head, world.clock(), delivery_epoch=int(str(head.body["delivery_epoch"])) + 1
    )
    assert world.store._atomic([Write(record_item(changed), head.version)], [])
    world.tap(id=21)
    saved = world.scribe.repo.load(world.doctor.scope, "scribe_proposal", p.id, Proposal)
    assert saved and saved.status == "rejected" and saved.reason == "stale_version"
    assert len(world.store.list_records(patient.scope, "care_order_version")[0]) == 1


def test_amendment_transaction_crash_writes_nothing(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = amendment(world)
    patient = world.claims.patient(world.doctor.id, p.selected_patient_id or "")
    assert patient is not None
    original = world.store._atomic

    def crash(writes: Any, checks: Any) -> bool:
        if any(w.item.get("entity_type") == "care_order_version" for w in writes):
            raise SimulatedCrash("amendment_atomic")
        return original(writes, checks)

    monkeypatch.setattr(world.store, "_atomic", crash)
    with pytest.raises(SimulatedCrash):
        world.tap(id=21)
    assert world.claims.patient(world.doctor.id, patient.id) == patient
    assert len(world.store.list_records(patient.scope, "care_order_version")[0]) == 1
    monkeypatch.setattr(world.store, "_atomic", original)
    world.clock.advance(timedelta(minutes=11))
    world.scribe(
        world.receipt(21),
        world.store.authorize(world.runtime.settings.bot_id, world.doctor.telegram_user_id),
    )
    assert len(world.store.list_records(patient.scope, "care_order_version")[0]) == 2
