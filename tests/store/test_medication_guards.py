"""Positive and forged-command parity for each released patient store rule."""

from datetime import timedelta

import pytest
from harness import FakeClock

from sanad.auth.service import revise
from sanad.domain import Mission, transition_mission
from sanad.domain.events import BarrierResolved, TransitionResult
from sanad.steward.apply import CommitBuilder, make_intent
from sanad.store._base import StoreBase
from sanad.store.records import (
    CommitRequest,
    CommitResult,
    PatientProfile,
    StoredRecord,
    from_record,
    to_record,
)
from store.contact_fixtures import required
from store.medication_fixtures import confirm, send, snapshot, world


def replace(request: CommitRequest, row: StoredRecord) -> CommitRequest:
    return request.model_copy(
        update={
            "puts": tuple(
                row if (r.entity_type, r.id) == (row.entity_type, row.id) else r
                for r in request.puts
            )
        }
    )


def test_clarification_guard_accepts_only_field_projection(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(store, clock)
    confirm(w, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
    actual = store.commit
    rejected = []

    def commit(request: CommitRequest) -> CommitResult:
        if request.command.payload.get("type") == "RecordPatientReply":
            row = next(r for r in request.puts if r.entity_type == "patient_profile")
            profile = from_record(row, PatientProfile)
            assert profile.pending_start_clarification
            for field, value in (
                ("routine_contact_enabled", False),
                ("recipient_ref", "foreign"),
                ("consent_version", 99),
                ("delivery_epoch", 99),
                ("lease_generation", 99),
            ):
                forged = profile.model_copy(update={field: value})
                result = actual(replace(request, to_record(forged, w.patient_scope)))
                assert result.status == "forbidden", (field, result)
                rejected.append(field)
            pending = profile.pending_start_clarification.model_copy(
                update={"expires_at": clock() + timedelta(days=2)}
            )
            result = actual(
                replace(
                    request,
                    to_record(
                        profile.model_copy(update={"pending_start_clarification": pending}),
                        w.patient_scope,
                    ),
                )
            )
            assert result.status == "forbidden"
        return actual(request)

    monkeypatch.setattr(store, "commit", commit)
    send(w, "I started it a while ago")
    assert len(rejected) == 5 and w.profile.pending_start_clarification
    assert w.profile.routine_contact_enabled


def test_barrier_and_scoped_suppression_guards(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(store, clock)
    confirm(
        w,
        {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"},
        {"action": "start", "drug": "Forxiga", "dose": "10 mg"},
    )
    snap = snapshot(w)
    queued = []
    for mission in snap.missions:
        intent = make_intent(
            w.patient_scope,
            "synthetic-prompt:" + mission.id,
            (to_record(mission, w.patient_scope).ref,),
            "routine_prompt",
            "synthetic",
            clock(),
            w.runtime.steward.policy_provider(w.patient_scope),
            snap.authority,
            snap.profile,
            audience="patient",
            slot="synthetic:" + mission.id,
            order_refs=mission.order_refs,
        )
        w.seed(intent)
        queued.append(intent)
    actual = store.commit
    checked = []

    def commit(request: CommitRequest) -> CommitResult:
        if request.command.payload.get("type") == "RecordPatientReply":
            row = next(
                r
                for r in request.puts
                if r.entity_type == "mission" and r.body.get("state") == "blocked"
            )
            mission = from_record(row, Mission)
            for field, value in (
                ("resume_at", clock() + timedelta(days=2)),
                ("barrier_reason", "fabricated"),
                ("barrier_type", "other"),
                ("title", "different medicine"),
            ):
                forged = to_record(mission.model_copy(update={field: value}), w.patient_scope)
                assert actual(replace(request, forged)).status == "forbidden", field
                checked.append(field)
            without_scope = request.command.model_copy(
                update={
                    "payload": {
                        k: v
                        for k, v in request.command.payload.items()
                        if k != "medication_suppressions"
                    }
                }
            )
            assert (
                actual(request.model_copy(update={"command": without_scope})).status == "forbidden"
            )
            other = next(
                i for i in queued if not set(i.order_refs).intersection(mission.order_refs)
            )
            forged = to_record(
                revise(
                    other,
                    clock(),
                    status="suppressed",
                    suppression_reason=mission.barrier_reason,
                    work_clock=None,
                ),
                w.patient_scope,
            )
            assert (
                actual(
                    request.model_copy(
                        update={
                            "puts": (*request.puts, forged),
                            "expected": (*request.expected, forged.ref),
                        }
                    )
                ).status
                == "forbidden"
            )
        result = actual(request)
        if request.command.payload.get("type") == "RecordPatientReply":
            states = [
                required(store.get(w.patient_scope, "outbound_intent", i.id)).body["status"]
                for i in queued
            ]
            assert sorted(str(s) for s in states) == ["queued", "suppressed"]
        return result

    monkeypatch.setattr(store, "commit", commit)
    send(w, "I can't afford Atorvastatin")
    assert len(checked) == 4


def test_resolved_barrier_projection_accepts_only_exact_event(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(store, clock)
    confirm(w, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
    send(w, "I can't afford it")
    snap = snapshot(w)
    original = snap.missions[0]
    actual = store.commit
    checked = []

    def commit(request: CommitRequest) -> CommitResult:
        if request.command.payload.get("type") != "RecordPatientReply":
            return actual(request)
        builder = CommitBuilder(
            w.patient_scope,
            request.command,
            clock(),
            w.runtime.steward.policy_provider(w.patient_scope),
            store,
        )
        outcome = transition_mission(
            original,
            BarrierResolved(
                event_id=request.command.command_id + ":barrier-resolved:" + original.id
            ),
            clock(),
            builder.policy.timing,
        )
        assert isinstance(outcome, TransitionResult)
        builder.add(outcome)
        for row in request.puts:
            if row.entity_type == "clinical_fact":
                builder.put(row)
        for row in request.intents:
            if row.body["audience"] == "patient":
                builder.intents[row.id] = row
        resolution = builder.finish()
        changed = to_record(outcome.aggregate, w.patient_scope)
        forged = to_record(
            outcome.aggregate.model_copy(update={"title": "another drug"}), w.patient_scope
        )
        assert actual(replace(resolution, forged)).status == "forbidden"
        checked.append(True)
        result = actual(resolution)
        assert result.status == "accepted"
        assert store.get(w.patient_scope, "mission", original.id) == changed
        return result

    monkeypatch.setattr(store, "commit", commit)
    send(w, "I started Atorvastatin")
    assert checked == [True]
    assert snapshot(w).missions[0].state == "open"
