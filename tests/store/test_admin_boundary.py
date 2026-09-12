"""Administrator identity, transactions and complete mounted-route refusal oracle."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import httpx
import pytest
from harness import FakeClock
from providers.fixtures import FakeS3Client
from pydantic import ValidationError

from sanad.auth.commands import IssueAdminLogin
from sanad.media.storage import S3MediaStore
from sanad.media.upload import UploadIngress
from sanad.store._base import Check, StoreBase, Write
from sanad.store.memory import MemoryStore
from sanad.store.records import AdminAccount, AuditEvent, IdentityConfig, LoginExchange, WebSession
from sanad.web.routes import upload_router
from sanad.web.security import redact
from store.account_fixtures import ADMIN, BOT, update
from store.login_fixtures import ORIGIN, LoginWorld, browser_login
from store.test_login_claim import enrollment as enrollment

if TYPE_CHECKING:
    from starlette.testclient import TestClient


def admin_path(world: LoginWorld) -> str:
    previous = {i.id for i in world.intents()}
    assert world.post(update(ADMIN, "/login admin", int(uuid4().hex[:10], 16))).status_code == 200
    intent = next(
        i for i in world.intents() if i.id not in previous and i.template_id == "admin_login_link"
    )
    assert world.dispatch(intent).status == "provider_accepted"
    assert intent.payload
    return str(intent.payload["text"]).splitlines()[-1].removeprefix(ORIGIN)


def headers(client: TestClient) -> dict[str, str]:
    return {"origin": ORIGIN, "x-csrf-token": client.cookies["sanad_csrf"]}


def mounted_routes() -> list[tuple[str, str]]:
    clock = FakeClock(datetime(2026, 9, 11, tzinfo=UTC))
    app = LoginWorld.create(MemoryStore(clock=clock), clock).app

    def walk(routes: Any) -> list[tuple[str, str]]:
        result = []
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                result.extend(walk(nested.routes))
            else:
                result.extend((method, route.path) for method in route.methods or ())
        return result

    app.include_router(
        upload_router(
            UploadIngress(
                app.state.telegram, app.state.login, S3MediaStore("synthetic", FakeS3Client())
            ),
            app.state.web_settings,
        )
    )
    return sorted(walk(app.routes))


ROUTES = mounted_routes()
PUBLIC = {
    ("GET", "/health"),
    ("GET", "/d/{token}"),
    ("POST", "/d/{token}"),
    ("GET", "/pl/{token}"),
    ("POST", "/pl/{token}"),
    ("GET", "/ad/{token}"),
    ("POST", "/ad/{token}"),
    ("GET", "/p/{token}"),
    ("GET", "/assets/{name}"),
    ("GET", "/demo"),
}
ADMIN_ROUTES = {
    ("GET", "/admin"),
    ("GET", "/api/admin/applications"),
    ("POST", "/api/admin/applications/{id}/approve"),
    ("POST", "/api/admin/applications/{id}/reject"),
    ("POST", "/api/admin/doctors/{id}/suspend"),
    ("POST", "/api/admin/doctors/{id}/reinstate"),
    ("POST", "/api/admin/logout"),
}


@pytest.mark.parametrize("method,path", ROUTES)
def test_admin_mounted_route_boundary(enrollment: LoginWorld, method: str, path: str) -> None:
    world = enrollment
    world.app.include_router(
        upload_router(
            UploadIngress(world.runtime, world.login, S3MediaStore("synthetic", FakeS3Client())),
            world.app.state.web_settings,
        )
    )
    with world.client() as client:
        assert browser_login(client, admin_path(world)).status_code == 303
        target = re.sub(r"\{[^}]+\}", "synthetic", path)
        response = client.request(method, target, headers=headers(client))
        if (method, path) not in PUBLIC | ADMIN_ROUTES:
            assert response.status_code in {401, 403}, (method, path, response.status_code)
            assert "Synthetic Doctor" not in response.text
        elif (method, path) == ("GET", "/admin"):
            assert response.status_code == 200 and "Sign out everywhere" in response.text
        elif (method, path) == ("GET", "/api/admin/applications"):
            assert response.status_code == 200


def test_admin_login_shape_replay_and_reissue(enrollment: LoginWorld) -> None:
    world = enrollment
    assert world.store.authorize(BOT, ADMIN).admin_epoch is None
    first, path = admin_path(world), admin_path(world)
    assert redact(ORIGIN + path).endswith("/ad/<redacted>")
    with world.client() as client:
        assert browser_login(client, first).status_code == 403
        assert browser_login(client, path).status_code == 303
        session = world.login.session(client.cookies["sanad_session"])
        assert session and session.role == "admin" and session.doctor_id is None
        assert session.patient_id is None and session.auth_epoch == 1
        assert world.store.authorize(BOT, ADMIN).auth_epoch is None
        assert browser_login(client, path).status_code == 403


@pytest.mark.parametrize(
    "field,value",
    [
        ("doctor_id", "clinical"),
        ("patient_id", "patient"),
        ("binding_id", "binding"),
        ("binding_epoch", 1),
        ("consent_version", 1),
    ],
)
def test_admin_shapes_forbid_clinical_fields(
    enrollment: LoginWorld, field: str, value: object
) -> None:
    world = enrollment
    path = admin_path(world)
    with world.client() as client:
        assert browser_login(client, path).status_code == 303
        session = world.login.session(client.cookies["sanad_session"])
        assert session
        with pytest.raises(ValidationError):
            WebSession[str | None].model_validate(session.model_dump() | {field: value})
        from sanad.store import keys

        exchange = world.login.load(
            world.login.scope, "admin_login", keys.digest(path.split("/")[-1]), LoginExchange
        )
        assert exchange
        with pytest.raises(ValidationError):
            LoginExchange.model_validate(exchange.model_dump() | {field: value})


@pytest.mark.parametrize("cause", ["logout", "telegram", "changed_admin", "idle", "absolute"])
def test_admin_revocation(enrollment: LoginWorld, cause: str) -> None:
    world = enrollment
    with world.client() as client:
        assert browser_login(client, admin_path(world)).status_code == 303
        raw = client.cookies["sanad_session"]
        unused = admin_path(world)
        doctor = world.doctor
        if cause == "logout":
            assert client.post("/api/admin/logout", headers=headers(client)).status_code == 200
        elif cause == "telegram":
            assert world.post(update(ADMIN, "/logout", 300)).status_code == 200
        elif cause == "changed_admin":
            world.store.configure_identity(IdentityConfig(bot_id=BOT, admin_user_id="999"))
        else:
            world.clock.advance(timedelta(minutes=30) if cause == "idle" else timedelta(hours=12))
        assert world.login.require(raw, "admin") is None
        assert world.doctor == doctor
        if cause in {"logout", "telegram", "changed_admin"}:
            assert browser_login(client, unused).status_code == 403


@pytest.mark.parametrize("dual", [False, True])
def test_logout_without_admin_record_creates_nothing(enrollment: LoginWorld, dual: bool) -> None:
    world = enrollment
    if dual:
        world.approve(ADMIN)
    assert world.post(update(ADMIN, "/logout", 300)).status_code == 200
    assert world.login.load(world.login.scope, "admin_account", ADMIN, AdminAccount) is None
    intent = next(
        i
        for i in world.intents()
        if i.template_id == ("dashboard_signed_out" if dual else "admin_no_sessions")
    )
    assert world.dispatch(intent).status == "provider_accepted"


def test_non_admin_cannot_issue(enrollment: LoginWorld) -> None:
    assert (
        enrollment.login.issue(
            IssueAdminLogin(command_id=uuid4().hex, actor=enrollment.owner)
        ).status
        == "forbidden"
    )


def test_admin_account_actions_and_provenance(enrollment: LoginWorld) -> None:
    world = enrollment
    application = world.apply("45678")
    rejected = world.apply("45679")
    with world.client() as client:
        assert browser_login(client, admin_path(world)).status_code == 303

        def post(path: str, version: int, **extra: str) -> httpx.Response:
            return cast(
                httpx.Response,
                client.post(
                    path,
                    headers=headers(client),
                    json={"command_id": uuid4().hex, "expected_version": version, **extra},
                ),
            )

        target = f"/api/admin/applications/{application.id}/approve"
        assert post(target, 99).json()["status"] == "stale_version"
        body = {"command_id": uuid4().hex, "expected_version": application.version}
        assert client.post(target, headers=headers(client), json=body).status_code == 200
        assert (
            client.post(target, headers=headers(client), json=body).json()["status"] == "duplicate"
        )
        assert post(target, application.version).json()["status"] == "already_in_state"
        assert (
            post(
                f"/api/admin/applications/{rejected.id}/reject",
                rejected.version,
                reason_code="unverified",
            ).status_code
            == 200
        )
        decided = world.runtime.accounts.application(application.id)
        assert decided and decided.doctor_id
        doctor = world.runtime.accounts.doctor(decided.doctor_id)
        assert doctor
        assert (
            post(
                f"/api/admin/doctors/{doctor.id}/suspend", doctor.version, reason_code="coverage"
            ).status_code
            == 200
        )
        rows = client.get("/api/admin/applications").json()
        row = next(r for r in rows if r["id"] == application.id)
        assert row["status"] == "suspended" and row["doctor_version"] == doctor.version + 1
        assert (
            post(f"/api/admin/doctors/{doctor.id}/reinstate", row["doctor_version"]).status_code
            == 200
        )
        events, _ = world.store.list_records(world.login.scope, "audit_event")
        web_events = [
            r
            for r in events
            if r.body.get("channel") == "web-admin" and r.body["event_type"] != "AccountCommand"
        ]
        assert {r.body["event_type"] for r in web_events} == {
            "ApproveDoctor",
            "RejectDoctor",
            "SuspendDoctor",
            "ReinstateDoctor",
        }


@pytest.mark.parametrize("bad", ["missing_pre", "csrf", "origin"])
def test_admin_exchange_security(enrollment: LoginWorld, bad: str) -> None:
    world = enrollment
    path = admin_path(world)
    with world.client() as client:
        page = client.get(path)
        nonce = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert nonce
        if bad == "missing_pre":
            client.cookies.clear()
        assert (
            client.post(
                path,
                data={"csrf": "wrong" if bad == "csrf" else nonce[1]},
                headers={"origin": "https://wrong.example" if bad == "origin" else ORIGIN},
            ).status_code
            == 403
        )


@pytest.mark.parametrize("bad", ["csrf", "origin"])
def test_admin_write_security(enrollment: LoginWorld, bad: str) -> None:
    world = enrollment
    application = world.apply("45678")
    with world.client() as client:
        assert browser_login(client, admin_path(world)).status_code == 303
        request_headers = headers(client) | (
            {"origin": "https://wrong.example"} if bad == "origin" else {"x-csrf-token": "wrong"}
        )
        assert (
            client.post(
                f"/api/admin/applications/{application.id}/approve",
                headers=request_headers,
                json={"command_id": uuid4().hex, "expected_version": application.version},
            ).status_code
            == 403
        )
        assert world.runtime.accounts.application(application.id) == application


def test_dual_role_isolation_and_independent_epoch(enrollment: LoginWorld) -> None:
    world = enrollment
    doctor = world.approve(ADMIN)
    with world.client() as client:
        assert browser_login(client, admin_path(world)).status_code == 303
        assert client.get("/api/me").status_code in {401, 403}
        assert browser_login(client, admin_path(world)).status_code == 303
        admin_cookie = client.cookies["sanad_session"]
        assert world.post(update(ADMIN, "/login", 400)).status_code == 200
        intent = next(
            i
            for i in world.intents()
            if i.template_id == "doctor_login_link" and i.recipient_subject == ADMIN
        )
        assert intent.payload
        assert (
            browser_login(
                client, str(intent.payload["text"]).splitlines()[-1].removeprefix(ORIGIN)
            ).status_code
            == 303
        )
        assert world.login.require(admin_cookie, "admin") is None
        assert client.get("/api/me").status_code == 200
        assert client.get("/api/admin/applications").status_code == 401
        assert world.runtime.accounts.doctor(doctor.id) == doctor


def test_browser_and_telegram_approval_are_business_equivalent(
    store: StoreBase, clock: FakeClock
) -> None:
    from copy import deepcopy

    from sanad.store import keys
    from sanad.store._base import Write
    from sanad.store.records import SubjectBinding, from_record
    from store.account_fixtures import APPLICANT, callback

    initial_store = MemoryStore(clock=clock)
    initial = LoginWorld.create(initial_store, clock)
    initial.apply()
    with initial.client() as client:
        assert browser_login(client, admin_path(initial)).status_code == 303
        cookies = dict(client.cookies)
    baseline = deepcopy(initial_store._items)
    results = []
    for adapter, target_store in zip(
        ("browser", "telegram"), (store, MemoryStore(clock=clock)), strict=True
    ):
        for item in baseline.values():
            assert target_store._atomic([Write(deepcopy(item), None)], [])
        world = LoginWorld.create(target_store, clock)
        app = world.runtime.accounts.application(keys.digest(f"{BOT}:{APPLICANT}"))
        assert app
        if adapter == "browser":
            with world.client() as client:
                client.cookies.update(cookies)
                response = client.post(
                    f"/api/admin/applications/{app.id}/approve",
                    headers=headers(client),
                    json={"command_id": uuid4().hex, "expected_version": app.version},
                )
                assert response.status_code == 200
        else:
            assert world.post(callback(world.token(), ADMIN, 999)).status_code == 200
        decided = world.runtime.accounts.application(app.id)
        assert decided and decided.doctor_id
        doctor = world.runtime.accounts.doctor(decided.doctor_id)
        assert doctor
        binding_row = target_store.get(world.login.scope, "subject_binding", APPLICANT)
        assert binding_row
        binding = from_record(binding_row, SubjectBinding)
        events, _ = target_store.list_records(world.login.scope, "audit_event")
        event = from_record(
            next(r for r in events if r.body["event_type"] == "ApproveDoctor"), AuditEvent
        )
        notice = next(i for i in world.intents() if i.template_id == "doctor_approved")
        results.append(
            {
                "application": decided.model_dump(exclude={"doctor_id", "approval_reference"}),
                "doctor": doctor.model_dump(exclude={"id", "scope"}),
                "binding": binding.model_dump(exclude={"doctor_id"}),
                "event": (
                    event.event_type,
                    sorted(
                        (r.entity_type, r.version)
                        for r in event.after_versions
                        if r.entity_type != "callback_token"
                    ),
                ),
                "notice": notice.model_dump(
                    include={
                        "audience",
                        "recipient_ref",
                        "template_id",
                        "payload",
                        "recipient_auth_epoch_seen",
                    }
                ),
            }
        )
        assert event.channel == ("web-admin" if adapter == "browser" else None)
    assert results[0] == results[1]


@pytest.mark.parametrize("role,epoch", [("doctor", 1), ("patient", 1), ("admin", 999)])
def test_account_transaction_refuses_wrong_session_role_or_epoch(
    enrollment: LoginWorld, role: str, epoch: int
) -> None:
    from sanad.accounts.commands import ApproveDoctor

    world = enrollment
    application = world.apply("45678")
    admin_path(world)
    command = ApproveDoctor.model_validate(
        dict(
            command_id=uuid4().hex,
            actor=world.actor(),
            application_id=application.id,
            expected_application_version=application.version,
            session_role=role,
            admin_epoch=epoch,
        )
    )
    # Bypass the adapter to exercise the store's own account transaction guard.
    result = world.runtime.accounts._commit(command)
    assert result.status == "forbidden"
    assert world.runtime.accounts.application(application.id) == application


def test_admin_epoch_cas_fences_account_mutation(
    enrollment: LoginWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.auth.service import revise
    from sanad.store._base import Write
    from sanad.store.records import record_item, to_record

    world = enrollment
    application = world.apply("45678")
    with world.client() as client:
        assert browser_login(client, admin_path(world)).status_code == 303
        original = world.store._atomic
        raced = False

        def atomic(writes: list[Write], checks: list[Check]) -> bool:
            nonlocal raced
            if not raced and any(w.item.get("entity_type") == "doctor" for w in writes):
                raced = True
                admin = world.login.load(world.login.scope, "admin_account", ADMIN, AdminAccount)
                assert admin
                row = to_record(
                    revise(admin, world.clock(), auth_epoch=admin.auth_epoch + 1), admin.scope
                )
                assert original([Write(record_item(row), admin.version)], [])
            return original(writes, checks)

        monkeypatch.setattr(world.store, "_atomic", atomic)
        response = client.post(
            f"/api/admin/applications/{application.id}/approve",
            headers=headers(client),
            json={"command_id": uuid4().hex, "expected_version": application.version},
        )
        assert raced and response.status_code in {403, 409}
        assert world.runtime.accounts.application(application.id) == application


def test_admin_logout_leaves_dual_role_doctor_session_alive(enrollment: LoginWorld) -> None:
    world = enrollment
    doctor = world.approve(ADMIN)
    with world.client() as admin, world.client() as clinical:
        assert browser_login(admin, admin_path(world)).status_code == 303
        assert world.post(update(ADMIN, "/login", 901)).status_code == 200
        intent = next(
            i
            for i in world.intents()
            if i.template_id == "doctor_login_link" and i.recipient_subject == ADMIN
        )
        assert intent.payload
        assert (
            browser_login(
                clinical, str(intent.payload["text"]).splitlines()[-1].removeprefix(ORIGIN)
            ).status_code
            == 303
        )
        assert admin.post("/api/admin/logout", headers=headers(admin)).status_code == 200
        assert clinical.get("/api/me").status_code == 200
        assert world.runtime.accounts.doctor(doctor.id) == doctor


def test_dual_role_admin_notifications_use_admin_epoch(enrollment: LoginWorld) -> None:
    world = enrollment
    world.approve(ADMIN)
    admin_path(world)
    assert world.login.revoke_admin(ADMIN, uuid4().hex).status == "accepted"
    application = world.apply("45678")
    intent = next(
        i
        for i in world.intents()
        if i.template_id == "admin_new_application" and i.source_versions[0].id == application.id
    )
    assert intent.recipient_auth_epoch_seen == 2
    assert world.dispatch(intent).status == "provider_accepted"


def test_session_principal_cannot_omit_transaction_role(enrollment: LoginWorld) -> None:
    from sanad.accounts.commands import ApproveDoctor

    world = enrollment
    application = world.apply("45678")
    command = ApproveDoctor(
        command_id=uuid4().hex,
        actor=world.actor().model_copy(update={"session_id": "synthetic-browser-session"}),
        application_id=application.id,
        expected_application_version=application.version,
    )
    assert world.runtime.accounts._commit(command).status == "forbidden"


def test_admin_exchange_replaces_suspended_doctor_cookie(enrollment: LoginWorld) -> None:
    from sanad.accounts.commands import SuspendDoctor

    world = enrollment
    doctor = world.approve(ADMIN)
    with world.client() as client:
        assert world.post(update(ADMIN, "/login", 901)).status_code == 200
        intent = next(
            i
            for i in world.intents()
            if i.template_id == "doctor_login_link" and i.recipient_subject == ADMIN
        )
        assert intent.payload
        assert (
            browser_login(
                client, str(intent.payload["text"]).splitlines()[-1].removeprefix(ORIGIN)
            ).status_code
            == 303
        )
        old_cookie = client.cookies["sanad_session"]
        assert (
            world.runtime.accounts.suspend(
                SuspendDoctor(
                    command_id=uuid4().hex,
                    actor=world.actor(),
                    doctor_id=doctor.id,
                    expected_doctor_version=doctor.version,
                    reason_code="coverage",
                )
            ).status
            == "accepted"
        )
        assert browser_login(client, admin_path(world)).status_code == 303
        assert client.get("/api/admin/applications").status_code == 200
        old = world.login.session(old_cookie)
        assert old and old.revoked_at


def test_admin_snapshot_checks_epoch_on_exact_conditioned_row(
    enrollment: LoginWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.auth.service import revise
    from sanad.store.identity import live_snapshot
    from sanad.store.records import Authorization, record_item, to_record

    world = enrollment
    with world.client() as client:
        assert browser_login(client, admin_path(world)).status_code == 303
        session = world.login.session(client.cookies["sanad_session"])
        assert session
        original = world.store.authorize

        def changing(bot: str, subject: str) -> Authorization:
            auth = original(bot, subject)
            admin = world.login.load(world.login.scope, "admin_account", ADMIN, AdminAccount)
            assert admin
            row = to_record(
                revise(admin, world.clock(), auth_epoch=admin.auth_epoch + 1), admin.scope
            )
            assert world.store._atomic([Write(record_item(row), admin.version)], [])
            return auth

        monkeypatch.setattr(world.store, "authorize", changing)
        assert not live_snapshot(world.store, world.login.scope, session, [])
