from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from domain_fixtures import mission
from harness import SimulatedCrash, crash_after
from pydantic import BaseModel

from sanad.auth.commands import (
    ClaimInvitation,
    IssueDoctorLogin,
    IssuePatientLogin,
    RevokeBinding,
)
from sanad.auth.service import revise
from sanad.domain import MissionState, PatientScope
from sanad.steward.sweep import SweepBudget, Sweeper
from sanad.store import keys
from sanad.store._base import Write
from sanad.store.records import (
    CommitRequest,
    Consent,
    LoginExchange,
    OutboundIntent,
    PatientBinding,
    WorkerCapability,
    from_record,
    record_item,
    to_record,
)
from store.account_fixtures import PATIENT, update
from store.login_fixtures import LoginWorld, browser_login
from store.test_login_claim import enrollment as enrollment


def intents_for(world: LoginWorld, scope: PatientScope) -> list[OutboundIntent]:
    return [
        from_record(r, OutboundIntent)
        for r in world.store.list_records(scope, "outbound_intent")[0]
    ]


@pytest.mark.parametrize("claimed,consented", [(False, False), (True, False), (True, True)])
def test_expiry_sweep_once_without_touching_mission_deadlines(
    enrollment: LoginWorld, claimed: bool, consented: bool
) -> None:
    world = enrollment
    patient = world.stub()
    value = mission(
        MissionState.awaiting_link, doctor_id=patient.scope.doctor_id, patient_id=patient.id
    )
    world.put_patient(world.doctor, value)
    issued = world.invite(patient)
    pending = world.claim(issued) if claimed else None
    if consented:
        assert pending
        pending = world.consent(pending)
    world.clock.advance(timedelta(hours=24))
    cap = WorkerCapability(
        service_subject="synthetic-claim-sweep",
        permitted_lanes=frozenset({"claim"}),
        resolved_scope=world.claims.scope,
        auth_expiry=world.clock() + timedelta(hours=1),
        invocation_id="synthetic-tick",
    )
    sweep = Sweeper(
        world.runtime.steward,
        world.runtime.inbound,
        world.runtime.dispatcher,
        cap,
        lane_handlers={"claim": world.app.state.claim_lane},
    )
    first = sweep.sweep("claim", "0", world.clock(), SweepBudget())
    second = sweep.sweep("claim", "0", world.clock(), SweepBudget())
    assert first.handled >= 1 and not first.errors and second.handled == 0
    assert world.store.get_mission(patient.scope, value.id) == value
    expired = [i for i in world.intents() if i.template_id == "invitation_expired_doctor"]
    assert len(expired) == 1
    assert world.dispatch(expired[0]).status == "provider_accepted"
    inv = world.claims.invitation(keys.digest(issued.token.get_secret_value()))
    assert inv and inv.state == "expired" and inv.work_clock is None
    if pending:
        saved = world.claims.patient_claim(pending.id)
        assert saved and saved.state == "expired" and saved.work_clock is None


def test_revoke_suppresses_routine_danger_still_sent_and_fresh_subject_relinks(
    enrollment: LoginWorld,
) -> None:
    world = enrollment
    patient = world.bound()
    confirmed = next(
        i for i in intents_for(world, patient.scope) if i.template_id == "binding_confirmed"
    )
    assert world.post(update(PATIENT, "صدري واجعني", id=103)).status_code == 200
    danger = next(
        i for i in intents_for(world, patient.scope) if i.notification_purpose == "DANGER"
    )
    # The existing ordinary processor has touched the patient lease; revocation preserves its fence.
    before = world.store.get_patient_profile(patient.scope)
    assert before
    assert (
        world.claims.revoke_binding(
            RevokeBinding(
                command_id=uuid4().hex,
                actor=world.owner,
                patient_id=patient.id,
                reason_code="wrong_identity",
            )
        ).status
        == "accepted"
    )
    after = world.store.get_patient_profile(patient.scope)
    assert after and not after.binding_active and not after.consent_active
    assert after.delivery_epoch == before.delivery_epoch + 1
    assert after.binding_epoch == before.binding_epoch + 1
    assert world.dispatch(confirmed).status == "suppressed"
    assert world.dispatch(danger).status == "provider_accepted"
    fresh = world.invite(patient)
    old_subject = world.claims.claim_invitation(
        ClaimInvitation(
            command_id=uuid4().hex,
            actor=world.actor(PATIENT),
            private_chat_id=PATIENT,
            invitation_hash=keys.digest(fresh.token.get_secret_value()),
        )
    )
    assert old_subject.status == "forbidden"
    pending = world.consent(world.claim(fresh, "50005", id=104), id=105)
    assert world.claims.confirm(world.confirm_command(pending)).status == "accepted"
    assert world.actor(PATIENT).actor_kind == "unknown"
    assert world.actor("50005").patient_id == patient.id


@pytest.mark.parametrize("change", ["withdrawn", "consent_version", "binding_epoch", "revoked"])
def test_patient_exchange_requires_current_binding_and_consent(
    enrollment: LoginWorld, change: str
) -> None:
    world = enrollment
    patient = world.bound()
    path = world.login_path(PATIENT)
    assert patient.active_binding_id and patient.consent_id
    model: BaseModel
    if change == "revoked":
        assert (
            world.claims.revoke_binding(
                RevokeBinding(
                    command_id=uuid4().hex,
                    actor=world.owner,
                    patient_id=patient.id,
                    reason_code="revoked",
                )
            ).status
            == "accepted"
        )
    else:
        if change == "binding_epoch":
            binding = world.claims.load(
                patient.scope, "patient_binding", patient.active_binding_id, PatientBinding
            )
            assert binding
            model = revise(binding, world.clock(), binding_epoch=binding.binding_epoch + 1)
        else:
            consent = world.claims.load(patient.scope, "consent", patient.consent_id, Consent)
            assert consent
            model = revise(
                consent,
                world.clock(),
                **({"withdrawn_at": world.clock()} if change == "withdrawn" else {}),
            )
        row = to_record(model, patient.scope)
        assert world.store._atomic([Write(record_item(row), row.version - 1)], [])
    with world.client() as client:
        assert browser_login(client, path).status_code == 403
    assert (
        world.login.issue(
            IssuePatientLogin(command_id=uuid4().hex, actor=world.actor(PATIENT))
        ).status
        == "forbidden"
    )


def test_concurrent_login_reissue_serializes_one_current_token(enrollment: LoginWorld) -> None:
    world = enrollment
    old_path = world.login_path()
    commands = [IssueDoctorLogin(command_id=uuid4().hex, actor=world.owner) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(world.login.issue, commands))
    assert any(r.status == "accepted" for r in results)
    paths = [
        str(i.payload["text"]).splitlines()[-1]
        for i in world.intents()
        if i.template_id == "doctor_login_link" and i.payload
    ]
    states = [
        world.login.load(
            world.login.scope, "doctor_login", keys.digest(p.rsplit("/", 1)[-1]), LoginExchange
        )
        for p in paths
    ]
    assert sum(s.state == "issued" for s in states if s) == 1
    with world.client() as client:
        assert browser_login(client, old_path).status_code == 403


def test_expiry_at_store_clock_after_service_read_is_atomic(
    enrollment: LoginWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = enrollment
    raw = world.login_path().rsplit("/", 1)[-1]
    pre = world.login.pre_session()
    assert pre
    original = world.store.commit_account

    def delayed(request: CommitRequest):  # type: ignore[no-untyped-def]
        if request.command.payload.get("type") == "ExchangeLogin":
            world.clock.advance(timedelta(minutes=10))
        return original(request)

    monkeypatch.setattr(world.store, "commit_account", delayed)
    assert (
        world.login.exchange(
            "doctor", raw, pre.cookie.get_secret_value(), pre.csrf.get_secret_value()
        ).status
        == "forbidden"
    )
    exchange = world.login.load(world.login.scope, "doctor_login", keys.digest(raw), LoginExchange)
    assert exchange and exchange.state == "issued"


def test_small_processing_latency_keeps_valid_confirmation(
    enrollment: LoginWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = enrollment
    pending = world.consent(world.claim(world.invite(world.stub())))
    command = world.confirm_command(pending)
    original = world.store.commit_account

    def delayed(request: CommitRequest):  # type: ignore[no-untyped-def]
        world.clock.advance(timedelta(milliseconds=2))
        return original(request)

    monkeypatch.setattr(world.store, "commit_account", delayed)
    assert world.claims.confirm(command).status == "accepted"


def test_receipt_stores_hash_and_crash_replay_preserves_first_claim(enrollment: LoginWorld) -> None:
    world = enrollment
    issued = world.invite(world.stub())
    raw = issued.token.get_secret_value()
    with crash_after(world.store, "commit_account"), pytest.raises(SimulatedCrash):
        world.post(update(PATIENT, "/start " + raw, 100))
    receipt = world.receipt(100)
    assert receipt.state == "completed"
    assert raw not in receipt.model_dump_json() and raw not in repr(receipt)
    assert receipt.payload and receipt.payload["invitation_hash"] == keys.digest(raw)
    assert world.post(update(PATIENT, "/start " + raw, 100)).status_code == 200
    assert len(world.store.list_records(world.claims.scope, "patient_claim")[0]) == 1


def test_missing_consent_policy_refuses_claim_without_orphan_rows(enrollment: LoginWorld) -> None:
    world = enrollment
    issued = world.invite(world.stub())
    world.claims.consent_policy = lambda doctor_id: None
    assert (
        world.post(update(PATIENT, "/start " + issued.token.get_secret_value(), 100)).status_code
        == 200
    )
    assert not world.store.list_records(world.claims.scope, "patient_claim")[0]
    assert next(i for i in world.intents() if i.template_id == "claim_refused")


def test_confirm_exact_invitation_expiry_and_stale_doctor_authority(enrollment: LoginWorld) -> None:
    world = enrollment
    pending = world.consent(world.claim(world.invite(world.stub())))
    command = world.confirm_command(pending)
    world.clock.advance(timedelta(hours=24))
    assert world.claims.confirm(command).status == "forbidden"
    assert world.claims.patient_claim(pending.id).state == "pending"  # type: ignore[union-attr]
