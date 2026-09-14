"""Strict request bodies and stable, body-bound retry identities."""

import json
from hashlib import sha256

import pytest
from pydantic import ValidationError

from sanad.domain import Principal
from sanad.presentation import doctor_actions
from sanad.steward.types import command_result
from sanad.store.records import Accepted, Duplicate
from sanad.web.api_actions import ActionBody, command_id, response


@pytest.mark.parametrize(
    "change",
    [
        {"expected_version": True},
        {"expected_version": 0},
        {"reason": " "},
        {"reason": "a" * 201},
        {"due_at": "2026-09-15T12:00:00"},
        {"action": "cancel"},
        {"extra": "injected"},
        {"text": "extra"},
        {"listing_token": "extra"},
    ],
)
def test_record_action_body_is_strict(change: dict[str, object]) -> None:
    data = {
        "item_kind": "mission",
        "item_id": "request",
        "action": "extend",
        "expected_version": 2,
        "command_id": "retry",
        "reason": "Agreed",
        "due_at": "2026-09-15T12:00:00Z",
    }
    with pytest.raises(ValidationError):
        ActionBody.model_validate(data | change)


def test_record_command_identity_covers_every_body_field() -> None:
    data = {
        "item_kind": "review",
        "item_id": "review",
        "action": "resolve",
        "expected_version": 2,
        "command_id": "retry",
        "reason": "Agreed",
        "disposition": "review",
        "listing_token": "listing",
        "expected_source_version": 1,
    }
    actor = Principal(subject="doctor", actor_kind="doctor", doctor_id="doctor")
    first = ActionBody.model_validate(data)
    canonical = json.dumps(
        first.model_dump(mode="json", exclude={"command_id"}, exclude_unset=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = sha256(canonical.encode()).hexdigest()
    expected = "web:" + sha256(("doctor|retry|" + digest).encode()).hexdigest()[:32]
    assert command_id(actor, first) == expected
    assert (
        command_id(actor, ActionBody.model_validate(dict(reversed(list(data.items()))))) == expected
    )
    for field, value in {
        "expected_version": 3,
        "reason": "Different",
        "disposition": "close",
        "item_id": "other",
        "listing_token": "other",
        "expected_source_version": 2,
    }.items():
        assert command_id(actor, ActionBody.model_validate(data | {field: value})) != expected
    assert command_id(actor.model_copy(update={"subject": "other"}), first) != expected


def test_record_legacy_outcome_is_saved_without_readback() -> None:
    result = command_result(Duplicate(original=Accepted(event_ids=(), resulting_versions=())))
    rendered = response(result, "en")
    assert json.loads(bytes(rendered.body))["detail"] == "Saved."
    assert (
        doctor_actions.outcome("suppressed", "patient_removed")
        == "Not sent: this patient was removed."
    )
