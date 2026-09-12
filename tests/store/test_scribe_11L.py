"""11L's unknown instruction and disputed patient cross both persistence modes."""

from typing import Any

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate

from sanad.scribe.card import render_card
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.grounding import inventory
from sanad.scribe.proposal import Proposal, ScribeCallback
from sanad.scribe.records import CareOrderVersion
from sanad.store._base import StoreBase
from sanad.store.records import from_record
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld


def confirmation(world: ScribeWorld, p: Proposal) -> str:
    token = ScribeCallback(
        id=p.confirmation_nonce_hash,
        scope=p.scope,
        proposal_id=p.id,
        proposal_version=p.version,
        actor_subject=world.actor(APPLICANT).subject,
        action="confirm",
        expires_at=p.expires_at,
        created_at=world.clock(),
        updated_at=world.clock(),
    )
    result = world.scribe.committer.confirm(p, token, world.actor(APPLICANT), "11L-refusal")
    return result.status


def assert_refused(world: ScribeWorld, p: Proposal, *, check_button: bool = True) -> None:
    assert confirmation(world, p) == "clarification"
    if not check_button:
        return
    with pytest.raises(AssertionError, match="card button missing"):
        world.button("✅ Confirm")


@pytest.mark.parametrize("correction", [None, "Forxiga", "Concor"])
@pytest.mark.parametrize("unknown_reader", [0, 1, "both"])
@pytest.mark.usefixtures("legacy_dictation_schema")
def test_unknown_action_and_corrections(
    store: StoreBase, clock: FakeClock, correction: str | None, unknown_reader: int | str
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    patient = world.named_stub("Synthetic Person")
    values: list[dict[str, Any]] = [
        {"patient": {"name_as_spoken": "Synthetic Person"}},
        {"patient": {"name_as_spoken": "Synthetic Person"}},
    ]
    for i, value in enumerate(values):
        if unknown_reader in {i, "both"}:
            value["orders"] = [{"action": "add", "drug": "Forxiga"}]
    model = ScriptedModel(*(candidate(v) for v in values))
    world.scribe.model_factory = lambda *_: model
    world.post(update(APPLICANT, "Synthetic Person. Add Forxiga.", 10))
    p = world.proposal
    assert len(model.script.calls) == 2
    assert p.candidate.orders == ()
    assert p.candidate.ambiguities == ("add Forxiga",)
    assert '\nI heard "add Forxiga"; please clarify.' in "\n".join(render_card(p))
    assert_refused(world, p)
    assert not store.list_records(patient.scope, "care_order_version")[0]
    if correction is None:
        return
    dose = "10" if correction == "Forxiga" else "5"
    p = world.dictate(
        f"start {correction} {dose}",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "start", "drug": correction, "dose": dose}],
        },
        id=11,
    )
    if correction == "Concor":
        assert p.candidate.ambiguities == ("add Forxiga",)
        assert_refused(world, p)
        assert not store.list_records(patient.scope, "care_order_version")[0]
        return
    assert p.candidate.ambiguities == ()
    assert not p.blocked("all") and not p.blocked("order:0")
    assert len(p.candidate.orders) == 1
    assert 'I heard "add Forxiga"' not in "\n".join(render_card(p))
    raw = world.button("✅ Confirm")
    world.tap(raw=raw, id=20)
    world.tap(raw=raw, id=21)
    rows = store.list_records(patient.scope, "care_order_version")[0]
    assert len(rows) == 1
    instruction = from_record(rows[0], CareOrderVersion).structured_instruction
    assert isinstance(instruction, OrderCandidate)
    assert (instruction.action, instruction.drug, instruction.dose) == ("start", "Forxiga", "10")


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_disputed_patient_readings_persist_as_questions_only(
    store: StoreBase, clock: FakeClock
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    values = [
        {
            "patient": {"name_as_spoken": name},
            "orders": [{"action": "start", "drug": "Forxiga", "dose": "10"}],
        }
        for name in ("Synthetic Person", "Synthetic Pearson")
    ]
    model = ScriptedModel(*(candidate(v) for v in values))
    world.scribe.model_factory = lambda *_: model
    world.post(update(APPLICANT, "New patient Synthetic Person. Start Forxiga 10.", 10))
    p = world.proposal
    issue = next(i for i in p.issues if i.item == "patient" and i.code == "extraction_conflict")
    assert issue.alternatives == ("Synthetic Person", "Synthetic Pearson")
    assert (
        'I heard the patient as "Synthetic Person" and as "Synthetic Pearson"; which is right?'
        in "\n".join(render_card(p))
    )
    assert not any(c.value == "Synthetic Pearson" for c in inventory(p))
    assert_refused(world, p)
    assert not world.scribe.repo.store.list_records(p.scope, "patient")[0]


def unknown_action_world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    world.named_stub("Synthetic Person")
    p = world.dictate(
        "Synthetic Person. Add Forxiga.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "add", "drug": "Forxiga"}],
        },
        id=10,
    )
    assert p.candidate.ambiguities == ("add Forxiga",)
    assert_refused(world, p)
    return world


@pytest.mark.parametrize(
    "reply,dose",
    [
        ("do not start Forxiga 10", "10"),
        ("start Forxiga", None),
        ("start Forxiga 10/5", "10"),
        ("start Forxiga. Start Concor 10", "10"),
        ("start Forxiga and Concor 10", "10"),
        ("Forxiga 10", "10"),
    ],
)
@pytest.mark.usefixtures("legacy_dictation_schema")
def test_reply_instruction_negatives(
    store: StoreBase, clock: FakeClock, reply: str, dose: str | None
) -> None:
    world = unknown_action_world(store, clock)
    p = world.dictate(
        reply,
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "start", "drug": "Forxiga", "dose": dose}],
        },
        id=11,
    )
    assert p.blocked("order:0")
    assert confirmation(world, p) in {"accepted", "clarification"}
    assert p.selected_patient_id
    patient = world.claims.patient(world.doctor.id, p.selected_patient_id)
    assert patient
    assert not store.list_records(patient.scope, "care_order_version")[0]
    assert "clarify" in "\n".join(render_card(p)) or "dose" in "\n".join(render_card(p))


@pytest.mark.parametrize(
    "field,value",
    [
        ("correction_version", 999),
        ("correction_id", "stale-card"),
        ("source_ref", "wrong-receipt"),
        ("offsets", ((0, 1),)),
        ("transformation", "answer_slot:order:0"),
    ],
)
@pytest.mark.usefixtures("legacy_dictation_schema")
def test_reply_evidence_tamper_refuses_confirmation(
    store: StoreBase, clock: FakeClock, field: str, value: Any
) -> None:
    from sanad.scribe.grounding import Claim, valid_record

    world = unknown_action_world(store, clock)
    p = world.dictate(
        "start Forxiga 10",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "start", "drug": "Forxiga", "dose": "10"}],
        },
        id=11,
    )
    record = next(e for e in p.evidence if e.item == "order:0" and e.field == "dose")
    assert record.origin == "authorized_correction"
    assert record.transformation == "reply_instruction:order:0"
    assert valid_record(record, Claim("order:0", "dose", "10"), p)
    tampered = record.model_copy(update={field: value})
    assert not valid_record(tampered, Claim("order:0", "dose", "10"), p)
    changed = p.model_copy(
        update={"evidence": tuple(tampered if e == record else e for e in p.evidence)}
    )
    assert changed.evidence_fingerprint == p.evidence_fingerprint
    assert changed.blocked("order:0")
    assert_refused(world, changed, check_button=False)


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_subsequent_reply_preserves_valid_evidence_but_rejects_stale_replay(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.scribe.grounding import Claim, valid_record

    world = unknown_action_world(store, clock)
    p = world.dictate(
        "start Forxiga 10",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "start", "drug": "Forxiga", "dose": "10"}],
        },
        id=11,
    )
    first = next(e for e in p.evidence if e.item == "order:0" and e.field == "dose")
    second = world.dictate(
        "Forxiga twice daily",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [
                {"action": "start", "drug": "Forxiga", "dose": "10", "frequency": "twice daily"}
            ],
        },
        id=12,
    )
    kept = next(e for e in second.evidence if e.item == "order:0" and e.field == "dose")
    assert kept.transformation == "reply_instruction:order:0"
    assert kept.correction_version == first.correction_version
    assert valid_record(kept, Claim("order:0", "dose", "10"), second)
    assert not valid_record(first, Claim("order:0", "dose", "10"), second)
    assert not second.blocked("order:0")
    assert second.source_partition
    latest = second.source_partition.corrections[-1]
    relabeled = kept.model_copy(
        update={"correction_id": latest.proposal_id, "correction_version": latest.proposal_version}
    )
    assert not valid_record(relabeled, Claim("order:0", "dose", "10"), second)
    raw = world.button("✅ Confirm")
    world.tap(raw=raw, id=20)
    world.tap(raw=raw, id=21)
    # Intake proposals use a tenant scope; resolve the selected patient's partition.
    assert second.selected_patient_id
    patient = world.claims.patient(world.doctor.id, second.selected_patient_id)
    assert patient
    rows = store.list_records(patient.scope, "care_order_version")[0]
    assert len(rows) == 1
    saved = from_record(rows[0], CareOrderVersion).structured_instruction
    assert isinstance(saved, OrderCandidate)
    assert (saved.drug, saved.dose, saved.frequency) == ("Forxiga", "10", "twice daily")
