from datetime import timedelta

import pytest
from domain_fixtures import NOW, followup, mission

from sanad.domain import FollowUpState, MissionState
from sanad.steward.apply import make_intent
from sanad.steward.dispatch import freshness
from sanad.store.records import DoctorAuthority, OutboundIntent, PatientProfile, to_record
from store.fixtures import SCOPE
from store.processing_fixtures import World
from store.test_processing import world as world


@pytest.mark.parametrize(
    "kind,state,validity,purpose,allowed",
    [
        ("mission", "fulfilled", "valid", "DONE:FULFILLMENT", True),
        ("mission", "fulfilled", "invalidated_pending_review", "DONE:FULFILLMENT", False),
        ("mission", "open", "not_fulfilled", "DONE:FULFILLMENT", False),
        ("mission", "overdue", "not_fulfilled", "DEADLINE", True),
        ("mission", "open", "not_fulfilled", "DEADLINE", False),
        ("followup", "fulfilled", "valid", "DONE:FULFILLMENT", True),
        ("followup", "scheduled", "not_fulfilled", "DONE:FULFILLMENT", False),
        ("followup", "overdue", "not_fulfilled", "DEADLINE", True),
        ("followup", "scheduled", "not_fulfilled", "DEADLINE", False),
    ],
)
def test_purpose_requires_current_objective_state(
    world: World, kind: str, state: str, validity: str, purpose: str, allowed: bool
) -> None:
    source = (
        mission(MissionState(state), fulfillment_validity=validity)
        if kind == "mission"
        else followup(FollowUpState(state))
    )
    world.put(source)
    intent = make_intent(
        SCOPE,
        "synthetic-event",
        (to_record(source, SCOPE).ref,),
        purpose,
        "synthetic-facts",
        NOW,
        world.policy,
        world.doctor,
        world.profile,
    )
    assert (freshness(world.store, intent, NOW) is None) == allowed


def test_doctor_accountability_survives_patient_unbinding_and_stop(world: World) -> None:
    source = mission(MissionState.fulfilled)
    world.put(source)
    intent = make_intent(
        SCOPE,
        "synthetic-done",
        (to_record(source, SCOPE).ref,),
        "DONE:FULFILLMENT",
        "synthetic-facts",
        NOW,
        world.policy,
        world.doctor,
        world.profile,
    )
    world.put(
        PatientProfile.model_validate(
            world.profile.model_dump()
            | {
                "binding_active": False,
                "consent_active": False,
                "delivery_epoch": 99,
                "safety_epoch": 99,
            }
        )
    )
    assert freshness(world.store, intent, NOW) is None


def test_danger_still_requires_current_approved_recipient(world: World) -> None:
    intent = make_intent(
        SCOPE,
        "synthetic-danger",
        (),
        "DANGER",
        "synthetic-facts",
        NOW,
        world.policy,
        world.doctor,
        world.profile,
    )
    world.put(
        DoctorAuthority.model_validate(
            world.doctor.model_dump()
            | {
                "version": 2,
                "approved": False,
                "auth_epoch": 1,
            }
        )
    )
    assert freshness(world.store, intent, NOW) == "recipient_authority"


@pytest.mark.parametrize(
    "field",
    [
        "recipient_auth_epoch_seen",
        "doctor_auth_epoch_seen",
        "binding_epoch_seen",
        "consent_version_seen",
        "safety_epoch_seen",
        "delivery_epoch_seen",
    ],
)
def test_missing_variant_authority_fact_never_allows_routine(world: World, field: str) -> None:
    world.confirm()
    prompt = world.prompt()
    missing = OutboundIntent.model_validate(prompt.model_dump() | {field: None})
    assert freshness(world.store, missing, NOW) is not None


def test_expired_slot_and_unreleased_correction_purpose_fail_closed(world: World) -> None:
    world.confirm()
    prompt = world.prompt()
    assert freshness(world.store, prompt, NOW + timedelta(days=30)) == "expired"
    correction = make_intent(
        SCOPE,
        "synthetic-correction",
        (),
        "DONE:CORRECTION",
        "synthetic-facts",
        NOW,
        world.policy,
        world.doctor,
        world.profile,
    )
    assert freshness(world.store, correction, NOW) == "correction_notice_not_released"
