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


@pytest.mark.parametrize("language", ["en", "ar"])
@pytest.mark.parametrize(
    "old,new,old_dose,new_dose",
    [
        ("Exforge", "Exforge HCT", "5/160", None),
        ("Exforge", "Exforge HCT", "5/160", "5/160/12.5"),
        ("Exforge", "Exforge HCT", "5/160", "5/160"),
        ("Concor", "Concor", "5 مج", None),
        ("Concor", "Concor", "5 مج", "10"),
    ],
)
def test_11k_stored_change_instruction(
    store: StoreBase,
    clock: FakeClock,
    language: str,
    old: str,
    new: str,
    old_dose: str,
    new_dose: str | None,
) -> None:
    from typing import cast

    from sanad.concierge.plan import order_line
    from sanad.domain.language import Language
    from sanad.scribe.extract import OrderCandidate
    from sanad.scribe.records import CareOrderVersion

    w = ScribeWorld.create(store, clock)
    w.approve(language=cast(Language, language))
    patient = w.named_stub("Synthetic Person")
    initial = w.dictate(
        f"Synthetic Person. Taking {old} {old_dose}.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "continue", "drug": old, "dose": old_dose}],
        },
        id=8,
    )
    assert not initial.blocked("order:0")
    button = "✅ Confirm" if language == "en" else "✅ تمام"
    w.tap(button, id=9)
    before = store.list_records(patient.scope, "care_order_version")[0]
    assert len(before) == 1
    old_value = from_record(before[0], CareOrderVersion).structured_instruction
    assert isinstance(old_value, OrderCandidate)
    assert old_value == initial.candidate.orders[0]
    old_head = store.list_records(patient.scope, "care_order_head")[0][0]
    source = (
        "Synthetic Person زود الكونكور لـ 10"
        if old == "Concor"
        else "Synthetic Person. Increase Exforge 5/160 to Exforge HCT 5/160/12.5."
    )
    value: dict[str, Any] = {"action": "change", "drug": new, "dose": new_dose}
    if old != new:
        value.update(previous_drug=old, previous_dose=old_dose)
    p = w.dictate(
        source, {"patient": {"name_as_spoken": "Synthetic Person"}, "orders": [value]}, id=11
    )
    blocked = new_dose is None or (new != old and new_dose == old_dose)
    assert p.blocked("order:0") == blocked
    if new_dose is None:
        assert p.candidate.orders[0].dose is None
        assert any(i.item == "order:0" and i.blocked for i in p.issues)
    card = "\n".join(render_card(p, cast(Language, language)))
    if new == "Exforge HCT" and new_dose == "5/160/12.5":
        assert card == EXFORGE_COMPLETE_CARDS[language]
    if new == "Concor" and new_dose is None:
        assert card == CONCOR_BLOCKED_CARDS[language]
    assert any(call.payload.get("text") == card for call in w.transport.calls)
    print(f"11k {language} {old}->{new} {new_dose}:\n{card}")
    w.tap(button, id=21)
    after = store.list_records(patient.scope, "care_order_version")[0]
    assert before[0] in after  # immutable prior instruction and provenance
    changes = [
        r
        for r in store.list_records(patient.scope, "mission")[0]
        if getattr(from_record(r, Mission).details, "action", None) == "CHANGE"
    ]
    if blocked:
        assert after == before
        assert not changes
        assert store.list_records(patient.scope, "care_order_head")[0][0] == old_head
    else:
        assert len(after) == 2 and len(changes) == 1
        saved = from_record(next(r for r in after if r.id != before[0].id), CareOrderVersion)
        assert saved.order_id == old_head.id
        assert saved.structured_instruction == OrderCandidate(**value)
        assert saved.structured_instruction.model_dump() == p.candidate.orders[0].model_dump()
        assert order_line(saved, "en") == f"Your doctor prescribed: {new}, {new_dose}"
        assert order_line(saved, "ar") == f"الدكتور قالك: {new}، {new_dose}"


@pytest.mark.parametrize(
    "old,new",
    [("Atacand", "Atacand Plus"), ("Micardis", "Micardis Plus"), ("Galvus", "Galvus Met")],
)
def test_11k_family_policy_still_refuses_switch(
    store: StoreBase, clock: FakeClock, old: str, new: str
) -> None:
    from sanad.scribe.records import CareOrderVersion

    w = ScribeWorld.create(store, clock)
    w.approve(language="en")
    patient = w.named_stub("Synthetic Person")
    initial = w.dictate(
        f"Synthetic Person. Taking {old} 5.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "continue", "drug": old, "dose": "5"}],
        },
        id=8,
    )
    w.tap("✅ Confirm", id=9)
    before = store.list_records(patient.scope, "care_order_version")[0]
    assert len(before) == 1
    assert (
        from_record(before[0], CareOrderVersion).structured_instruction
        == initial.candidate.orders[0]
    )
    changed = w.dictate(
        f"Synthetic Person. Switch {old} 5 to {new} 5/10.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [
                {
                    "action": "change",
                    "drug": new,
                    "dose": "5/10",
                    "previous_drug": old,
                    "previous_dose": "5",
                }
            ],
        },
        id=11,
    )
    assert changed.blocked("order:0")
    w.tap("✅ Confirm", id=21)
    assert store.list_records(patient.scope, "care_order_version")[0] == before


@pytest.mark.parametrize("language", ["en", "ar"])
@pytest.mark.parametrize("dose", ["5/160/12.5", "5/160"])
def test_11k_correction_cannot_authorize_partial_quantity(
    store: StoreBase, clock: FakeClock, language: str, dose: str
) -> None:
    from typing import cast

    from sanad.domain.language import Language
    from sanad.scribe.extract import OrderCandidate
    from sanad.scribe.records import CareOrderVersion

    w = ScribeWorld.create(store, clock)
    w.approve(language=cast(Language, language))
    patient = w.named_stub("Synthetic Person")
    seed = w.dictate(
        "Synthetic Person. Taking Exforge 5/160.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "continue", "drug": "Exforge", "dose": "5/160"}],
        },
        id=8,
    )
    w.tap("✅ Confirm" if language == "en" else "✅ تمام", id=9)
    before = store.list_records(patient.scope, "care_order_version")[0]
    assert len(before) == 1
    assert (
        from_record(before[0], CareOrderVersion).structured_instruction == seed.candidate.orders[0]
    )
    first = w.dictate(
        "Synthetic Person. Taking Exforge 5/160. Increase to Exforge HCT.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [
                {
                    "action": "change",
                    "drug": "Exforge HCT",
                    "previous_drug": "Exforge",
                    "previous_dose": "5/160",
                }
            ],
        },
    )
    value = first.candidate.model_dump()
    value["orders"][0]["dose"] = dose
    second = w.dictate("Exforge HCT 5/160/12.5", value, id=11)
    assert second.source_partition
    extent = second.source_partition.corrections[-1]
    assert extent.proposal_version == first.version and extent.proposal_id == first.id
    assert second.source_partition.original_end == len(first.source_text)
    from sanad.scribe.grounding import Claim, valid_record

    claim = Claim("order:0", "dose", dose)
    evidence = [e for e in second.evidence if e.item == claim.item and e.field == claim.field]
    assert any(valid_record(e, claim, second) for e in evidence) == (dose == "5/160/12.5")
    w.tap("✅ Confirm" if language == "en" else "✅ تمام", id=21)
    rows = store.list_records(patient.scope, "care_order_version")[0]
    if dose == "5/160/12.5":
        assert len(rows) == 2 and before[0] in rows
        saved = from_record(next(r for r in rows if r.id != before[0].id), CareOrderVersion)
        assert saved.structured_instruction == OrderCandidate(
            action="change",
            drug="Exforge HCT",
            dose=dose,
            previous_drug="Exforge",
            previous_dose="5/160",
        )
    else:
        assert rows == before


@pytest.mark.parametrize(
    "origin", ["transcript_span", "authorized_correction", "stored_prior_order", "code_computed"]
)
def test_11k_reloaded_defective_evidence_cannot_commit(
    store: StoreBase, clock: FakeClock, origin: str
) -> None:
    from sanad.scribe.grounding import (
        Claim as EvidenceClaim,
    )
    from sanad.scribe.grounding import (
        FieldEvidence,
        _fingerprint,
        valid_record,
    )
    from sanad.scribe.proposal import ScribeCallback
    from sanad.scribe.records import CareOrderVersion

    w = ScribeWorld.create(store, clock)
    w.approve(language="en")
    patient = w.named_stub("Synthetic Person")
    initial = w.dictate(
        "Synthetic Person. Taking Exforge 5/160.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "continue", "drug": "Exforge", "dose": "5/160"}],
        },
        id=8,
    )
    w.tap("✅ Confirm", id=9)
    before = store.list_records(patient.scope, "care_order_version")[0]
    assert (
        from_record(before[0], CareOrderVersion).structured_instruction
        == initial.candidate.orders[0]
    )
    p = w.dictate(
        "Synthetic Person. Increase Exforge 5/160 to Exforge HCT 5/160/12.5.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [
                {
                    "action": "change",
                    "drug": "Exforge HCT",
                    "dose": "5/160/12.5",
                    "previous_drug": "Exforge",
                    "previous_dose": "5/160",
                }
            ],
        },
        id=11,
    )
    field = "previous_dose" if origin == "code_computed" else "dose"
    wrong = "5/160/12.5" if field == "previous_dose" else "5/160"
    order = p.candidate.orders[0].model_copy(update={field: wrong})
    altered = p.model_copy(
        update={"candidate": p.candidate.model_copy(update={"orders": (order,)}), "issues": ()}
    )
    start = p.source_text.index("5/160/12.5")
    drug = p.source_text.index("Exforge HCT")
    from sanad.scribe.grounding import _prior_ref

    data: dict[str, Any] = dict(
        item="order:0",
        field=field,
        value=wrong,
        origin=origin,
        source_ref=_prior_ref(p, "order:0")
        if origin == "stored_prior_order"
        else p.source_receipt_id,
        offsets=((drug, drug + len("Exforge HCT")), (start, start + len(wrong)))
        if origin == "transcript_span"
        else ((start, start + len(wrong)),)
        if origin == "authorized_correction"
        else (),
        transformation={
            "transcript_span": "instruction_attachment",
            "authorized_correction": "answer_slot:order:0",
            "stored_prior_order": "unchanged_prior_field",
            "code_computed": "validated_previous_instruction",
        }[origin],
    )
    record = FieldEvidence(**data)
    altered = altered.model_copy(
        update={"evidence": tuple(e for e in p.evidence if e.field != field) + (record,)}
    )
    altered = altered.model_copy(update={"evidence_fingerprint": _fingerprint(altered)})
    reloaded = Proposal.model_validate_json(altered.model_dump_json())
    assert not valid_record(record, EvidenceClaim("order:0", field, wrong), reloaded)
    token = ScribeCallback(
        id=p.confirmation_nonce_hash,
        scope=p.scope,
        proposal_id=p.id,
        proposal_version=p.version,
        actor_subject=w.actor(APPLICANT).subject,
        action="confirm",
        expires_at=p.expires_at,
        created_at=clock(),
        updated_at=clock(),
    )
    outcome = w.scribe.committer.confirm(reloaded, token, w.actor(APPLICANT), "11k-injected")
    assert outcome.status == "clarification"
    assert store.list_records(patient.scope, "care_order_version")[0] == before


@pytest.mark.parametrize("dose", [None, "5/160/12.5"])
def test_11k_photo_plus_dictation_change_preserves_stored_instruction(
    store: StoreBase, clock: FakeClock, dose: str | None
) -> None:
    from sanad.scribe.extract import OrderCandidate
    from sanad.scribe.records import CareOrderVersion

    w = ScribeWorld.create(store, clock)
    w.approve()
    patient = w.named_stub("أحمد رضا")
    seed = w.dictate(
        "أحمد رضا Taking Exforge 5/160",
        {
            "patient": {"name_as_spoken": "أحمد رضا"},
            "orders": [{"action": "continue", "drug": "Exforge", "dose": "5/160"}],
        },
        id=8,
    )
    w.tap(id=9)
    before = store.list_records(patient.scope, "care_order_version")[0]
    assert len(before) == 1
    assert (
        from_record(before[0], CareOrderVersion).structured_instruction == seed.candidate.orders[0]
    )
    providers(
        w, prescription("5/160/12.5", "Exforge HCT"), prescription("5/160/12.5", "Exforge HCT")
    )
    w.post(photo(id=10))
    first = w.proposal
    assert first.photo
    corrected = w.dictate(
        "Increase Exforge 5/160 to Exforge HCT 5/160/12.5",
        {
            "patient": {"name_as_spoken": "أحمد رضا"},
            "orders": [
                {
                    "action": "change",
                    "drug": "Exforge HCT",
                    "dose": dose,
                    "previous_drug": "Exforge",
                    "previous_dose": "5/160",
                }
            ],
        },
        id=11,
    )
    assert corrected.photo and corrected.id == first.id
    assert corrected.candidate.orders[0].dose == dose
    assert corrected.blocked("order:0") == (dose is None)
    if dose:
        w.tap(id=21)
    else:
        from sanad.scribe.proposal import ScribeCallback

        token = ScribeCallback(
            id=corrected.confirmation_nonce_hash,
            scope=corrected.scope,
            proposal_id=corrected.id,
            proposal_version=corrected.version,
            actor_subject=w.actor(APPLICANT).subject,
            action="confirm",
            expires_at=corrected.expires_at,
            created_at=clock(),
            updated_at=clock(),
        )
        assert (
            w.scribe.committer.confirm(
                corrected, token, w.actor(APPLICANT), "11k-photo-blocked"
            ).status
            == "clarification"
        )
    rows = store.list_records(patient.scope, "care_order_version")[0]
    if dose:
        assert len(rows) == 2 and before[0] in rows
        saved = from_record(next(r for r in rows if r.id != before[0].id), CareOrderVersion)
        assert saved.order_id == before[0].body["order_id"]
        assert saved.structured_instruction == OrderCandidate(
            action="change",
            drug="Exforge HCT",
            dose=dose,
            previous_drug="Exforge",
            previous_dose="5/160",
        )
    else:
        assert rows == before


EXFORGE_COMPLETE_CARDS = {
    "en": (
        "Patient: Synthetic Person\n"
        "Medications:\n"
        "Exforge 5/160 → Exforge HCT 5/160/12.5 (change)\n"
        "✅ Confirm | ✏️ Edit | ❌ Cancel\n"
        "valid 30 minutes"
    ),
    "ar": (
        "المريض: Synthetic Person\n"
        "الأدوية:\n"
        "Exforge HCT 5/160/12.5 (تغيير)\n"
        "Exforge 5/160 ← Exforge HCT 5/160/12.5\n"
        "✅ تمام | ✏️ تعديل | ❌ إلغاء\n"
        "صالح 30 دقيقة"
    ),
}
CONCOR_BLOCKED_CARDS = {
    "en": (
        "Patient: Synthetic Person\n"
        "Medications:\n"
        "Concor 5 مج → Concor (change)\n"
        "Needs confirmation:\n"
        "What dose of Concor did you intend?\n"
        'I heard "10"; which item does it belong to?\n'
        "✅ Confirm | ✏️ Edit | ❌ Cancel\n"
        "valid 30 minutes"
    ),
    "ar": (
        "المريض: Synthetic Person\n"
        "الأدوية:\n"
        "Concor (تغيير)\n"
        "Concor: 5 مج ← \n"
        "محتاج تأكيد:\n"
        'جرعة "Concor" إيه؟\n'
        'سمعت "10"، ده يخص إيه؟\n'
        "✅ تمام | ✏️ تعديل | ❌ إلغاء\n"
        "صالح 30 دقيقة"
    ),
}


def test_11k_multiline_original_and_two_correction_versions(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.scribe.extract import OrderCandidate
    from sanad.scribe.records import CareOrderVersion

    w = ScribeWorld.create(store, clock)
    w.approve(language="en")
    patient = w.named_stub("Synthetic Person")
    initial = w.dictate(
        "Synthetic Person. Taking Exforge 5/160.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "continue", "drug": "Exforge", "dose": "5/160"}],
        },
        id=8,
    )
    w.tap("✅ Confirm", id=9)
    before = store.list_records(patient.scope, "care_order_version")[0]
    assert (
        from_record(before[0], CareOrderVersion).structured_instruction
        == initial.candidate.orders[0]
    )
    first = w.dictate(
        "Synthetic Person. Taking Exforge 5/160.\nRequest CBC.\nIncrease Exforge to Exforge HCT.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [
                {
                    "action": "change",
                    "drug": "Exforge HCT",
                    "previous_drug": "Exforge",
                    "previous_dose": "5/160",
                }
            ],
            "missions": [{"kind": "TEST", "text": "CBC"}],
        },
    )
    old_button = w.button("✅ Confirm")
    value = first.candidate.model_dump()
    value["orders"][0]["dose"] = "5/160/12.5"
    second = w.dictate("Exforge HCT 5/160/12.5", value, id=11)
    value = second.candidate.model_dump()
    value["orders"][0]["frequency"] = "daily"
    third = w.dictate("Exforge HCT daily", value, id=12)
    assert third.source_partition and third.source_partition.original_end == len(first.source_text)
    assert [c.proposal_version for c in third.source_partition.corrections] == [
        first.version,
        second.version,
    ]
    assert not third.blocked("order:0")
    dose_evidence = next(e for e in third.evidence if e.item == "order:0" and e.field == "dose")
    assert dose_evidence.correction_version == first.version
    w.tap(raw=old_button, id=20)
    assert store.list_records(patient.scope, "care_order_version")[0] == before
    w.tap("✅ Confirm", id=21)
    after = store.list_records(patient.scope, "care_order_version")[0]
    assert len(after) == 2 and before[0] in after
    saved = from_record(next(r for r in after if r.id != before[0].id), CareOrderVersion)
    assert saved.structured_instruction == OrderCandidate(
        action="change",
        drug="Exforge HCT",
        dose="5/160/12.5",
        frequency="daily",
        previous_drug="Exforge",
        previous_dose="5/160",
    )


@pytest.mark.parametrize("dose", [None, "10/160/25"])
def test_11k_cross_clause_non_dictionary_strength_stored_exactly(
    store: StoreBase, clock: FakeClock, dose: str | None
) -> None:
    from sanad.scribe.extract import OrderCandidate
    from sanad.scribe.records import CareOrderVersion

    w = ScribeWorld.create(store, clock)
    w.approve(language="en")
    patient = w.named_stub("Synthetic Person")
    initial = w.dictate(
        "Synthetic Person. Taking Exforge 5/160.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "continue", "drug": "Exforge", "dose": "5/160"}],
        },
        id=8,
    )
    w.tap("✅ Confirm", id=9)
    before = store.list_records(patient.scope, "care_order_version")[0]
    assert len(before) == 1
    assert (
        from_record(before[0], CareOrderVersion).structured_instruction
        == initial.candidate.orders[0]
    )
    expected = OrderCandidate(
        action="change",
        drug="Exforge HCT",
        dose=dose,
        previous_drug="Exforge",
        previous_dose="5/160",
    )
    p = w.dictate(
        "Synthetic Person. On Exforge 5 over 160. I ordered CBC. "
        "Upgrade the Exforge to be Exforge HCT 10/160/25.",
        {"patient": {"name_as_spoken": "Synthetic Person"}, "orders": [expected.model_dump()]},
        id=11,
    )
    assert p.candidate.orders[0].dose == dose
    assert p.blocked("order:0") == (dose is None)
    w.tap("✅ Confirm", id=21)
    after = store.list_records(patient.scope, "care_order_version")[0]
    if dose is None:
        assert after == before
    else:
        assert len(after) == 2 and before[0] in after
        saved = from_record(next(r for r in after if r.id != before[0].id), CareOrderVersion)
        assert saved.structured_instruction == expected


@pytest.mark.parametrize("dose", ["5/160/12.5", "5/160"])
def test_11k_explicit_correct_reply_callback_keeps_partition_and_stored_value(
    store: StoreBase, clock: FakeClock, dose: str
) -> None:
    from providers.fixtures import ScriptedModel, candidate

    from sanad.scribe.extract import OrderCandidate
    from sanad.scribe.records import CareOrderVersion

    w = ScribeWorld.create(store, clock)
    w.approve()
    patient = w.named_stub("أحمد رضا")
    initial = w.dictate(
        "أحمد رضا Taking Exforge 5/160",
        {
            "patient": {"name_as_spoken": "أحمد رضا"},
            "orders": [{"action": "continue", "drug": "Exforge", "dose": "5/160"}],
        },
        id=8,
    )
    w.tap(id=9)
    before = store.list_records(patient.scope, "care_order_version")[0]
    assert len(before) == 1
    assert (
        from_record(before[0], CareOrderVersion).structured_instruction
        == initial.candidate.orders[0]
    )
    first = w.dictate(
        "أحمد رضا Increase Exforge 5/160 to Exforge HCT.",
        {
            "patient": {"name_as_spoken": "أحمد رضا"},
            "orders": [
                {
                    "action": "change",
                    "drug": "Exforge HCT",
                    "previous_drug": "Exforge",
                    "previous_dose": "5/160",
                }
            ],
        },
        id=10,
    )
    w.scribe.model_factory = lambda registry, role: ScriptedModel()
    w.post(update(APPLICANT, "Exforge HCT 5/160/12.5 وفيه مريض تاني مريم تجربة محتاجة CBC", 11))
    pending = w.proposal
    assert pending.pending_reply
    value = first.candidate.model_dump()
    value["orders"][0]["dose"] = dose
    w.scribe.model_factory = lambda registry, role: ScriptedModel(candidate(value))
    w.tap("تعديل للكارت", id=12)
    corrected = w.proposal
    assert corrected.source_partition
    assert corrected.source_partition.original_end == len(first.source_text)
    assert corrected.source_partition.corrections[-1].proposal_version == pending.version
    assert corrected.blocked("order:0") == (dose != "5/160/12.5")
    w.tap(id=21)
    after = store.list_records(patient.scope, "care_order_version")[0]
    if dose == "5/160/12.5":
        assert len(after) == 2 and before[0] in after
        saved = from_record(next(r for r in after if r.id != before[0].id), CareOrderVersion)
        assert saved.structured_instruction == OrderCandidate(
            action="change",
            drug="Exforge HCT",
            dose=dose,
            previous_drug="Exforge",
            previous_dose="5/160",
        )
    else:
        assert after == before
