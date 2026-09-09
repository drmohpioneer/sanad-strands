"""Contract 19: synthetic persistence and race cases on both stores."""

from collections.abc import Mapping
from datetime import timedelta
from uuid import uuid4

import pytest
from harness import FakeClock

from sanad.domain import Mission
from sanad.steward.types import CommandResult
from sanad.store._base import StoreBase
from sanad.store.records import CommandEnvelope, from_record, to_record
from store import evidence_fixtures as f
from store.concierge_fixtures import PatientWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> PatientWorld:
    return f.world(store, clock)


def act(w: PatientWorld, payload: Mapping[str, object], id: str | None = None) -> CommandResult:
    command = CommandEnvelope.model_validate(
        {
            "command_id": id or uuid4().hex,
            "scope": w.patient_scope,
            "principal": w.owner,
            "requested_at": w.clock(),
            "payload": payload,
        }
    )
    return w.runtime.steward.handle(command)


def upload(w: PatientWorld) -> None:
    f.mission(w, order_refs=())
    f.providers(w)
    assert f.upload(w) == "accepted"


def test_correction_preserves_source_and_invalidates_reliance(world: PatientWorld) -> None:
    upload(world)
    old = f.current(world)
    before = world.store.get_mission(world.patient_scope, old.mission_id or "")
    assert before
    command = {
        "type": "CorrectRecord",
        "predecessor": to_record(old, old.scope).ref.model_dump(),
        "operation": "detach",
        "reason": "Wrong chart",
    }
    world.clock.advance(timedelta(days=40))
    result = act(world, command, "detach-one")
    assert result.status == "accepted", result
    current = f.current(world)
    assert current.association_state == "detached"
    assert world.store.get(old.scope, "evidence", old.id) == to_record(old, old.scope)
    assert current.readers == old.readers and current.provenance == old.provenance
    assert current.content_hash == old.content_hash and current.observation_id == old.observation_id
    mission = world.store.get_mission(world.patient_scope, old.mission_id or "")
    assert mission
    assert mission.fulfillment_validity == "invalidated_pending_review"
    for name in (
        "state",
        "fulfillment_event_id",
        "fulfilled_at",
        "objective_received_at",
        "timeliness",
        "confirmed_by",
    ):
        assert getattr(mission, name) == getattr(before, name)
    assert act(world, command, "detach-one").status == "accepted"
    assert len(world.rows("correction")) == 1
    assert act(world, command).status == "invalid_input"


def test_satisfying_correction_then_review_restores_validity(world: PatientWorld) -> None:
    upload(world)
    old = f.current(world)
    assert (
        act(
            world,
            {
                "type": "CorrectRecord",
                "predecessor": to_record(old, old.scope).ref.model_dump(),
                "operation": "replace",
                "row_index": 0,
                "changes": {"unit": None},
                "reason": "Unit was unreadable",
            },
        ).status
        == "accepted"
    )
    changed = f.current(world)
    assert (
        act(
            world,
            {
                "type": "CorrectRecord",
                "predecessor": to_record(changed, changed.scope).ref.model_dump(),
                "operation": "replace",
                "row_index": 0,
                "changes": {"unit": "mmol/L"},
                "reason": "Doctor checked original unit",
            },
        ).status
        == "accepted"
    )
    mission = world.store.get_mission(world.patient_scope, old.mission_id or "")
    assert mission and mission.fulfillment_validity == "invalidated_pending_review"
    correction = next(r for r in world.rows("correction") if r.id == f.current(world).correction_id)
    review = next(r for r in world.rows("review") if r.body.get("source_id") == correction.id)
    outcome = act(
        world,
        {
            "type": "ValidateCorrection",
            "correction_id": correction.id,
            "mission_ref": to_record(mission, world.patient_scope).ref.model_dump(),
            "review_ref": review.ref.model_dump(),
            "reason": "Reviewed current predicate",
        },
    )
    assert outcome.status == "accepted", outcome
    latest = world.store.get_mission(world.patient_scope, mission.id)
    assert latest and latest.fulfillment_validity == "valid"
    assert world.store.get(world.patient_scope, "review", review.id).body["state"] == "resolved"  # type: ignore[union-attr]

    # Recording a doctor response first cannot strand later reviewed validation.
    assert correct_value(world, changes={"unit": None}).status == "accepted"
    assert correct_value(world, changes={"unit": "mmol/L"}).status == "accepted"
    correction = next(r for r in world.rows("correction") if r.id == f.current(world).correction_id)
    review = next(r for r in world.rows("review") if r.body.get("source_id") == correction.id)
    assert (
        act(
            world,
            {
                "type": "CorrectionResponse",
                "correction_id": correction.id,
                "review_ref": review.ref.model_dump(),
                "reason": "Doctor recorded separate response",
            },
        ).status
        == "accepted"
    )
    resolved = world.store.get(world.patient_scope, "review", review.id)
    mission = world.store.get_mission(world.patient_scope, old.mission_id or "")
    assert resolved and mission and mission.fulfillment_validity == "invalidated_pending_review"
    assert (
        act(
            world,
            {
                "type": "ValidateCorrection",
                "correction_id": correction.id,
                "mission_ref": to_record(mission, world.patient_scope).ref.model_dump(),
                "review_ref": resolved.ref.model_dump(),
                "reason": "Now reviewed current evidence",
            },
        ).status
        == "accepted"
    )
    latest = world.store.get_mission(world.patient_scope, mission.id)
    assert latest and latest.fulfillment_validity == "valid"
    assert world.store.get(world.patient_scope, "review", review.id) == resolved


@pytest.mark.parametrize("field", ["predicate_still_holds", "actor_id", "delete", "mission_id"])
def test_caller_cannot_supply_predicate_authority_or_delete(
    world: PatientWorld, field: str
) -> None:
    upload(world)
    old = f.current(world)
    result = act(
        world,
        {
            "type": "CorrectRecord",
            "predecessor": to_record(old, old.scope).ref.model_dump(),
            "operation": "detach",
            "reason": "Wrong chart",
            field: True,
        },
    )
    assert result.status == "invalid_input"
    assert f.current(world) == old


def correct_value(
    w: PatientWorld, *, changes: dict[str, object] | None = None, detach: bool = False
) -> CommandResult:
    e = f.current(w)
    return act(
        w,
        {
            "type": "CorrectRecord",
            "predecessor": to_record(e, e.scope).ref.model_dump(),
            "operation": "detach" if detach else "replace",
            "reason": "Doctor checked original",
            **({} if detach else {"row_index": 0, "changes": changes or {"value": "4.8"}}),
        },
    )


def test_correction_is_not_a_late_fresh_upload(world: PatientWorld) -> None:
    from providers.fixtures import png

    upload(world)
    first = f.current(world)
    assert correct_value(world).status == "accepted"
    correcting = f.current(world)
    assert correcting.observation_id == first.observation_id
    assert correcting.supersedes_evidence_version == first.version
    world.clock.advance(timedelta(days=4))
    f.providers(world, f.lab("4.9"), f.lab("4.9"), data=png(34, 35))
    assert f.upload(world, id=402) == "accepted"
    heads = world.rows("evidence_head")
    assert len(heads) == 2
    late_head = next(h for h in heads if h.id != first.evidence_id)
    late_row = world.store.get(
        world.patient_scope, "evidence", f"{late_head.id}:{late_head.body['current_version']}"
    )
    assert late_row
    assert late_row.body["observation_id"] != first.observation_id
    assert late_row.body.get("correction_id") is None
    assert len(world.rows("correction")) == 1
    assert world.store.get(first.scope, "evidence", correcting.id) == to_record(
        correcting, first.scope
    )


@pytest.mark.parametrize(
    "failure", ["blank_reason", "missing_source", "noncurrent", "cross_scope", "forged_actor"]
)
def test_invalid_predecessor_and_authority_leave_no_correction(
    world: PatientWorld, failure: str
) -> None:
    upload(world)
    old = f.current(world)
    ref = to_record(old, old.scope).ref
    if failure in {"missing_source", "cross_scope"}:
        ref = ref.model_copy(update={"id": "foreign-evidence:1"})
    elif failure == "noncurrent":
        assert correct_value(world).status == "accepted"
    before = len(world.rows("correction"))
    command = CommandEnvelope.model_validate(
        {
            "command_id": uuid4().hex,
            "scope": world.patient_scope,
            "principal": world.owner.model_copy(update={"subject": "another-doctor"})
            if failure == "forged_actor"
            else world.owner,
            "requested_at": world.clock(),
            "payload": {
                "type": "CorrectRecord",
                "predecessor": ref.model_dump(),
                "operation": "detach",
                "reason": "   " if failure == "blank_reason" else "Wrong patient",
            },
        }
    )
    assert world.runtime.steward.handle(command).status in {"invalid_input", "forbidden"}
    assert len(world.rows("correction")) == before


def test_crash_before_atomic_commit_and_retry(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload(world)
    old = f.current(world)
    original = world.store.commit

    def crash(request):  # type: ignore[no-untyped-def]
        if request.command.payload.get("type") == "CorrectRecord":
            raise RuntimeError("synthetic crash before commit")
        return original(request)

    monkeypatch.setattr(world.store, "commit", crash)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        correct_value(world, detach=True)
    assert f.current(world) == old
    assert not world.rows("correction")
    monkeypatch.setattr(world.store, "commit", original)
    assert correct_value(world, detach=True).status == "accepted"


def test_forged_write_set_is_refused(world: PatientWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    upload(world)
    original = world.store.commit

    def forge(request):  # type: ignore[no-untyped-def]
        if request.command.payload.get("type") == "CorrectRecord":
            request = request.model_copy(
                update={"puts": tuple(r for r in request.puts if r.entity_type != "mission")}
            )
        return original(request)

    monkeypatch.setattr(world.store, "commit", forge)
    assert correct_value(world, detach=True).status == "forbidden"
    assert not world.rows("correction")
    assert f.current(world).association_state == "accepted"


def test_corrected_critical_value_retains_existing_incident(world: PatientWorld) -> None:
    f.mission(world)
    f.providers(world, f.lab("6.3"), f.lab("6.3"))
    assert f.upload(world) == "accepted"
    incidents = world.rows("incident")
    assert incidents
    assert correct_value(world, changes={"value": "4.8"}).status == "accepted"
    assert world.rows("incident") == incidents
    assert any(r.body.get("review_kind") == "incident_response" for r in world.rows("review"))


def test_correction_screens_new_danger_before_patient_lease(world: PatientWorld) -> None:
    upload(world)
    lease = world.store.acquire_patient(
        world.patient_scope, "busy-other-worker", world.clock(), timedelta(minutes=3)
    )
    assert lease
    result = correct_value(world, changes={"value": "6.3"})
    assert result.status == "stale_version"
    assert world.rows("incident")
    assert f.current(world).extracted_values[0].value == "5.0"
    world.store.release_patient(lease)


@pytest.mark.parametrize("state", ["queued", "sending", "uncertain", "provider_accepted"])
def test_instruction_exposure_ladder_is_honest(world: PatientWorld, state: str) -> None:
    from sanad.corrections import Correction
    from sanad.steward.apply import make_intent
    from sanad.steward.corrections import notice_text
    from sanad.store.records import DoctorAuthority, OutboundIntent
    from store.medication_fixtures import confirm

    confirm(world, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
    old = world.rows("care_order_version")[0]
    mission = from_record(world.rows("mission")[0], Mission)
    profile = world.store.get_patient_profile(world.patient_scope)
    authority_row = world.store.get(world.patient_scope, "doctor_authority", world.doctor.id)
    assert profile and authority_row
    intent = make_intent(
        world.patient_scope,
        "synthetic-instruction",
        (to_record(mission, world.patient_scope).ref,),
        "routine_prompt",
        "synthetic",
        world.clock(),
        world.runtime.steward.policy_provider(world.patient_scope),
        from_record(authority_row, DoctorAuthority),
        profile,
        audience="patient",
        slot="synthetic:slot",
        order_refs=mission.order_refs,
    )
    intent = OutboundIntent.model_validate(
        intent.model_dump()
        | {
            "status": state,
            "accepted_at": world.clock() if state == "provider_accepted" else None,
            "accepted_message_id": "synthetic-accepted" if state == "provider_accepted" else None,
        }
    )
    world.seed(intent)
    unchanged = world.store.get(world.patient_scope, "outbound_intent", intent.id)
    outcome = act(
        world,
        {
            "type": "AmendOrder",
            "predecessor": old.ref.model_dump(),
            "changes": {"dose": "40 mg"},
            "reason": "Doctor changed prescribed dose",
        },
    )
    assert outcome.status == "accepted", outcome
    saved = world.store.get(world.patient_scope, "outbound_intent", intent.id)
    assert saved
    correction = from_record(world.rows("correction")[0], Correction)
    text = notice_text(correction)
    if state == "queued":
        assert (
            saved.body["status"] == "suppressed"
            and saved.body["suppression_reason"] == "accepted_record_corrected"
        )
        assert not correction.exposures
    else:
        assert saved == unchanged
        assert correction.exposures[intent.id] == state
        assert (
            "cannot be unsent" if state == "provider_accepted" else "Delivery remains uncertain"
        ) in text
    assert "withdrawn" not in text.lower()
    assert world.store.get(world.patient_scope, "care_order_version", old.id) == old


@pytest.mark.parametrize(
    "field,value",
    [
        ("dose", "40 mg"),
        ("frequency", "twice daily"),
        ("duration", "7 days"),
        ("timing", "at night"),
    ],
)
def test_each_order_field_uses_versioned_supersession(
    world: PatientWorld, field: str, value: str
) -> None:
    from store.medication_fixtures import confirm

    confirm(world, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
    old = world.rows("care_order_version")[0]
    plan = world.rows("care_plan")[0]
    assert (
        act(
            world,
            {
                "type": "AmendOrder",
                "predecessor": old.ref.model_dump(),
                "changes": {field: value},
                "reason": "Reviewed instruction",
            },
        ).status
        == "accepted"
    )
    assert world.store.get(world.patient_scope, "care_order_version", old.id) == old
    assert world.store.get(world.patient_scope, "care_plan", plan.id) == plan
    assert any(r.body["state"] == "superseded" for r in world.rows("mission"))
    assert any(r.body["review_kind"] == "correction_disposition" for r in world.rows("review"))
    assert (
        act(
            world,
            {
                "type": "AmendOrder",
                "predecessor": old.ref.model_dump(),
                "changes": {field: value},
                "reason": "Stale instruction",
            },
        ).status
        == "invalid_input"
    )


def test_typo_without_meaning_change_has_no_correction_review(world: PatientWorld) -> None:
    from store.medication_fixtures import confirm

    confirm(
        world, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg", "timing": "at night"}
    )
    old = world.rows("care_order_version")[0]
    assert (
        act(
            world,
            {
                "type": "AmendOrder",
                "predecessor": old.ref.model_dump(),
                "changes": {"timing": "At Night"},
                "reason": "Capitalization only",
            },
        ).status
        == "accepted"
    )
    assert not any(r.body["review_kind"] == "correction_disposition" for r in world.rows("review"))


@pytest.mark.parametrize("stopped", [False, True])
def test_reopen_requires_preview_confirmation_with_actual_scribe_order(
    world: PatientWorld, stopped: bool
) -> None:
    from sanad.auth.service import revise
    from store.medication_fixtures import confirm, send

    confirm(world, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
    send(world, "I started Atorvastatin")
    mission = from_record(world.rows("mission")[0], Mission)
    assert mission.state == "fulfilled"
    profile = world.store.get_patient_profile(world.patient_scope)
    assert profile
    if stopped:
        world.seed(revise(profile, world.clock(), routine_contact_enabled=False))
    deadline = world.clock() + timedelta(days=4)
    outcome = act(
        world,
        {
            "type": "PreviewReopen",
            "mission_ref": to_record(mission, world.patient_scope).ref.model_dump(),
            "due_at": deadline.isoformat(),
            "reason": "Doctor requests a new report",
        },
    )
    assert outcome.status == "accepted", outcome
    offer = world.rows("correction_offer")[0]
    assert ("remain stopped" if stopped else "may receive reminders") in str(offer.body["preview"])
    assert world.store.get_mission(world.patient_scope, mission.id) == mission
    result = act(world, {"type": "ConfirmReopen", "offer_ref": offer.ref.model_dump()})
    assert result.status == "accepted", result
    reopened = world.store.get_mission(world.patient_scope, mission.id)
    assert reopened and reopened.state == "open" and reopened.due_at == deadline
    assert mission.fulfillment_event_id in reopened.prior_fulfillment_event_ids
    assert (
        act(world, {"type": "ConfirmReopen", "offer_ref": offer.ref.model_dump()}).status
        == "invalid_input"
    )


def test_superseded_real_medication_cannot_be_reopened(world: PatientWorld) -> None:
    from store.medication_fixtures import confirm, send

    confirm(world, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
    send(world, "I started Atorvastatin")
    old_mission = from_record(world.rows("mission")[0], Mission)
    confirm(world, {"action": "change", "drug": "Atorvastatin", "dose": "40 mg"})
    result = act(
        world,
        {
            "type": "PreviewReopen",
            "mission_ref": to_record(old_mission, world.patient_scope).ref.model_dump(),
            "due_at": (world.clock() + timedelta(days=4)).isoformat(),
            "reason": "Reopen old instruction",
        },
    )
    assert result.status == "invalid_input"


def fact_change(w: PatientWorld, row_id: str, text: str | None) -> CommandResult:
    row = w.store.get(w.patient_scope, "clinical_fact", row_id)
    assert row
    return act(
        w,
        {
            "type": "CorrectRecord",
            "predecessor": row.ref.model_dump(),
            "operation": "detach" if text is None else "replace",
            "changes": {} if text is None else {"text": text},
            "reason": "Doctor verified source",
        },
    )


def test_doctor_fact_correctable_and_detachable_with_immutable_history(world: PatientWorld) -> None:
    from sanad.steward.corrections import current_facts
    from store.medication_fixtures import confirm

    confirm(world, facts=({"category": "diagnosis", "text": "Original synthetic diagnosis"},))
    old = current_facts(world.store, world.patient_scope)[0]
    assert fact_change(world, old.id, "Corrected synthetic diagnosis").status == "accepted"
    current = current_facts(world.store, world.patient_scope)
    assert len(current) == 1 and "Corrected synthetic diagnosis" in str(current[0].body["payload"])
    assert world.store.get(world.patient_scope, "clinical_fact", old.id) == old
    assert fact_change(world, current[0].id, None).status == "accepted"
    assert not current_facts(world.store, world.patient_scope)
    assert len(world.rows("clinical_fact")) == 3

    from sanad.scribe.records import ClinicalFact, LabFactPayload

    original_fact = from_record(old, ClinicalFact)
    lab = original_fact.model_copy(
        update={
            "id": "doctor-lab",
            "category": "finding",
            "payload": LabFactPayload(
                analyte="Potassium", value="5.0", unit="mmol/L", text="Potassium 5.0 mmol/L"
            ),
        }
    )
    world.seed(lab)
    assert (
        act(
            world,
            {
                "type": "CorrectRecord",
                "predecessor": to_record(lab, world.patient_scope).ref.model_dump(),
                "operation": "replace",
                "reason": "Checked doctor supplied lab",
                "changes": {"value": "4.8"},
            },
        ).status
        == "accepted"
    )
    saved = from_record(current_facts(world.store, world.patient_scope)[0], ClinicalFact)
    assert isinstance(saved.payload, LabFactPayload)
    assert saved.payload.value == "4.8" and saved.payload.text == "Potassium 4.8 mmol/L"
    assert world.store.get(world.patient_scope, "clinical_fact", lab.id) == to_record(
        lab, world.patient_scope
    )

    # Accepted partial evidence has a head association but no fulfilment membership yet.
    f.mission(world, "partial", order_refs=())
    unreadable_unit = f.lab(items=[{"name": "Potassium", "value": "5.0", "unit": None}])
    f.providers(world, unreadable_unit, unreadable_unit)
    assert f.upload(world, id=1880) == "accepted"
    partial = f.current(world)
    assert partial.association_state == "accepted"
    assert correct_value(world, changes={"unit": "mmol/L"}).status == "accepted"


def test_monitor_correction_one_current_value_and_original_chronology(world: PatientWorld) -> None:
    from sanad.domain.entities import MonitorDetails
    from sanad.monitor.report import doctor_table
    from sanad.monitor.slots import coverage, filled
    from sanad.steward.corrections import current_facts
    from store.test_monitor import current, monitor

    monitor(world, count=1)
    world.send("BP 120/80", id=1700)
    old = current_facts(world.store, world.patient_scope)[0]
    original = current(world)
    assert fact_change(world, old.id, "BP 130/85").status == "accepted"
    updated = current(world)
    assert isinstance(updated.details, MonitorDetails)
    assert filled(updated.details)[0].value == "130/85"
    assert coverage(updated.details, world.clock()).satisfied
    assert "120/80" not in str(doctor_table(world.store, world.patient_scope, updated, "en"))
    assert updated.objective_received_at == original.objective_received_at
    value = current_facts(world.store, world.patient_scope)[0]
    assert fact_change(world, value.id, "BP 130/85 kg").status == "accepted"
    removed = current(world)
    assert isinstance(removed.details, MonitorDetails)
    assert not filled(removed.details) and not coverage(removed.details, world.clock()).satisfied
    assert removed.fulfillment_validity == "invalidated_pending_review"
    value = current_facts(world.store, world.patient_scope)[0]
    assert fact_change(world, value.id, "BP 130/85").status == "accepted"
    restored = current(world)
    assert isinstance(restored.details, MonitorDetails)
    assert filled(restored.details)[0].value == "130/85"
    assert isinstance(original.details, MonitorDetails)
    assert restored.details.readings[0].received_at == original.details.readings[0].received_at


def test_old_monitor_reading_does_not_become_newest(world: PatientWorld) -> None:
    from sanad.domain.entities import MonitorDetails
    from sanad.monitor.slots import filled
    from sanad.steward.corrections import current_facts
    from store.test_monitor import current, monitor

    monitor(world, count=2)
    world.send("BP 120/80", id=1700)
    old = current_facts(world.store, world.patient_scope)[0]
    world.clock.advance(timedelta(minutes=20))
    world.send("BP 130/85", id=1701)
    assert fact_change(world, old.id, "BP 125/80").status == "accepted"
    latest = current(world)
    assert isinstance(latest.details, MonitorDetails)
    assert filled(latest.details)[0].value == "130/85"


def test_start_correction_invalidates_only_its_day3_anchor(world: PatientWorld) -> None:
    from sanad.steward.corrections import current_facts
    from store.medication_fixtures import confirm, send

    confirm(world, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
    send(world, "I started Atorvastatin")
    task = world.rows("followup")[0]
    fact = next(
        r
        for r in current_facts(world.store, world.patient_scope)
        if r.body["category"] == "patient_report"
    )
    assert fact_change(world, fact.id, "I started Atorvastatin yesterday").status == "accepted"
    anchored = world.store.get(world.patient_scope, "followup", task.id)
    assert anchored and anchored.body["state"] == "contact_suppressed"
    assert anchored.body["anchor_time"] == task.body["anchor_time"]
    fact = next(
        r
        for r in current_facts(world.store, world.patient_scope)
        if r.body["category"] == "patient_report"
    )
    assert fact_change(world, fact.id, "I have not started Atorvastatin").status == "accepted"
    saved = world.store.get(world.patient_scope, "followup", task.id)
    assert saved and saved.body["state"] == "contact_suppressed"
    assert saved.body["anchor_time"] == task.body["anchor_time"]
    assert saved.body["due_at"] == task.body["due_at"]
    assert any(
        r.body.get("fulfillment_validity") == "invalidated_pending_review"
        for r in world.rows("mission")
    )


@pytest.mark.parametrize("outcome", ["accepted", "uncertain"])
def test_in_flight_amendment_retains_possible_exposure(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from pydantic import JsonValue

    from sanad.channels.transport import SendOutcome
    from sanad.contact.scheduler import schedule
    from store.contact_fixtures import dispatch
    from store.medication_fixtures import confirm

    world.clock.now = (world.clock() + timedelta(days=1)).replace(hour=7)
    confirm(world, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
    old = world.rows("care_order_version")[0]
    mission = world.rows("mission")[0]
    assert schedule(world.runtime.steward, mission).status == "accepted"
    prompt = next(i for i in world.patient_intents() if i.notification_purpose == "routine_prompt")
    calls = []

    def sending(recipient: str, payload: dict[str, JsonValue]) -> SendOutcome:
        calls.append(payload)
        sent = world.store.get(world.patient_scope, "outbound_intent", prompt.id)
        assert sent and sent.body["status"] == "sending"
        result = act(
            world,
            {
                "type": "AmendOrder",
                "predecessor": old.ref.model_dump(),
                "changes": {"dose": "40 mg"},
                "reason": "Changed during network send",
            },
        )
        assert result.status == "accepted", result
        return SendOutcome(
            status="accepted" if outcome == "accepted" else "uncertain",
            provider_message_id="synthetic-sent" if outcome == "accepted" else None,
        )

    monkeypatch.setattr(world.transport, "send", sending)
    sent = dispatch(world, prompt)
    assert sent.status == ("provider_accepted" if outcome == "accepted" else "uncertain")
    correction = world.rows("correction")[0]
    assert correction.body["exposures"] == {prompt.id: "sending"}
    assert len(calls) == 1
    assert any(
        r.body["review_kind"] == "correction_disposition" and r.body["review_at"]
        for r in world.rows("review")
    )
    assert dispatch(world, sent).status == sent.status
    assert len(calls) == 1


def test_prior_report_is_required_for_proactive_correction(world: PatientWorld) -> None:
    from sanad.store.records import OutboundIntent
    from store.contact_fixtures import dispatch

    upload(world)
    done = next(
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body["notification_purpose"] == "DONE:FULFILLMENT"
    )
    dispatched = dispatch(world, done)
    assert dispatched.status == "provider_accepted", dispatched.suppression_reason
    assert correct_value(world).status == "accepted"
    notice = next(
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body["notification_purpose"] == "DONE:CORRECTION"
    )
    delivered = dispatch(world, notice)
    assert delivered.status == "provider_accepted", delivered
    assert "Doctor checked original" in str(world.transport.calls[-1].payload)


def test_without_prior_report_correction_is_solicited(world: PatientWorld) -> None:
    upload(world)
    assert correct_value(world).status == "accepted"
    assert not any(
        r.body["notification_purpose"] == "DONE:CORRECTION" for r in world.rows("outbound_intent")
    )
    assert any(
        r.body["template_id"] == "accepted_correction"
        and r.body["notification_purpose"] == "solicited_reply"
        for r in world.rows("outbound_intent")
    )


def test_browser_authority_csrf_binding_and_correction_projection(world: PatientWorld) -> None:
    from store.login_fixtures import ORIGIN, browser_login

    upload(world)
    old = f.current(world)
    with world.client() as client:
        assert browser_login(client, world.login_path()).status_code == 303
        url = f"/api/patients/{world.patient_scope.patient_id}"
        record = client.get(url).json()
        body = {
            "command_id": "browser-test",
            "expected_binding_epoch": record["correction_authority"]["binding_epoch"],
            "expected_delivery_epoch": record["correction_authority"]["delivery_epoch"],
            "action": {
                "type": "CorrectRecord",
                "predecessor": to_record(old, old.scope).ref.model_dump(),
                "operation": "replace",
                "row_index": 0,
                "changes": {"value": "4.8"},
                "reason": "Doctor checked source",
            },
        }
        headers = {
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": client.cookies["sanad_csrf"],
        }
        response = client.post(url + "/corrections", json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "accepted", "offers": []}
        changed = client.get(url).json()
        assert len(changed["corrections"]) == 1
        assert changed["corrections"][0]["actor"]["session_id"]
        assert "4.8" in changed["corrections"][0]["notice"]
        assert client.post(url + "/corrections", json=body, headers=headers).status_code == 200
        assert (
            client.post("/api/patients/foreign/corrections", json=body, headers=headers).status_code
            == 404
        )
        assert client.post(url + "/corrections", json=body).status_code == 403
        assert browser_login(client, world.login_path(id=201)).status_code == 303
        headers["X-CSRF-Token"] = client.cookies["sanad_csrf"]
        body["command_id"] = "stale-browser"
        body["expected_binding_epoch"] = 999
        assert client.post(url + "/corrections", json=body, headers=headers).status_code == 409


def test_browser_reopen_returns_only_pending_offers(world: PatientWorld) -> None:
    from store.login_fixtures import ORIGIN, browser_login
    from store.medication_fixtures import confirm, send

    confirm(world, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
    send(world, "I started Atorvastatin")
    before = from_record(world.rows("mission")[0], Mission)
    assert before.state == "fulfilled"
    deadline = world.clock() + timedelta(days=4)
    with world.client() as client:
        assert browser_login(client, world.login_path()).status_code == 303
        url = f"/api/patients/{world.patient_scope.patient_id}"
        authority = client.get(url).json()["correction_authority"]
        headers = {
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": client.cookies["sanad_csrf"],
        }
        preview = {
            "command_id": "browser-reopen-preview",
            "expected_binding_epoch": authority["binding_epoch"],
            "expected_delivery_epoch": authority["delivery_epoch"],
            "action": {
                "type": "PreviewReopen",
                "mission_ref": to_record(before, world.patient_scope).ref.model_dump(),
                "due_at": deadline.isoformat(),
                "reason": "Doctor requests another report",
            },
        }
        response = client.post(url + "/corrections", json=preview, headers=headers)
        assert response.status_code == 200, response.text
        pending = response.json()
        assert pending["status"] == "accepted" and len(pending["offers"]) == 1
        offer = world.rows("correction_offer")[0]
        assert pending["offers"][0]["id"] == offer.id
        assert pending["offers"][0]["version"] == 1
        assert pending["offers"][0]["consumed_at"] is None
        assert "Atorvastatin" in pending["offers"][0]["preview"]
        assert world.store.get_mission(world.patient_scope, before.id) == before
        assert client.post(url + "/corrections", json=preview, headers=headers).json() == pending

        confirmation = {
            **preview,
            "command_id": "browser-reopen-confirm",
            "action": {"type": "ConfirmReopen", "offer_ref": offer.ref.model_dump()},
        }
        response = client.post(url + "/corrections", json=confirmation, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "accepted", "offers": []}
        consumed = world.store.get(world.patient_scope, "correction_offer", offer.id)
        assert consumed and consumed.version == 2 and consumed.body["consumed_at"]
        reopened = world.store.get_mission(world.patient_scope, before.id)
        assert reopened and reopened.state == "open" and reopened.due_at == deadline
        assert before.fulfillment_event_id in reopened.prior_fulfillment_event_ids
        projected = next(m for m in client.get(url).json()["missions"] if m["id"] == before.id)
        assert projected["state"] == "open" and projected["version"] == reopened.version

        # A lost response or replay of the earlier preview cannot revive the offer.
        for body in (confirmation, preview):
            replay = client.post(url + "/corrections", json=body, headers=headers)
            assert replay.status_code == 200, replay.text
            assert replay.json() == {"status": "accepted", "offers": []}
        assert world.store.get_mission(world.patient_scope, before.id) == reopened
        confirmation["command_id"] = "browser-reopen-second-click"
        refused = client.post(url + "/corrections", json=confirmation, headers=headers)
        assert refused.status_code == 409
        assert world.store.get_mission(world.patient_scope, before.id) == reopened


def test_nonmember_reference_and_bounded_fanout_refuse_atomically(world: PatientWorld) -> None:
    from sanad.auth.service import revise

    upload(world)
    old = f.current(world)
    mission = world.store.get_mission(world.patient_scope, old.mission_id or "")
    assert mission
    world.seed(revise(mission, world.clock(), evidence_refs=()))
    assert correct_value(world).status == "invalid_input"
    assert f.current(world) == old and not world.rows("correction")
    world.seed(
        revise(
            from_record(world.rows("mission")[0], Mission),
            world.clock(),
            evidence_refs=mission.evidence_refs,
        )
    )
    for i in range(25):
        world.seed(mission.model_copy(update={"id": f"fanout-{i}", "version": 1}))
    before = world.rows("mission")
    refused = correct_value(world)
    assert refused.status == "invalid_input"
    assert f.current(world) == old and not world.rows("correction")
    assert world.rows("mission") == before
    # The ordinary fan-out bound cannot suppress newly observed danger.
    assert correct_value(world, changes={"value": "6.3"}).status == "invalid_input"
    assert world.rows("incident")
    assert f.current(world) == old and not world.rows("correction")


def test_old_review_cannot_validate_successor_correction(world: PatientWorld) -> None:
    upload(world)
    assert correct_value(world, changes={"unit": None}).status == "accepted"
    first = world.rows("correction")[0]
    old_review = next(r for r in world.rows("review") if r.body.get("source_id") == first.id)
    assert correct_value(world, changes={"unit": "mmol/L"}).status == "accepted"
    mission = world.rows("mission")[0]
    result = act(
        world,
        {
            "type": "ValidateCorrection",
            "correction_id": first.id,
            "mission_ref": mission.ref.model_dump(),
            "review_ref": old_review.ref.model_dump(),
            "reason": "Old offer",
        },
    )
    assert result.status == "invalid_input"
    assert world.store.get(world.patient_scope, "mission", mission.id) == mission
    assert world.store.get(world.patient_scope, "review", old_review.id) == old_review


def test_queued_done_rechecks_correction_before_transport(world: PatientWorld) -> None:
    from sanad.store.records import OutboundIntent
    from store.contact_fixtures import dispatch

    upload(world)
    done = next(
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body["notification_purpose"] == "DONE:FULFILLMENT"
    )
    assert correct_value(world, detach=True).status == "accepted"
    count = len(world.transport.calls)
    assert dispatch(world, done).status == "suppressed"
    assert len(world.transport.calls) == count


def test_doctor_command_and_notice_entry_are_solicited(world: PatientWorld) -> None:
    from sanad.evidence.correction_doctor import callback as correction_callback
    from sanad.store.records import OutboundIntent
    from store.account_fixtures import APPLICANT, callback, update

    upload(world)
    done = next(
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body["notification_purpose"] == "DONE:FULFILLMENT"
    )
    assert world.post(callback(correction_callback(done), APPLICANT, 1810)).status_code == 200
    old = f.current(world)
    text = (
        f"/correct {world.patient_scope.patient_id} evidence {old.id} {old.version} "
        '0.value "4.8" "Doctor checked source"'
    )
    assert world.post(update(APPLICANT, text, 1811)).status_code == 200
    assert f.current(world).extracted_values[0].value == "4.8"
    assert world.post(update(APPLICANT, text, 1811)).status_code == 200
    assert len(world.rows("correction")) == 1
