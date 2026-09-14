"""Contract 35: a start-parameter access code approves doctor applications."""

import json
import logging
import re
from typing import Any, cast
from uuid import uuid4

import pytest
from harness import FakeClock
from pydantic import SecretStr

from deploy.ops import doctor_access_code
from sanad.accounts.commands import (
    AlreadyInState,
    ApproveDoctor,
    CallbackRefused,
    RejectDoctor,
)
from sanad.accounts.records import Application
from sanad.api.app import create_app
from sanad.api.internal import process_event
from sanad.channels.telegram.router import TelegramRuntime
from sanad.channels.telegram.settings import TelegramSettings
from sanad.channels.transport import CapturedTransport
from sanad.domain import TenantScope
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.keys import AccountScope, ScopedKey
from sanad.store.records import DoctorAuthority, from_record
from sanad.web.settings import WebSettings
from store.account_fixtures import ADMIN, APPLICANT, BOT, PATIENT, AccountWorld, settings, update
from store.login_fixtures import CONSENT_POLICY, ORIGIN, LoginWorld

CODE = "synthetic-doctor-access-code-42"
INVALID = "short-code-1"


def code_settings(code: str | None = CODE) -> TelegramSettings:
    base = settings()
    return TelegramSettings(
        bot_id=base.bot_id,
        admin_user_id=base.admin_user_id,
        bot_token=base.bot_token,
        webhook_secret=base.webhook_secret,
        doctor_access_code=SecretStr(code) if code is not None else None,
    )


def code_world(store: StoreBase, clock: FakeClock, code: str | None = CODE) -> AccountWorld:
    transport = CapturedTransport()

    def submit(key: ScopedKey) -> None:
        process_event(
            app.state.telegram,
            {"type": "process_receipt", "receipt": key.model_dump(mode="json")},
        )

    app = create_app(
        telegram_settings=code_settings(code),
        store=store,
        clock=clock,
        transport=transport,
        process_receipts=True,
        receipt_submit=submit,
    )
    return AccountWorld(store, clock, cast(TelegramRuntime, app.state.telegram), app, transport)


def wired_world(store: StoreBase, clock: FakeClock, code: str | None = CODE) -> LoginWorld:
    """Full routing (identity, claims, concierge, scribe) as the deployed app wires it."""
    transport = CapturedTransport()

    def submit(key: ScopedKey) -> None:
        process_event(
            app.state.telegram,
            {"type": "process_receipt", "receipt": key.model_dump(mode="json")},
        )

    app = create_app(
        telegram_settings=code_settings(code),
        store=store,
        clock=clock,
        transport=transport,
        process_receipts=True,
        receipt_submit=submit,
        web_settings=WebSettings(public_base_url=ORIGIN, bot_username="synthetic_sanad_bot"),
        consent_policy=lambda doctor_id: CONSENT_POLICY,
    )
    return LoginWorld(store, clock, cast(TelegramRuntime, app.state.telegram), app, transport)


def application(world: AccountWorld, subject: str) -> Application | None:
    return world.runtime.accounts.application(keys.digest(f"{BOT}:{subject}"))


def partition_items(store: StoreBase, pk: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        page, cursor = store._query(pk, cursor=cursor, limit=1000)
        items.extend(page)
        if cursor is None:
            return items


def test_start_with_the_code_approves_a_new_doctor(store: StoreBase, clock: FakeClock) -> None:
    world = code_world(store, clock)
    assert world.post(update(APPLICANT, "/start " + CODE, 1)).status_code == 200
    auth = world.store.authorize(BOT, APPLICANT)
    assert auth.principal.actor_kind == "doctor"
    assert auth.principal.verified_roles == frozenset({"doctor"})
    assert auth.auth_epoch == 1
    assert auth.binding and auth.binding.role_set == frozenset({"doctor"})
    decided = application(world, APPLICANT)
    assert decided and decided.status == "approved" and decided.approval_reference == "access_code"
    assert decided.reviewer_id == ADMIN and decided.doctor_id
    assert auth.binding.doctor_id == decided.doctor_id
    doctor = world.runtime.accounts.doctor(decided.doctor_id)
    assert doctor and doctor.status == "approved" and doctor.approved_by == ADMIN
    assert doctor.telegram_user_id == APPLICANT and doctor.application_id == decided.id
    authority = world.store.get(doctor.scope, "doctor_authority", doctor.id)
    assert authority and from_record(authority, DoctorAuthority).approved
    assert [i.template_id for i in world.intents()] == ["doctor_approved"]
    account = partition_items(store, keys.partition(AccountScope(bot_id=BOT)))
    assert [i for i in account if i.get("entity_type") == "callback_token"] == []
    assert world.receipt(1).state == "completed"


def test_pending_application_then_the_code_approves_the_same_application(
    store: StoreBase, clock: FakeClock
) -> None:
    world = code_world(store, clock)
    assert world.post(update(APPLICANT, "/start", 1)).status_code == 200
    pending = application(world, APPLICANT)
    assert pending and pending.status == "pending" and pending.version == 1
    token = world.token()
    assert world.post(update(APPLICANT, "/start " + CODE, 2)).status_code == 200
    decided = application(world, APPLICANT)
    assert decided and decided.id == pending.id and decided.status == "approved"
    assert decided.version == 2 and decided.approval_reference == "access_code"
    refused = world.runtime.accounts.callback(token, world.actor(ADMIN), "late-review")
    assert isinstance(refused, CallbackRefused) and refused.reason == "stale_version"
    second = world.runtime.accounts.approve(
        ApproveDoctor(
            command_id="second:" + uuid4().hex,
            actor=world.actor(ADMIN),
            application_id=decided.id,
            expected_application_version=decided.version,
        )
    )
    assert isinstance(second, AlreadyInState) and second.state == "approved"
    templates = [i.template_id for i in world.intents()]
    assert templates.count("doctor_approved") == 1 and templates.count("admin_new_application") == 1


def _pending_outcome(world: AccountWorld, subject: str, text: str, id: int) -> None:
    assert world.post(update(subject, text, id)).status_code == 200
    current = application(world, subject)
    assert current and current.status == "pending"
    assert world.store.authorize(BOT, subject).principal.actor_kind == "unknown"


def test_wrong_code_feature_off_and_invalid_format_stay_pending(
    store: StoreBase, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="sanad.channels.telegram.settings")
    world = code_world(store, clock)
    _pending_outcome(world, APPLICANT, "/start wrong-wrong-wrong-xx", 1)
    _pending_outcome(world, "20003", "/start", 2)
    _pending_outcome(world, "20004", "/start ", 3)
    off = code_world(store, clock, code=None)
    _pending_outcome(off, "20005", "/start " + CODE, 4)
    invalid = code_world(store, clock, code=INVALID)
    _pending_outcome(invalid, "20006", "/start " + INVALID, 5)
    assert [
        r.getMessage() for r in caplog.records if r.getMessage() == "doctor access code invalid"
    ] == ["doctor access code invalid"]
    assert INVALID not in caplog.text and CODE not in caplog.text
    templates = [i.template_id for i in world.intents()]
    assert templates.count("admin_new_application") == 5
    assert templates.count("application_received") == 5
    assert "doctor_approved" not in templates
    assert not [
        i
        for i in partition_items(store, keys.partition(AccountScope(bot_id=BOT)))
        if i.get("entity_type") == "doctor"
    ]


def test_rejected_application_stays_rejected_with_the_code(
    store: StoreBase, clock: FakeClock
) -> None:
    world = code_world(store, clock)
    pending = world.apply(APPLICANT)
    assert (
        world.runtime.accounts.reject(
            RejectDoctor(
                command_id="reject:" + uuid4().hex,
                actor=world.actor(ADMIN),
                application_id=pending.id,
                expected_application_version=pending.version,
                reason_code="unverified",
            )
        ).status
        == "accepted"
    )
    assert world.post(update(APPLICANT, "/start " + CODE, 2)).status_code == 200
    rejected = application(world, APPLICANT)
    assert rejected and rejected.status == "rejected" and rejected.version == 2
    assert rejected.doctor_id is None
    auth = world.store.authorize(BOT, APPLICANT)
    assert auth.principal.actor_kind == "unknown" and auth.binding is None
    assert "doctor_approved" not in [i.template_id for i in world.intents()]


def test_replayed_update_creates_one_doctor(store: StoreBase, clock: FakeClock) -> None:
    world = code_world(store, clock)
    body = update(APPLICANT, "/start " + CODE, 1)
    assert world.post(body).status_code == 200
    assert world.post(body).status_code == 200
    decided = application(world, APPLICANT)
    assert decided and decided.status == "approved" and decided.doctor_id
    tenant = partition_items(store, keys.partition(TenantScope(doctor_id=decided.doctor_id)))
    assert len([i for i in tenant if i.get("entity_type") == "doctor"]) == 1
    assert [i.template_id for i in world.intents()] == ["doctor_approved"]


def test_the_code_value_is_never_stored_or_logged(
    store: StoreBase, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    world = code_world(store, clock)
    assert world.post(update(APPLICANT, "/start " + CODE, 1)).status_code == 200
    decided = application(world, APPLICANT)
    assert decided and decided.status == "approved" and decided.doctor_id
    written = [
        partition_items(store, keys.partition(AccountScope(bot_id=BOT))),
        partition_items(store, keys.partition(TenantScope(doctor_id=decided.doctor_id))),
        partition_items(store, keys.inbound("telegram", keys.digest(f"{BOT}:1")).pk),
        partition_items(store, keys.subject(BOT, APPLICANT).pk),
    ]
    assert all(items for items in written)
    for items in written:
        assert CODE not in json.dumps(items)
    assert CODE not in caplog.text


def test_judge_code_and_patient_invitation_in_the_wired_app(
    store: StoreBase, clock: FakeClock
) -> None:
    world = wired_world(store, clock)
    world.approve()
    assert world.post(update("60006", "/start " + CODE, 300)).status_code == 200
    judge = application(world, "60006")
    assert judge and judge.status == "approved" and judge.approval_reference == "access_code"
    assert world.store.authorize(BOT, "60006").principal.actor_kind == "doctor"
    assert world.post(update("60007", "/start wrong-wrong-wrong-xx", 301)).status_code == 200
    assert application(world, "60007") is None
    assert world.store.authorize(BOT, "60007").principal.actor_kind == "unknown"
    assert [
        i
        for i in world.intents()
        if i.template_id == "claim_refused" and i.recipient_subject == "60007"
    ]
    patient = world.stub()
    pending = world.claim(world.invite(patient), id=305)
    assert pending.state == "pending"
    assert application(world, PATIENT) is None
    assert world.store.authorize(BOT, PATIENT).binding is None


def test_bound_patient_is_refused_and_binding_unchanged(store: StoreBase, clock: FakeClock) -> None:
    world = wired_world(store, clock)
    world.approve()
    assert world.bound().contact_status == "active"
    before = world.store.authorize(BOT, PATIENT)
    assert before.principal.actor_kind == "patient"
    assert world.post(update(PATIENT, "/start " + CODE, 310)).status_code == 200
    after = world.store.authorize(BOT, PATIENT)
    assert after.principal.actor_kind == "patient" and after.binding == before.binding
    assert application(world, PATIENT) is None
    assert [
        i
        for i in world.intents()
        if i.template_id == "claim_refused" and i.recipient_subject == PATIENT
    ]


class FakeSSM:
    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self) -> None:
        self.parameters: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def put_parameter(self, **kwargs: Any) -> None:
        self.calls.append(("put", kwargs))
        self.parameters[kwargs["Name"]] = kwargs["Value"]

    def delete_parameter(self, **kwargs: Any) -> None:
        self.calls.append(("delete", kwargs))
        if kwargs["Name"] not in self.parameters:
            raise FakeSSM.exceptions.ParameterNotFound()


def test_operator_doctor_access_code_set_and_clear(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ssm = FakeSSM()
    code = doctor_access_code(ssm, "dev", "set")
    assert isinstance(code, str)
    assert len(code) == 16 and re.fullmatch(r"[A-Za-z0-9_-]{16,64}", code)
    assert ssm.calls == [
        (
            "put",
            {
                "Name": "/sanad/dev/doctor-access-code",
                "Value": code,
                "Type": "SecureString",
                "Overwrite": True,
            },
        )
    ]
    assert ssm.parameters == {"/sanad/dev/doctor-access-code": code}
    assert code in capsys.readouterr().out
    assert doctor_access_code(ssm, "dev", "clear") is None
    cleared = capsys.readouterr().out
    assert "/sanad/dev/doctor-access-code cleared" in cleared
    assert code not in cleared
    assert ssm.calls[-1] == ("delete", {"Name": "/sanad/dev/doctor-access-code"})
    assert doctor_access_code(ssm, "judge", "clear") is None
