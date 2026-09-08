from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from harness import SimulatedCrash
from pydantic import ValidationError

from sanad.auth.commands import (
    ClaimInvitation,
    ConfirmPatientClaim,
    IssuedInvitation,
    IssueInvitation,
    RecordConsent,
    RejectClaim,
)
from sanad.auth.service import revise
from sanad.channels.telegram import wording
from sanad.store import keys
from sanad.store._base import Check, Write
from sanad.store.records import (
    CommitResult,
)
from store.account_fixtures import ADMIN, APPLICANT, PATIENT, callback, update
from store.login_fixtures import LoginWorld, replace_model
from store.test_login_claim import enrollment as enrollment

# invitation × claim × actor × command → result; pending consent is explicit.
CLAIM_STATES = [
    ("issued", "none", "claimant", "scan", "accepted", "claimed", "pending"),
    ("issued", "none", "doctor", "scan", "forbidden", "issued", "none"),
    ("issued", "none", "admin", "scan", "forbidden", "issued", "none"),
    ("claimed", "pending", "claimant", "scan", "forbidden", "claimed", "pending"),
    ("claimed", "pending", "other", "scan", "forbidden", "claimed", "pending"),
    ("claimed", "pending", "claimant", "accept", "accepted", "claimed", "consented"),
    ("claimed", "pending", "other", "accept", "forbidden", "claimed", "pending"),
    ("claimed", "pending", "claimant", "decline", "accepted", "claimed", "rejected"),
    ("claimed", "pending", "doctor", "confirm", "stale_version", "claimed", "pending"),
    ("claimed", "pending", "doctor", "reject", "accepted", "revoked", "rejected"),
    ("claimed", "consented", "claimant", "accept", "forbidden", "claimed", "consented"),
    ("claimed", "consented", "doctor", "confirm", "accepted", "consumed", "approved"),
    ("claimed", "consented", "other_doctor", "confirm", "forbidden", "claimed", "consented"),
    ("claimed", "consented", "claimant", "confirm", "forbidden", "claimed", "consented"),
    ("claimed", "consented", "doctor", "reject", "accepted", "revoked", "rejected"),
    ("claimed", "consented", "other_doctor", "reject", "forbidden", "claimed", "consented"),
    ("claimed", "rejected", "doctor", "confirm", "forbidden", "claimed", "rejected"),
    ("claimed", "rejected", "claimant", "accept", "forbidden", "claimed", "rejected"),
    ("claimed", "rejected", "other", "scan", "forbidden", "claimed", "rejected"),
    ("revoked", "rejected", "claimant", "scan", "forbidden", "revoked", "rejected"),
    ("revoked", "rejected", "doctor", "confirm", "forbidden", "revoked", "rejected"),
    ("expired", "expired", "claimant", "scan", "forbidden", "expired", "expired"),
    ("expired", "expired", "doctor", "confirm", "forbidden", "expired", "expired"),
    ("consumed", "approved", "doctor", "confirm", "forbidden", "consumed", "approved"),
    ("consumed", "approved", "other", "scan", "forbidden", "consumed", "approved"),
    ("claimed", "pending", "doctor", "reissue", "issued", "revoked", "rejected"),
    ("claimed", "consented", "doctor", "reissue", "issued", "revoked", "rejected"),
    ("claimed", "rejected", "doctor", "reissue", "issued", "revoked", "rejected"),
    ("issued", "none", "doctor", "reissue", "issued", "revoked", "none"),
    ("consumed", "approved", "doctor", "reissue", "forbidden", "consumed", "approved"),
]


@pytest.mark.parametrize(
    "inv_state,claim_state,actor_kind,command,expected,next_inv,next_claim", CLAIM_STATES
)
def test_claim_transition_table(
    enrollment: LoginWorld,
    inv_state: str,
    claim_state: str,
    actor_kind: str,
    command: str,
    expected: str,
    next_inv: str,
    next_claim: str,
) -> None:
    world = enrollment
    patient = world.stub()
    issued = world.invite(patient)
    hash = keys.digest(issued.token.get_secret_value())
    pending = world.claim(issued) if claim_state != "none" else None
    if pending and claim_state in {"consented", "approved"}:
        pending = world.consent(pending)
    elif pending and inv_state == "claimed" and claim_state == "rejected":
        pending = world.consent(pending, accept=False)
    if pending and inv_state == "revoked":
        assert (
            world.claims.reject(
                RejectClaim(command_id=uuid4().hex, actor=world.owner, claim_id=pending.id)
            ).status
            == "accepted"
        )
    elif pending and inv_state == "expired":
        world.clock.advance(timedelta(hours=24))
        assert world.claims.expire(hash).status == "accepted"
    elif pending and inv_state == "consumed":
        assert world.claims.confirm(world.confirm_command(pending)).status == "accepted"
    if actor_kind == "other_doctor":
        world.approve("40004")
    subject = {
        "doctor": APPLICANT,
        "admin": ADMIN,
        "claimant": PATIENT,
        "other": "50005",
        "other_doctor": "40004",
    }[actor_kind]
    actor, id = world.actor(subject), uuid4().hex
    result: IssuedInvitation | CommitResult
    if command == "scan":
        result = world.claims.claim_invitation(
            ClaimInvitation(
                command_id=id, actor=actor, private_chat_id=subject, invitation_hash=hash
            )
        )
    elif command == "reissue":
        result = world.claims.issue_invitation(
            IssueInvitation(command_id=id, actor=actor, patient_id=patient.id)
        )
    else:
        assert pending
        if command in {"accept", "decline"}:
            result = world.claims.record_consent(
                RecordConsent(
                    command_id=id, actor=actor, claim_id=pending.id, accept=command == "accept"
                )
            )
        elif command == "reject":
            result = world.claims.reject(
                RejectClaim(command_id=id, actor=actor, claim_id=pending.id)
            )
        else:
            result = world.claims.confirm(
                ConfirmPatientClaim(
                    command_id=id,
                    actor=actor,
                    claim_id=pending.id,
                    expected_versions=world.claims.confirmation_versions(actor, pending.id),
                )
            )
    assert result.status == expected
    inv = world.claims.invitation(hash)
    assert inv and inv.state == next_inv
    changed = world.claims.patient_claim(inv.pending_claim_id) if inv.pending_claim_id else None
    assert (
        "consented"
        if changed and changed.state == "pending" and changed.consent_id
        else changed.state
        if changed
        else "none"
    ) == next_claim
    profile = world.store.get_patient_profile(patient.scope)
    assert profile and profile.binding_active == (next_claim == "approved")


def test_copied_qr_first_claim_reject_reissue_intended_patient(enrollment: LoginWorld) -> None:
    world = enrollment
    patient = world.stub()
    first = world.invite(patient)
    wrong = world.claim(first, "50005")
    assert (
        world.claims.reject(
            RejectClaim(command_id=uuid4().hex, actor=world.owner, claim_id=wrong.id)
        ).status
        == "accepted"
    )
    intended = world.consent(world.claim(world.invite(patient), PATIENT, id=103), id=104)
    assert world.claims.confirm(world.confirm_command(intended)).status == "accepted"
    assert world.actor("50005").actor_kind == "unknown"
    assert world.actor(PATIENT).patient_id == patient.id
    assert len(world.store.list_records(patient.scope, "patient_binding")[0]) == 1


def test_bound_subject_cannot_claim_other_doctor_and_no_disclosure(enrollment: LoginWorld) -> None:
    world = enrollment
    bound = world.bound()
    other = world.approve("40004")
    from sanad.auth.commands import CreatePatientStub, IssuedInvitation

    stub = world.claims.create_stub(
        CreatePatientStub(
            command_id=uuid4().hex, actor=world.actor("40004"), display_name="Other Private Name"
        )
    )
    assert stub.status == "accepted"
    id = next(r.id for r in stub.resulting_versions if r.entity_type == "patient")
    invitation = world.claims.issue_invitation(
        IssueInvitation(command_id=uuid4().hex, actor=world.actor("40004"), patient_id=id)
    )
    assert isinstance(invitation, IssuedInvitation)
    assert (
        world.post(
            update(PATIENT, "/start " + invitation.token.get_secret_value(), 103)
        ).status_code
        == 200
    )
    refusal = next(i for i in world.intents() if i.template_id == "claim_refused")
    assert world.dispatch(refusal).status == "provider_accepted"
    assert refusal.payload == {"text": wording.render("claim_refused", "en")}
    assert other.id not in str(refusal.payload) and bound.id not in str(refusal.payload)
    assert world.actor(PATIENT).patient_id == bound.id


@pytest.mark.parametrize("action", ["accept", "confirm"])
def test_wrong_actor_callback_and_one_time_action(enrollment: LoginWorld, action: str) -> None:
    world = enrollment
    pending = world.claim(world.invite(world.stub()))
    if action == "confirm":
        pending = world.consent(pending)
    template = "consent_request" if action == "accept" else "claim_awaiting_doctor"
    intended = PATIENT if action == "accept" else APPLICANT
    token = world.action_token(pending.id, template)
    assert world.post(callback(token, "50005", 103)).status_code == 200
    assert world.transport.callback_calls[-1].text == wording.render("claim_refused", "en")
    assert world.post(callback(token, intended, 104)).status_code == 200
    assert world.transport.callback_calls[-1].text == ""
    assert world.post(callback(token, intended, 105)).status_code == 200
    assert world.transport.callback_calls[-1].text == wording.render("claim_refused", "en")


def test_role_escalation_payload_rejected(enrollment: LoginWorld) -> None:
    world = enrollment
    issued = world.invite(world.stub())
    with pytest.raises(ValidationError):
        ClaimInvitation.model_validate(
            dict(
                command_id="forged",
                actor=world.actor(PATIENT),
                private_chat_id=PATIENT,
                invitation_hash=keys.digest(issued.token.get_secret_value()),
                role_set=["doctor", "admin"],
            )
        )
    body = update(PATIENT, "/start " + issued.token.get_secret_value(), 100)
    message = body["message"]
    assert isinstance(message, dict)
    message["role_set"] = ["doctor", "admin"]
    assert world.post(body).status_code == 200
    assert not world.actor(PATIENT).verified_roles


def test_stale_patient_version_refuses_confirmation(enrollment: LoginWorld) -> None:
    world = enrollment
    patient = world.stub()
    pending = world.consent(world.claim(world.invite(patient)))
    command = world.confirm_command(pending)
    current = world.claims.patient(world.doctor.id, patient.id)
    assert current
    replace_model(world, revise(current, world.clock(), display_name="Corrected Synthetic Patient"))
    assert world.claims.confirm(command).status == "stale_version"
    assert not world.store.list_records(patient.scope, "patient_binding")[0]


def test_two_scanners_preserve_one_claim(enrollment: LoginWorld) -> None:
    world = enrollment
    issued = world.invite(world.stub())
    hash = keys.digest(issued.token.get_secret_value())

    def scan(subject: str) -> str:
        return world.claims.claim_invitation(
            ClaimInvitation(
                command_id=uuid4().hex,
                actor=world.actor(subject),
                private_chat_id=subject,
                invitation_hash=hash,
            )
        ).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(scan, (PATIENT, "50005")))
    assert results.count("accepted") == 1
    inv = world.claims.invitation(hash)
    assert inv and inv.pending_claim_id
    claims, _ = world.store.list_records(world.claims.scope, "patient_claim")
    assert len(claims) == 1 and claims[0].id == inv.pending_claim_id


def test_concurrent_confirmations_one_binding(enrollment: LoginWorld) -> None:
    world = enrollment
    patient = world.stub()
    pending = world.consent(world.claim(world.invite(patient)))
    commands = [world.confirm_command(pending), world.confirm_command(pending)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(world.claims.confirm, commands))
    assert [r.status for r in results].count("accepted") == 1
    assert len(world.store.list_records(patient.scope, "patient_binding")[0]) == 1
    assert len(world.store.list_records(patient.scope, "outbound_intent")[0]) == 1


@pytest.mark.parametrize("moment", ["before", "after"])
def test_confirmation_transaction_crash_all_or_nothing(
    enrollment: LoginWorld, monkeypatch: pytest.MonkeyPatch, moment: str
) -> None:
    world = enrollment
    patient = world.stub()
    pending = world.consent(world.claim(world.invite(patient)))
    command = world.confirm_command(pending)
    original = world.store._atomic

    def crash(writes: list[Write], checks: list[Check]) -> bool:
        confirmation = any(w.item.get("entity_type") == "patient_binding" for w in writes)
        if confirmation and moment == "before":
            raise SimulatedCrash("before confirmation")
        result = original(writes, checks)
        if confirmation and result:
            raise SimulatedCrash("after confirmation")
        return result

    monkeypatch.setattr(world.store, "_atomic", crash)
    with pytest.raises(SimulatedCrash):
        world.claims.confirm(command)
    monkeypatch.setattr(world.store, "_atomic", original)
    accepted = moment == "after"
    assert bool(world.store.list_records(patient.scope, "patient_binding")[0]) == accepted
    assert bool(world.store.list_records(patient.scope, "outbound_intent")[0]) == accepted
    assert (world.actor(PATIENT).actor_kind == "patient") == accepted
    inv = world.claims.invitation(pending.invitation_id)
    claim = world.claims.patient_claim(pending.id)
    assert inv and claim
    assert (inv.state == "consumed") == accepted and (claim.state == "approved") == accepted
    if not accepted:
        assert world.claims.confirm(command).status == "accepted"


def test_two_patient_records_race_for_global_subject(enrollment: LoginWorld) -> None:
    world = enrollment
    first = world.stub()
    second = world.stub()
    one = world.consent(world.claim(world.invite(first)))
    two = world.consent(world.claim(world.invite(second), id=103), id=104)
    commands = [world.confirm_command(one), world.confirm_command(two)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(world.claims.confirm, commands))
    assert sorted(r.status for r in results) == ["accepted", "forbidden"]
    assert (
        sum(len(world.store.list_records(p.scope, "patient_binding")[0]) for p in (first, second))
        == 1
    )
    loser = two if results[0].status == "accepted" else one
    assert world.claims.patient_claim(loser.id).state == "pending"  # type: ignore[union-attr]
    assert world.claims.invitation(loser.invitation_id).state == "claimed"  # type: ignore[union-attr]
