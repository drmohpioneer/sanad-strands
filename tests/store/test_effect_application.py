from datetime import timedelta

import pytest
from domain_fixtures import (
    CORRECTION,
    EVIDENCE,
    NOW,
    POLICY,
    followup,
    followup_payload,
    mission,
)

import sanad.domain.events as ev
from sanad.domain import (
    FollowUpState,
    MissionState,
    ObservationRef,
    ReviewKind,
    transition_mission,
)
from sanad.steward.apply import build_commit
from sanad.steward.types import records
from sanad.store import keys
from sanad.store.records import Accepted, CommandEnvelope
from store.fixtures import SCOPE
from store.processing_fixtures import World
from store.test_processing import world as world


@pytest.mark.parametrize(
    "effect_kind",
    [
        "audit",
        "intent",
        "create_review",
        "resolve_review",
        "apply_review",
        "create_followup",
        "anchor_followup",
        "suppress",
        "supersede",
        "associate",
        "retain",
    ],
)
def test_every_slice_one_effect_maps_to_atomic_records(world: World, effect_kind: str) -> None:
    original = mission(order_refs=())
    world.put(original)
    changed = mission(version=2, updated_at=NOW, order_refs=())
    command = world.command("RecordPatientReply", id="synthetic-effects")
    effect: ev.Effect
    if effect_kind in {"create_review", "resolve_review", "apply_review"}:
        kind = (
            ReviewKind.unmet_objective
            if effect_kind == "resolve_review"
            else ReviewKind.result_review
        )
        from domain_fixtures import review_payload

        from sanad.domain import create_review

        payload = review_payload(event_id=command.command_id, review_kind=kind)
        obligation = create_review(payload, NOW, POLICY).aggregate
        if effect_kind != "create_review":
            world.put(obligation)
        effect = (
            payload
            if effect_kind == "create_review"
            else ev.ResolveReviewEffect(
                review_kind=kind,
                source_type="mission",
                source_id=original.id,
                reason="Synthetic extension",
            )
            if effect_kind == "resolve_review"
            else ev.ApplyReviewEvent(
                obligation_id=obligation.id,
                event=ev.AcknowledgeReview(
                    event_id=command.command_id, actor_id="synthetic-doctor"
                ),
            )
        )
    elif effect_kind == "create_followup":
        effect = followup_payload(event_id=command.command_id)
    elif effect_kind == "anchor_followup":
        world.put(followup(FollowUpState.awaiting_anchor))
        effect = ev.AnchorFollowUp(
            parent_mission_id=original.id, anchor_time=NOW, anchor_kind="reported_effective_start"
        )
    elif effect_kind == "suppress":
        world.prompt()
        effect = ev.SuppressRoutineIntents(reason="Synthetic correction")
    elif effect_kind == "supersede":
        effect = ev.SupersedeEvidence(superseded_ref=EVIDENCE, correcting_ref=CORRECTION)
    elif effect_kind == "associate":
        effect = ev.RecordEvidenceAssociation(evidence_ref=EVIDENCE)
    elif effect_kind == "retain":
        effect = ev.RetainObservation(
            observation_ref=ObservationRef(observation_id="synthetic-observation")
        )
    elif effect_kind == "intent":
        effect = ev.EmitIntent(
            purpose="DANGER",
            source_event_id=command.command_id,
            source_version=2,
            facts_ref="synthetic-facts",
        )
    else:
        effect = ev.RecordAudit(
            event_type="PATIENT_REPLIED",
            event_id=command.command_id,
            before_version=1,
            after_version=2,
        )
    lease = world.store.acquire_patient(SCOPE, "synthetic-effects", NOW, timedelta(minutes=5))
    assert lease is not None
    command = CommandEnvelope.model_validate(command.model_dump() | {"fence": lease})
    effects = (
        (effect,)
        if effect_kind == "audit"
        else (
            effect,
            ev.RecordAudit(
                event_type="PATIENT_REPLIED",
                event_id=command.command_id,
                before_version=1,
                after_version=2,
            ),
        )
    )
    result = ev.TransitionResult(aggregate=changed, effects=effects)
    request = build_commit(SCOPE, command, result, NOW, world.policy, store=world.store)
    assert {r.ref for r in (*request.puts, *request.events, *request.intents)} == set(
        request.expected
    )
    assert world.mission().version == 1  # Building effects cannot write anything.
    assert isinstance(world.store.commit(request), Accepted)
    world.store.release_patient(lease)
    assert world.mission().version == 2
    assert world.store.get(SCOPE, "audit_event", command.command_id) is not None
    if effect_kind in {"create_review", "resolve_review", "apply_review"}:
        saved = list(records(world.store, SCOPE, "review"))[0]
        assert (
            saved.body["state"]
            == {
                "create_review": "open",
                "resolve_review": "resolved",
                "apply_review": "acknowledged",
            }[effect_kind]
        )
        assert (saved.review_pk is not None) == (effect_kind != "resolve_review")
    elif effect_kind in {"create_followup", "anchor_followup"}:
        saved = list(records(world.store, SCOPE, "followup"))[0]
        assert saved.body["state"] == "scheduled" and saved.due_lane_shard == "followup#0"
    elif effect_kind in {"supersede", "associate", "retain"}:
        saved = list(records(world.store, SCOPE, "evidence_annotation"))[0]
        assert saved.body["annotation"] == effect.model_dump(mode="json")
    elif effect_kind == "intent":
        intent = world.queued()[0]
        assert intent.logical_key == keys.digest(f"{command.command_id}|doctor|DANGER|")
        assert (
            world.store._read(keys.uniqueness(SCOPE, "OUTKEY", keys.digest(intent.logical_key)))
            is not None
        )
        assert world.store.get(SCOPE, "audit_event", intent.source_event_ids[0]) is not None
    elif effect_kind == "suppress":
        assert not world.queued()


def test_rejected_transition_persists_only_its_reviews_and_rejection_audit(world: World) -> None:
    original = mission(MissionState.blocked)
    world.put(original)
    command = world.command("RecordPatientReply", id="synthetic-rejected-resume")
    result = transition_mission(
        original,
        ev.ResumeContact(
            event_id=command.command_id,
            consent_active=False,
            doctor_active=False,
            order_active=False,
        ),
        NOW,
        POLICY,
    )
    assert isinstance(result, ev.TransitionRejected)
    lease = world.store.acquire_patient(SCOPE, "synthetic-rejection", NOW, timedelta(minutes=1))
    assert lease is not None
    command = CommandEnvelope.model_validate(command.model_dump() | {"fence": lease})
    request = build_commit(SCOPE, command, result, NOW, world.policy, store=world.store)
    assert not any(r.entity_type == "mission" for r in request.puts)
    assert isinstance(world.store.commit(request), Accepted)
    assert world.mission() == original
    assert len(list(records(world.store, SCOPE, "review"))) == 3
    assert any(r.body["event_type"] == "command_rejected" for r in request.events)
    replay = world.store.lookup_command(command)
    assert replay is not None and replay.status == "duplicate"
    assert replay.original.command_status == "needs_confirmation"
    world.store.release_patient(lease)


def test_no_effect_rejection_never_builds_a_domain_commit(world: World) -> None:
    from sanad.steward.apply import EffectsRejected

    original = mission()
    command = world.command("RecordPatientReply")
    result = transition_mission(
        original, ev.DeadlineReached(event_id=command.command_id), NOW, POLICY
    )
    assert isinstance(result, ev.TransitionRejected) and not result.effects
    with pytest.raises(EffectsRejected, match="deadline_not_reached"):
        build_commit(SCOPE, command, result, NOW, world.policy, store=world.store)


def test_anchor_effect_preserves_an_existing_doctor_anchor(world: World) -> None:
    original, task = mission(), followup()
    world.put(original, task)
    command = world.command("RecordPatientReply")
    effect = ev.AnchorFollowUp(
        parent_mission_id=original.id, anchor_time=NOW, anchor_kind="reported_effective_start"
    )
    result = ev.TransitionResult(aggregate=mission(version=2), effects=(effect,))
    request = build_commit(SCOPE, command, result, NOW, world.policy, store=world.store)
    assert not any(r.entity_type == "followup" for r in request.puts)
    assert world.store.get_followup(SCOPE, task.id) == task
