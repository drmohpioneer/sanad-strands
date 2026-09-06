from datetime import timedelta

import pytest
from harness import FakeClock
from pydantic import ValidationError

from sanad.accounts.commands import (
    AlreadyInState,
    ApplyAsDoctor,
    ApproveDoctor,
    ReinstateDoctor,
    RejectDoctor,
    SuspendDoctor,
)
from sanad.accounts.records import Authorization, Doctor, SubjectBinding
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.keys import AccountScope
from sanad.store.records import DoctorAuthority, OperationalIssue, from_record, to_record
from store.account_fixtures import ADMIN, APPLICANT, BOT, AccountWorld


@pytest.fixture
def accounts(store: StoreBase, clock: FakeClock) -> AccountWorld:
    return AccountWorld.create(store, clock)


def test_application_approval_roles_and_atomic_rows(accounts: AccountWorld) -> None:
    assert accounts.actor(APPLICANT).actor_kind == "unknown"
    app = accounts.apply()
    assert app.status == "pending" and app.work_clock
    assert app.work_clock.next_action_at == accounts.clock() + timedelta(hours=24)
    assert accounts.actor(APPLICANT).actor_kind == "unknown"
    assert sorted(str(i.template_id) for i in accounts.intents()) == [
        "admin_new_application",
        "application_received",
    ]
    doctor = accounts.approve()
    assert doctor.id != APPLICANT and doctor.auth_epoch == 1
    auth = accounts.store.authorize(BOT, APPLICANT)
    assert auth.principal.verified_roles == frozenset({"doctor"})
    assert auth.principal.doctor_id == doctor.id and auth.auth_epoch == 1
    assert Authorization.model_validate_json(auth.model_dump_json()) == auth
    row = accounts.store.get(doctor.scope, "doctor_authority", doctor.id)
    assert row and from_record(row, DoctorAuthority).approved
    assert not accounts.store.get(accounts.runtime.accounts.scope, "doctor_authority", doctor.id)
    assert auth.binding and auth.binding.role_set == frozenset({"doctor"})
    decided = accounts.runtime.accounts.application(app.id)
    assert decided and decided.doctor_id == doctor.id
    for model in (doctor, auth.binding, app):
        assert from_record(to_record(model, model.scope), type(model)) == model


def test_admin_is_settings_only_and_may_also_be_doctor(accounts: AccountWorld) -> None:
    assert accounts.actor().verified_roles == frozenset({"admin"})
    doctor = accounts.approve(ADMIN)
    assert accounts.actor().verified_roles == frozenset({"admin", "doctor"})
    assert accounts.actor().actor_kind == "doctor"
    binding = accounts.store.authorize(BOT, ADMIN).binding
    assert binding and binding.role_set == frozenset({"doctor"})
    assert doctor.telegram_user_id == ADMIN


def test_safe_repeats_stale_reject_and_reapplication(accounts: AccountWorld) -> None:
    app = accounts.apply()
    service = accounts.runtime.accounts
    denied = service.approve(
        ApproveDoctor(
            command_id="forged",
            actor=accounts.actor(APPLICANT),
            application_id=app.id,
            expected_application_version=1,
        )
    )
    assert denied.status == "forbidden"
    stale = service.approve(
        ApproveDoctor(
            command_id="stale",
            actor=accounts.actor(),
            application_id=app.id,
            expected_application_version=2,
        )
    )
    assert stale.status == "stale_version"
    assert (
        service.reject(
            RejectDoctor(
                command_id="reject",
                actor=accounts.actor(),
                application_id=app.id,
                expected_application_version=1,
                reason_code="unverified",
            )
        ).status
        == "accepted"
    )
    assert (
        service.reject(
            RejectDoctor(
                command_id="repeat",
                actor=accounts.actor(),
                application_id=app.id,
                expected_application_version=1,
                reason_code="unverified",
            )
        ).status
        == "already_in_state"
    )
    assert (
        service.approve(
            ApproveDoctor(
                command_id="rejected",
                actor=accounts.actor(),
                application_id=app.id,
                expected_application_version=2,
            )
        ).status
        == "forbidden"
    )
    assert (
        service.apply(
            ApplyAsDoctor(
                command_id="text", actor=accounts.actor(APPLICANT), private_chat_id=APPLICANT
            )
        ).status
        == "already_in_state"
    )
    assert (
        service.apply(
            ApplyAsDoctor(
                command_id="restart",
                actor=accounts.actor(APPLICANT),
                private_chat_id=APPLICANT,
                restart_rejected=True,
            )
        ).status
        == "accepted"
    )
    current = service.application(app.id)
    assert current and current.id == app.id and current.version == 3 and current.status == "pending"
    assert current.created_at == app.created_at
    assert (
        service.approve(
            ApproveDoctor(
                command_id="approve",
                actor=accounts.actor(),
                application_id=app.id,
                expected_application_version=3,
            )
        ).status
        == "accepted"
    )
    repeated = service.approve(
        ApproveDoctor(
            command_id="approve-again",
            actor=accounts.actor(),
            application_id=app.id,
            expected_application_version=3,
        )
    )
    assert isinstance(repeated, AlreadyInState)
    assert AlreadyInState.model_validate_json(repeated.model_dump_json()) == repeated
    assert len([i for i in accounts.intents() if i.template_id == "doctor_approved"]) == 1


def test_ack_cap_does_not_stale_admin_buttons(accounts: AccountWorld) -> None:
    app = accounts.apply()
    raw = accounts.token()
    accounts.apply()
    assert len(accounts.intents()) == 2
    accounts.clock.advance(timedelta(hours=24))
    accounts.apply()
    assert len(accounts.intents()) == 3
    pending = accounts.runtime.accounts.application(app.id)
    assert pending and pending.version == 1
    assert (
        accounts.runtime.accounts.callback(raw, accounts.actor(), "late-review").status
        == "accepted"
    )


def test_suspension_reinstatement_epochs_coverage_and_no_replay(accounts: AccountWorld) -> None:
    doctor = accounts.approve()
    service = accounts.runtime.accounts
    assert (
        service.suspend(
            SuspendDoctor(
                command_id="suspend",
                actor=accounts.actor(),
                doctor_id=doctor.id,
                expected_doctor_version=1,
                reason_code="coverage",
            )
        ).status
        == "accepted"
    )
    auth = accounts.store.authorize(BOT, APPLICANT)
    assert auth.doctor_status == "suspended" and auth.auth_epoch == 2
    assert auth.principal.actor_kind == "unknown" and not auth.principal.verified_roles
    assert auth.binding and auth.binding.status == "frozen"
    issue = accounts.store.get(service.scope, "operational_issue", "coverage:" + doctor.id)
    assert issue and from_record(issue, OperationalIssue).due_at == accounts.clock()
    assert (
        service.suspend(
            SuspendDoctor(
                command_id="again",
                actor=accounts.actor(),
                doctor_id=doctor.id,
                expected_doctor_version=1,
                reason_code="coverage",
            )
        ).status
        == "already_in_state"
    )
    assert (
        service.reinstate(
            ReinstateDoctor(
                command_id="reinstate",
                actor=accounts.actor(),
                doctor_id=doctor.id,
                expected_doctor_version=2,
            )
        ).status
        == "accepted"
    )
    assert accounts.store.authorize(BOT, APPLICANT).auth_epoch == 3
    issue = accounts.store.get(service.scope, "operational_issue", "coverage:" + doctor.id)
    assert issue and from_record(issue, OperationalIssue).status == "resolved"
    assert len([i for i in accounts.intents() if i.template_id == "doctor_suspended_notice"]) == 1


def test_forged_roles_and_patient_exclusivity(accounts: AccountWorld) -> None:
    with pytest.raises(ValidationError):
        ApplyAsDoctor.model_validate(
            {
                "command_id": "spoof",
                "actor": accounts.actor(APPLICANT),
                "private_chat_id": APPLICANT,
                "role_set": {"admin"},
            }
        )
    with pytest.raises(ValidationError):
        SubjectBinding(
            id=keys.subject(BOT, APPLICANT).pk,
            scope=AccountScope(bot_id=BOT),
            telegram_user_id=APPLICANT,
            private_chat_id=APPLICANT,
            role_set=frozenset({"admin", "patient"}),
            doctor_id="d",
            patient_id="p",
            created_at=accounts.clock(),
            updated_at=accounts.clock(),
        )


def test_account_scope_serialization_due_work_and_partition_isolation(
    accounts: AccountWorld,
) -> None:
    from sanad.domain import TenantScope
    from sanad.store.keys import IntakeScope
    from sanad.store.records import WorkerCapability, scope_owns

    app = accounts.apply()
    scope = app.scope
    assert keys.partition(scope) == f"ACCT#{BOT}"
    other = AccountScope(bot_id="9999")
    tenant = TenantScope(doctor_id="synthetic-tenant")
    for foreign in (other, tenant, IntakeScope(doctor_id=tenant.doctor_id, intake_id="draft")):
        assert not scope_owns(scope, foreign) and not scope_owns(foreign, scope)
        assert accounts.store.get(foreign, "application", app.id) is None
        with pytest.raises(ValueError):
            to_record(app, foreign)
    cap = WorkerCapability(
        service_subject="synthetic-account-worker",
        resolved_scope=scope,
        permitted_lanes=frozenset({"account"}),
        auth_expiry=accounts.clock() + timedelta(days=2),
        invocation_id="due-application",
    )
    rows, _ = accounts.store.query_due(
        "account", "0", accounts.clock() + timedelta(hours=24), capability=cap
    )
    assert len(rows) == 1 and rows[0].id == app.id
    assert rows[0].record_key.scope == scope
    with pytest.raises(ValidationError):
        Doctor.model_validate(accounts.approve().model_dump() | {"timezone": "not-an-iana-zone"})


def test_actor_cannot_choose_a_different_private_recipient(accounts: AccountWorld) -> None:
    assert (
        accounts.runtime.accounts.apply(
            ApplyAsDoctor(
                command_id="other-chat",
                actor=accounts.actor(APPLICANT),
                private_chat_id=ADMIN,
            )
        ).status
        == "forbidden"
    )
    assert not accounts.intents()
