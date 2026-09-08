from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from harness import SimulatedCrash

from sanad.accounts.commands import (
    AccountCommand,
    ApproveDoctor,
    CallbackRefused,
    RejectDoctor,
)
from sanad.accounts.records import CallbackToken, Doctor, SubjectBinding
from sanad.channels.telegram import wording
from sanad.store import keys
from sanad.store._base import Check, Write
from sanad.store.records import from_record, record_item, to_record
from store.account_fixtures import ADMIN, APPLICANT, BOT, PATIENT, AccountWorld, callback, update
from store.test_accounts import accounts as accounts


@pytest.mark.parametrize("case", ["wrong_actor", "expired", "stale", "replay", "unknown"])
def test_callback_refusals_are_neutral_and_change_no_account_state(
    accounts: AccountWorld, case: str
) -> None:
    app = accounts.apply()
    raw, actor = accounts.token(), accounts.actor()
    if case == "wrong_actor":
        actor = accounts.actor(APPLICANT)
    elif case == "expired":
        accounts.clock.advance(timedelta(days=7))
    elif case == "stale":
        accounts.runtime.accounts.reject(
            RejectDoctor(
                command_id="reject",
                actor=actor,
                application_id=app.id,
                expected_application_version=1,
                reason_code="unverified",
            )
        )
    elif case == "replay":
        assert accounts.runtime.accounts.callback(raw, actor, "first").status == "accepted"
    elif case == "unknown":
        raw = "unknown-synthetic-token"
    before = accounts.runtime.accounts.application(app.id)
    intents_before = accounts.intents()
    result = accounts.runtime.accounts.callback(raw, actor, "refuse")
    assert isinstance(result, CallbackRefused)
    assert CallbackRefused.model_validate_json(result.model_dump_json()) == result
    assert accounts.post(callback(raw, actor.subject, id=99)).status_code == 200
    assert accounts.transport.callback_calls[-1].text == wording.render("callback_refused", "en")
    assert accounts.runtime.accounts.application(app.id) == before and [
        (i.id, i.payload, i.source_versions) for i in accounts.intents()
    ] == [(i.id, i.payload, i.source_versions) for i in intents_before]
    assert raw not in accounts.receipt(99).model_dump_json()


def test_callback_hash_single_use_and_duplicate_http_update(accounts: AccountWorld) -> None:
    accounts.apply()
    raw = accounts.token()
    row = accounts.store.get(accounts.runtime.accounts.scope, "callback_token", keys.digest(raw))
    assert row and row.key == keys.token("callback", keys.digest(raw))
    assert raw not in row.model_dump_json()
    assert from_record(row, CallbackToken).consumed_at is None
    assert accounts.post(callback(raw)).status_code == 200
    receipt = accounts.receipt(2)
    assert accounts.post(callback(raw)).status_code == 200
    assert accounts.receipt(2) == receipt and len(accounts.transport.callback_calls) == 1
    assert accounts.post(callback(raw, id=3)).status_code == 200
    assert len(accounts.transport.callback_calls) == 2
    assert accounts.transport.callback_calls[-1].text == wording.render("callback_refused", "en")
    assert len([i for i in accounts.intents() if i.template_id == "doctor_approved"]) == 1


def test_patient_copied_callback_is_refused_without_admin_rights(accounts: AccountWorld) -> None:
    doctor = accounts.approve()
    accounts.patient(doctor)
    raw = accounts.token()
    assert accounts.post(callback(raw, PATIENT, id=4)).status_code == 200
    assert accounts.transport.callback_calls[-1].text == wording.render("callback_refused", "ar")
    assert accounts.receipt(4).state == "completed"
    assert accounts.actor(PATIENT).verified_roles == frozenset({"patient"})


def test_out_of_order_updates_and_pending_rejected_ack_intervals(accounts: AccountWorld) -> None:
    assert accounts.post(update(id=8)).status_code == 200
    assert accounts.post(update(text="Synthetic next", id=7)).status_code == 200
    assert len(accounts.intents()) == 2
    app = accounts.runtime.accounts.application(keys.digest(f"{BOT}:{APPLICANT}"))
    assert app
    accounts.runtime.accounts.reject(
        RejectDoctor(
            command_id="reject",
            actor=accounts.actor(),
            application_id=app.id,
            expected_application_version=app.version,
            reason_code="unverified",
        )
    )
    assert accounts.post(update(text="Synthetic rejected", id=6)).status_code == 200
    assert len([i for i in accounts.intents() if i.template_id == "application_rejected"]) == 1
    accounts.clock.advance(timedelta(hours=24))
    assert accounts.post(update(text="Synthetic rejected", id=5)).status_code == 200
    assert len([i for i in accounts.intents() if i.template_id == "application_rejected"]) == 2


@pytest.mark.parametrize("after", [False, True])
def test_crash_inside_account_transaction_is_all_or_nothing(
    accounts: AccountWorld, monkeypatch: pytest.MonkeyPatch, after: bool
) -> None:
    app = accounts.apply()
    original = accounts.store._atomic
    attempted: list[Write] = []

    def crash(writes: list[Write], checks: list[Check]) -> bool:
        if any(w.item.get("entity_type") == "doctor" for w in writes):
            attempted.extend(writes)
            if after:
                assert original(writes, checks)
            raise SimulatedCrash("account transaction")
        return original(writes, checks)

    command = ApproveDoctor(
        command_id="crash",
        actor=accounts.actor(),
        application_id=app.id,
        expected_application_version=1,
    )
    monkeypatch.setattr(accounts.store, "_atomic", crash)
    with pytest.raises(SimulatedCrash):
        accounts.runtime.accounts.approve(command)
    monkeypatch.setattr(accounts.store, "_atomic", original)
    assert attempted
    for write in attempted:
        if write.before is None:
            assert (accounts.store._read(write.key) is not None) == after
    decided = accounts.runtime.accounts.application(app.id)
    assert decided and decided.status == ("approved" if after else "pending")
    assert accounts.runtime.accounts.approve(command).status == (
        "duplicate" if after else "accepted"
    )
    assert len([i for i in accounts.intents() if i.template_id == "doctor_approved"]) == 1


def test_concurrent_approvals_have_one_doctor_and_one_notification(
    accounts: AccountWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = accounts.apply()
    actor = accounts.actor()
    barrier = Barrier(2, timeout=15)
    original = accounts.store._atomic
    attempted: list[Write] = []

    def race(writes: list[Write], checks: list[Check]) -> bool:
        doctors = [w for w in writes if w.item.get("entity_type") == "doctor"]
        if doctors:
            attempted.extend(doctors)
            barrier.wait()
        return original(writes, checks)

    monkeypatch.setattr(accounts.store, "_atomic", race)

    def approve(index: int) -> str:
        return accounts.runtime.accounts.approve(
            ApproveDoctor(
                command_id=f"device-{index}",
                actor=actor,
                application_id=app.id,
                expected_application_version=1,
            )
        ).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, range(2)))
    assert results.count("accepted") == 1
    assert set(results) <= {"accepted", "already_in_state", "stale_version"}
    assert sum(accounts.store._read(w.key) is not None for w in attempted) == 1
    assert len([i for i in accounts.intents() if i.template_id == "doctor_approved"]) == 1


def test_conflicting_subject_insert_rolls_back_entire_approval(
    accounts: AccountWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = accounts.approve("20009")
    app = accounts.apply()
    original = accounts.store._atomic
    conflict = SubjectBinding(
        id=keys.subject(BOT, APPLICANT).pk,
        scope=accounts.runtime.accounts.scope,
        telegram_user_id=APPLICANT,
        private_chat_id=APPLICANT,
        role_set=frozenset({"patient"}),
        doctor_id=other.id,
        patient_id="synthetic-other-patient",
        created_at=accounts.clock(),
        updated_at=accounts.clock(),
    )
    attempted: list[Write] = []

    def insert_conflict(writes: list[Write], checks: list[Check]) -> bool:
        if any(
            w.item.get("entity_type") == "subject_binding" and w.key.pk == conflict.id
            for w in writes
        ):
            attempted.extend(writes)
            assert original([Write(record_item(to_record(conflict, conflict.scope)), None)], [])
        return original(writes, checks)

    monkeypatch.setattr(accounts.store, "_atomic", insert_conflict)
    result = accounts.runtime.accounts.approve(
        ApproveDoctor(
            command_id="conflict",
            actor=accounts.actor(),
            application_id=app.id,
            expected_application_version=1,
        )
    )
    assert result.status == "stale_version"
    assert accounts.runtime.accounts.application(app.id) == app
    for write in attempted:
        if write.item.get("entity_type") in {"doctor", "doctor_authority"}:
            assert accounts.store._read(write.key) is None
    assert accounts.store.authorize(BOT, APPLICANT).binding == conflict


@pytest.mark.parametrize("status", ["pending", "suspended", "revoked", "rejected"])
def test_doctor_status_is_read_from_canonical_row(accounts: AccountWorld, status: str) -> None:
    doctor = accounts.approve()
    changed = Doctor.model_validate(doctor.model_dump() | {"status": status, "version": 2})
    assert accounts.store._atomic([Write(record_item(to_record(changed, changed.scope)), 1)], [])
    auth = accounts.store.authorize(BOT, APPLICANT)
    assert auth.doctor_status == status and "doctor" not in auth.principal.verified_roles
    assert auth.principal.actor_kind == "unknown"


def test_stored_admin_role_is_not_authority_and_account_commands_cannot_write_it(
    accounts: AccountWorld,
) -> None:
    binding = SubjectBinding(
        id=keys.subject(BOT, APPLICANT).pk,
        scope=accounts.runtime.accounts.scope,
        telegram_user_id=APPLICANT,
        private_chat_id=APPLICANT,
        role_set=frozenset({"admin"}),
        created_at=accounts.clock(),
        updated_at=accounts.clock(),
    )
    assert (
        accounts.runtime.accounts._commit(
            AccountCommand(command_id="forged-role", actor=accounts.actor()), (binding,)
        ).status
        == "forbidden"
    )
    assert accounts.store._atomic([Write(record_item(to_record(binding, binding.scope)), None)], [])
    assert accounts.actor(APPLICANT).actor_kind == "unknown"
    assert accounts.actor(ADMIN).verified_roles == frozenset({"admin"})


def test_exhausted_account_receipt_retains_operational_ownership(accounts: AccountWorld) -> None:
    from sanad.channels.telegram.router import route_receipt
    from sanad.store.records import OperationalIssue

    world = AccountWorld.create(accounts.store, accounts.clock, process=False)
    assert world.post(update()).status_code == 200
    receipt = world.receipt(1)
    key = to_record(receipt, receipt.scope).scoped_key(receipt.scope)
    for _ in range(5):
        current = world.receipt(1)
        assert world.store.claim_work(
            key, current.version, "dead-worker", world.clock(), timedelta(seconds=1)
        )
        world.clock.advance(timedelta(seconds=2))
    assert route_receipt(world.runtime, key).status == "needs_attention"
    current = world.receipt(1)
    assert (
        current.state == "needs_attention" and current.work_clock and current.review_obligation_id
    )
    row = world.store.get(
        world.runtime.accounts.scope, "operational_issue", current.review_obligation_id
    )
    assert row and from_record(row, OperationalIssue).owner_id == ADMIN
    assert not world.intents()
    assert route_receipt(world.runtime, key).status == "needs_attention"


def test_admin_reject_callback_consumes_token_and_sends_neutral_decision(
    accounts: AccountWorld,
) -> None:
    app = accounts.apply()
    raw = accounts.token(action=1)
    assert accounts.post(callback(raw, id=22)).status_code == 200
    decided = accounts.runtime.accounts.application(app.id)
    assert decided and decided.status == "rejected" and decided.reviewer_id == ADMIN
    notice = next(i for i in accounts.intents() if i.template_id == "application_rejected")
    assert accounts.dispatch(notice).status == "provider_accepted"
    assert accounts.transport.calls[-1].payload["text"] == wording.render(
        "application_rejected", "en"
    )
    row = accounts.store.get(app.scope, "callback_token", keys.digest(raw))
    assert row and from_record(row, CallbackToken).consumed_at == accounts.clock()
    assert accounts.actor(APPLICANT).actor_kind == "unknown"
