import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import BaseModel

from sanad.accounts.commands import SuspendDoctor
from sanad.auth.commands import IssueDoctorLogin, RevokeBinding
from sanad.auth.service import revise
from sanad.auth.tokens import issue_token
from sanad.store import keys
from sanad.store._base import Write
from sanad.store.records import (
    Consent,
    LoginExchange,
    PatientBinding,
    record_item,
    to_record,
)
from store.account_fixtures import APPLICANT, PATIENT
from store.login_fixtures import ORIGIN, LoginWorld, browser_login
from store.test_login_claim import enrollment as enrollment


def suspend(world: LoginWorld) -> None:
    doctor = world.doctor
    assert (
        world.runtime.accounts.suspend(
            SuspendDoctor(
                command_id=uuid4().hex,
                actor=world.actor(),
                doctor_id=doctor.id,
                expected_doctor_version=doctor.version,
                reason_code="synthetic_suspension",
            )
        ).status
        == "accepted"
    )


# Handwritten request-guard oracle: each condition independently denies and revokes.
SESSION_CASES = [
    ("doctor", "current", 200),
    ("doctor", "revoked", 401),
    ("doctor", "idle_boundary", 401),
    ("doctor", "absolute_boundary", 401),
    ("doctor", "suspended", 401),
    ("doctor", "wrong_role", 401),
    ("patient", "current", 200),
    ("patient", "revoked", 401),
    ("patient", "idle_boundary", 401),
    ("patient", "absolute_boundary", 401),
    ("patient", "binding_revoked", 401),
    ("patient", "binding_epoch", 401),
    ("patient", "consent_version", 401),
    ("patient", "consent_withdrawn", 401),
    ("patient", "consent_disabled", 401),
    ("patient", "suspended", 401),
    ("patient", "wrong_role", 401),
]


@pytest.mark.parametrize("role,condition,status", SESSION_CASES)
def test_require_session_table(
    enrollment: LoginWorld, role: str, condition: str, status: int
) -> None:
    world = enrollment
    patient = world.bound() if role == "patient" else None
    path = world.login_path(PATIENT if patient else APPLICANT)
    with world.client() as client:
        assert browser_login(client, path).status_code == 303
        raw = client.cookies["sanad_session"]
        session = world.login.session(raw)
        assert session is not None
        if condition == "revoked":
            world.login.revoke(session)
        elif condition == "idle_boundary":
            world.clock.advance(timedelta(minutes=30))
        elif condition == "absolute_boundary":
            # Keep idle alive until the independent absolute deadline wins.
            for _ in range(24):
                world.clock.advance(timedelta(minutes=29))
                assert world.login.require(raw, session.role)
            world.clock.advance(session.absolute_expires_at - world.clock())
        elif condition == "suspended":
            suspend(world)
        elif condition == "binding_revoked":
            assert patient is not None
            assert (
                world.claims.revoke_binding(
                    RevokeBinding(
                        command_id=uuid4().hex,
                        actor=world.owner,
                        patient_id=patient.id,
                        reason_code="identity_error",
                    )
                ).status
                == "accepted"
            )
        elif condition in {
            "binding_epoch",
            "consent_version",
            "consent_withdrawn",
            "consent_disabled",
        }:
            assert patient and patient.active_binding_id and patient.consent_id
            changed: BaseModel
            if condition == "binding_epoch":
                binding = world.claims.load(
                    patient.scope, "patient_binding", patient.active_binding_id, PatientBinding
                )
                assert binding
                changed = revise(binding, world.clock(), binding_epoch=binding.binding_epoch + 1)
            else:
                consent = world.claims.load(patient.scope, "consent", patient.consent_id, Consent)
                assert consent
                changed = revise(
                    consent,
                    world.clock(),
                    **(
                        {"withdrawn_at": world.clock()}
                        if condition == "consent_withdrawn"
                        else {"routine_contact_enabled": False}
                        if condition == "consent_disabled"
                        else {}
                    ),
                )
            row = to_record(changed, patient.scope)
            assert world.store._atomic([Write(record_item(row), row.version - 1)], [])
        target = "/api/patient/me" if role == "patient" else "/api/me"
        if condition == "wrong_role":
            target = "/api/me" if role == "patient" else "/api/patient/me"
        assert client.get(target).status_code == status
        saved = world.login.session(raw)
        assert saved is not None
        assert (saved.revoked_at is not None) == (status == 401)


def test_previews_never_consume_cookie_rotation_and_replay(enrollment: LoginWorld) -> None:
    world = enrollment
    path = world.login_path()
    hash = keys.digest(path.rsplit("/", 1)[-1])
    with world.client() as client:
        for _ in range(5):
            assert client.get(path).status_code == 200
            exchange = world.login.load(world.login.scope, "doctor_login", hash, LoginExchange)
            assert exchange is not None and exchange.state == "issued"
        fixed = issue_token().secret.get_secret_value()
        client.cookies.set("sanad_session", fixed)
        result = browser_login(client, path)
        assert result.status_code == 303
        headers = result.headers.get_list("set-cookie")
        assert len(headers) == 3
        session_cookie = next(h for h in headers if h.startswith("sanad_session="))
        assert session_cookie.endswith("; HttpOnly; Max-Age=43200; Path=/; SameSite=lax; Secure")
        csrf_cookie = next(h for h in headers if h.startswith("sanad_csrf="))
        assert csrf_cookie.endswith("; Max-Age=43200; Path=/; SameSite=lax; Secure")
        assert fixed not in session_cookie
        # Replayed credential fails even with a fresh, valid pre-session CSRF pair.
        assert browser_login(client, path).status_code == 403


def test_rotation_revokes_previous_real_session(enrollment: LoginWorld) -> None:
    world = enrollment
    with world.client() as client:
        assert browser_login(client, world.login_path()).status_code == 303
        old = client.cookies["sanad_session"]
        assert browser_login(client, world.login_path(id=201)).status_code == 303
        assert client.cookies["sanad_session"] != old
        assert world.login.session(old).revoked_at is not None  # type: ignore[union-attr]
        assert client.get("/api/me").status_code == 200


@pytest.mark.parametrize("bad", ["missing", "mismatch", "other_pre", "origin", "no_origin", "host"])
def test_exchange_csrf_and_same_origin(enrollment: LoginWorld, bad: str) -> None:
    world = enrollment
    path = world.login_path()
    with world.client() as client:
        page = client.get(path)
        nonce = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert nonce
        fields = {} if bad == "missing" else {"csrf": "wrong" if bad == "mismatch" else nonce[1]}
        if bad == "other_pre":
            with world.client() as other:
                other.get(path)
                client.cookies.clear()
                client.cookies.set("sanad_pre", other.cookies["sanad_pre"])
        headers = {"Origin": "https://foreign.example" if bad == "origin" else ORIGIN}
        if bad == "no_origin":
            headers = {}
        if bad == "host":
            headers["Host"] = "foreign.example"
        assert client.post(path, data=fields, headers=headers).status_code == 403
        exchange = world.login.load(
            world.login.scope, "doctor_login", keys.digest(path.rsplit("/", 1)[-1]), LoginExchange
        )
        assert exchange is not None and exchange.state == "issued"


@pytest.mark.parametrize("path", ["/api/me", "/a", "/api/patient/me", "/pp"])
@pytest.mark.parametrize("cookie", ["", "forged", "A" * 43])
def test_forged_or_missing_session_discloses_nothing(
    enrollment: LoginWorld, path: str, cookie: str
) -> None:
    with enrollment.client() as client:
        if cookie:
            client.cookies.set("sanad_session", cookie)
        response = client.get(path)
        assert response.status_code == 401
        assert "Synthetic" not in response.text and enrollment.doctor.id not in response.text


def test_unknown_expired_get_is_same_neutral_page(enrollment: LoginWorld) -> None:
    path = enrollment.login_path()
    with enrollment.client() as client:
        responses = [client.get(path), client.get("/d/" + "A" * 43)]
        enrollment.clock.advance(timedelta(minutes=10))
        responses.append(client.get(path))

    def neutral(text: str) -> str:
        return re.sub(r'(action|value)="[^"]+"', r'\1="opaque"', text)

    assert len({neutral(r.text) for r in responses}) == 1
    for response in responses:
        assert response.status_code == 200
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert "script" not in response.text and "src=" not in response.text
        assert response.headers["set-cookie"].endswith(
            "; HttpOnly; Max-Age=600; Path=/; SameSite=lax; Secure"
        )


def test_reissue_revokes_old_link_and_unsent_payload(enrollment: LoginWorld) -> None:
    world = enrollment
    first = world.login_path()
    assert (
        world.login.issue(IssueDoctorLogin(command_id=uuid4().hex, actor=world.owner)).status
        == "accepted"
    )
    with world.client() as client:
        assert browser_login(client, first).status_code == 403
    old = world.login.load(
        world.login.scope, "doctor_login", keys.digest(first.rsplit("/", 1)[-1]), LoginExchange
    )
    assert old and old.state == "revoked"


def test_two_concurrent_consumptions_have_one_winner(enrollment: LoginWorld) -> None:
    world = enrollment
    raw = world.login_path().rsplit("/", 1)[-1]
    pres = [world.login.pre_session(), world.login.pre_session()]

    def post(i: int) -> str:
        pre = pres[i]
        assert pre is not None
        return world.login.exchange(
            "doctor", raw, pre.cookie.get_secret_value(), pre.csrf.get_secret_value()
        ).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(post, (0, 1))) == ["already_used", "consumed"]


def test_suspension_between_issue_and_exchange(enrollment: LoginWorld) -> None:
    path = enrollment.login_path()
    suspend(enrollment)
    with enrollment.client() as client:
        assert browser_login(client, path).status_code == 403
    assert (
        enrollment.login.issue(
            IssueDoctorLogin(command_id=uuid4().hex, actor=enrollment.actor(APPLICANT))
        ).status
        == "forbidden"
    )


def test_login_paths_and_payload_reprs_do_not_log_credentials(
    enrollment: LoginWorld, caplog: pytest.LogCaptureFixture
) -> None:
    world = enrollment
    caplog.set_level(logging.INFO)
    patient = world.bound()
    invitation = world.invite(world.stub())
    paths = [
        world.login_path(),
        world.login_path(PATIENT, id=201),
        invitation.qr_payload.get_secret_value().removeprefix(ORIGIN),
    ]
    with world.client() as client:
        for path in paths:
            response = client.get(path)
            logging.getLogger("uvicorn.access").info(
                "GET %s HTTP/1.1 %s", path, response.status_code
            )
    for path in paths:
        raw = path.rsplit("/", 1)[-1]
        assert raw not in caplog.text
        assert all(raw not in r.getMessage() and raw not in repr(r.args) for r in caplog.records)
        assert raw not in repr(world.intents()) and raw not in repr(world.transport.calls)
    assert "/d/<redacted>" in caplog.text and "/pl/<redacted>" in caplog.text
    assert patient.display_name not in caplog.text
    assert invitation.token.get_secret_value() not in repr(invitation)


def test_token_generator_random_256_bits_and_hashed() -> None:
    import base64

    values = [issue_token() for _ in range(20)]
    assert len({t.hash for t in values}) == 20
    for token in values:
        raw = token.secret.get_secret_value()
        assert len(base64.urlsafe_b64decode(raw + "=")) == 32
        assert token.hash == keys.digest(raw)
        assert raw not in repr(token) and raw not in token.model_dump_json()
