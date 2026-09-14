"""Forged F04/F06/F07 receipts and their authoritative rows, fully offline."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from harness import FakeClock

from sanad.api import lambda_entry
from sanad.auth.claim import ClaimService
from sanad.channels.transport import CapturedTransport
from sanad.ops.worker import AsyncReceiptInvoker
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.records import CommitRequest, Forbidden
from store.account_fixtures import ADMIN, APPLICANT, PATIENT, update
from store.login_fixtures import ORIGIN, LoginWorld, browser_login
from store.test_admin_boundary import admin_path


def test_configure_enrollment(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        "gemini_api_key": "synthetic",
        "bot-token": "4242:synthetic-token-value",
        "webhook-secret": "synthetic-webhook-secret",
        "tick-secret": "synthetic-tick",
        "admin-telegram-id": ADMIN,
        "public-base-url": ORIGIN,
        "bot-username": "synthetic_bot",
    }

    def client(service: str, **kwargs: Any) -> Any:
        if service == "ssm":
            return SimpleNamespace(
                get_parameters=lambda **kw: {
                    "Parameters": [{"Name": "/test/" + k, "Value": v} for k, v in values.items()]
                }
            )
        assert service in {"dynamodb", "s3", "lambda"}
        return SimpleNamespace()

    for key, value in {
        "SANAD_SSM_PREFIX": "/test/",
        "SANAD_TABLE": "synthetic",
        "SANAD_ENV": "dev",
        "SANAD_BUCKET": "synthetic",
        "AWS_LAMBDA_FUNCTION_NAME": "synthetic",
        "SANAD_CLINIC_CONTACT": "Synthetic clinic contact",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("boto3.client", client)
    monkeypatch.setattr(lambda_entry, "DynamoStore", lambda *a: store)
    monkeypatch.setattr(lambda_entry, "utc_now", clock)
    monkeypatch.setattr("sanad.store._base.utc_now", clock)
    app = lambda_entry.configure("synthetic-6d")
    app.state.telegram.transport.http.close()
    app.state.scribe.rxnorm_client.close()
    transport = CapturedTransport()
    app.state.telegram.transport = transport
    app.state.telegram.dispatcher.transport = transport
    world = LoginWorld(store, clock, app.state.telegram, app, transport)
    world.approve()
    patient = world.stub()
    invitation = world.invite(patient)
    # Runtime async invocation is captured; execute the real receipt route offline.
    from sanad.api.internal import process_event

    # The existing router resolves receipt_submit at construction, so fake Lambda invoke.
    monkeypatch.setattr(
        AsyncReceiptInvoker,
        "__call__",
        lambda self, key: None,
    )
    world.post(update(PATIENT, "/start " + invitation.token.get_secret_value(), 1789171161))
    from sanad.store.records import to_record

    receipt = world.receipt(1789171161)
    process_event(
        app.state.telegram,
        {
            "type": "process_receipt",
            "receipt": to_record(receipt, receipt.scope)
            .scoped_key(receipt.scope)
            .model_dump(mode="json"),
        },
    )
    inv = world.claims.invitation(keys.digest(invitation.token.get_secret_value()))
    assert inv and inv.pending_claim_id
    pending = world.claims.patient_claim(inv.pending_claim_id)
    assert pending
    assert pending.state == "pending"
    assert (
        pending.consent_policy["retention"]
        == "Development environment: synthetic data only, reset at any time."
    )
    assert pending.consent_policy["quiet_hours"] == ["22:00", "08:00"]
    saved_patient = world.claims.patient(world.doctor.id, patient.id)
    assert saved_patient and saved_patient.active_binding_id is None
    with pytest.raises(ValueError, match="Consent policy"):
        ClaimService(world.runtime.accounts, ORIGIN)


def test_digest_both_channels_with_advancing_clock(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = LoginWorld.create(store, clock)
    world.approve()

    def advance() -> Any:
        clock.advance(timedelta(microseconds=17))
        return clock()

    monkeypatch.setattr(world.app.state.scribe.repo, "clock", advance)
    with world.client() as client:
        assert browser_login(client, world.login_path()).status_code == 303
        world.post(update(APPLICANT, "/digest 03:08 each", 1789171159))
        assert world.receipt(1789171159).state == "completed"
        assert (world.doctor.digest_time, world.doctor.digest_packing) == ("03:08", "each")
        pref = client.get("/api/preferences").json()
        result = client.post(
            "/api/preferences",
            json={
                "digest_time": "03:25",
                "digest_packing": "each",
                "expected_version": pref["version"],
                "command_id": "c74323ef-b0eb-4721-81a8-1bbf30cee87e",
            },
            headers={"Origin": ORIGIN, "X-CSRF-Token": client.cookies["sanad_csrf"]},
        )
        assert result.status_code == 200
        assert client.get("/api/preferences").json()["digest_time"] == "03:25"
        world.post(update(APPLICANT, "/digest", 1789171169))
        assert world.receipt(1789171169).state == "completed"
        intents = world.app.state.scribe.repo.store.list_records(
            world.app.state.scribe.repo.intent(world.doctor, "x", {}, "x").scope, "outbound_intent"
        )[0]
        assert any("03:25" in str(r.body) and "each question" in str(r.body) for r in intents)


@pytest.mark.parametrize("setting", ["digest", "language"])
def test_forbidden_digest_completes_with_reply(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    world = LoginWorld.create(store, clock)
    world.approve()
    original = store.commit

    def refuse(request: CommitRequest) -> Any:
        return (
            Forbidden()
            if request.command.payload.get("type") == "Scribe" + setting.title()
            else original(request)
        )

    monkeypatch.setattr(store, "commit", refuse)
    world.post(
        update(APPLICANT, "/digest 03:08 each" if setting == "digest" else "/lang ar", 1789171159)
    )
    assert world.receipt(1789171159).state == "completed"
    assert any(f"{setting} setting was refused" in str(c.payload) for c in world.transport.calls)
    assert world.doctor.digest_time == "20:00"


@pytest.mark.parametrize("dual", [False, True])
def test_logout_revokes_own_role_rows(store: StoreBase, clock: FakeClock, dual: bool) -> None:
    world = LoginWorld.create(store, clock)
    world.approve()
    subject = ADMIN if dual else APPLICANT
    if dual:
        world.approve(ADMIN)
    with world.client() as first, world.client() as second:
        if dual:
            assert browser_login(first, admin_path(world)).status_code == 303
            from sanad.auth.commands import IssueDoctorLogin

            assert (
                world.login.issue(
                    IssueDoctorLogin(command_id="dual-login", actor=world.actor(ADMIN))
                ).status
                == "accepted"
            )
            intent = next(
                i
                for i in world.intents()
                if i.template_id == "doctor_login_link" and i.recipient_subject == ADMIN
            )
            assert intent.payload
            path = str(intent.payload["text"]).splitlines()[-1].removeprefix(ORIGIN)
        else:
            assert browser_login(first, world.login_path(id=201)).status_code == 303
            path = world.login_path()
        assert browser_login(second, path).status_code == 303
        sessions = [world.login.session(c.cookies["sanad_session"]) for c in [first, second]]
        assert all(s and s.revoked_at is None for s in sessions)
        world.post(update(subject, "/logout", 1789171176))
        assert world.receipt(1789171176).state == "completed"
        for c in [first, second]:
            saved = world.login.session(c.cookies["sanad_session"])
            assert saved and saved.revoked_at
            assert c.get("/admin" if saved.role == "admin" else "/a").status_code in {401, 403}
        assert any("Signed out of the dashboard." in str(c.payload) for c in world.transport.calls)


def test_enrollment_smoke_runs_deployed_receipt_path(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from deploy import smoke

    world = LoginWorld.create(store, clock)
    monkeypatch.setattr(smoke, "DynamoStore", lambda *args: store)
    monkeypatch.setattr(smoke, "utc_now", clock)
    monkeypatch.setattr(smoke, "client", lambda *args: object())

    def forward(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tg"
        response = world.post(request.content)
        return httpx.Response(response.status_code, content=response.content)

    with httpx.Client(transport=httpx.MockTransport(forward)) as http:
        result = smoke.enrollment(
            object(),
            http,
            ORIGIN,
            "synthetic",
            {
                "bot-token": "4242:synthetic-token-value",
                "admin-telegram-id": ADMIN,
                "webhook-secret": "synthetic-webhook-secret",
                "clinic-contact": "Synthetic clinic contact",
            },
        )
    assert result == {"invitation_issued": True, "pending_claim": True, "patient_activated": False}


def test_context_refusals_and_correction_listing(store: StoreBase, clock: FakeClock) -> None:
    from sanad.accounts.commands import SuspendDoctor

    world = LoginWorld.create(store, clock)
    world.approve()
    world.post(update(APPLICANT, "/corrections", 1789171165))
    assert any("No patients yet" in str(c.payload) for c in world.transport.calls)
    patient = world.stub()
    world.post(update(APPLICANT, "/corrections", 1789171166))
    assert any(
        patient.id in str(c.payload) and "0 correctable records" in str(c.payload)
        for c in world.transport.calls
    )
    world.post(update(PATIENT, "login", 1789171162))
    assert any(
        "You are not linked to a doctor yet." in str(c.payload) for c in world.transport.calls
    )
    assert (
        world.runtime.accounts.suspend(
            SuspendDoctor(
                command_id="suspend6d",
                actor=world.actor(ADMIN),
                doctor_id=world.doctor.id,
                expected_doctor_version=world.doctor.version,
                reason_code="coverage",
            )
        ).status
        == "accepted"
    )
    world.post(update(APPLICANT, "/login", 1789171163))
    assert any(
        "Your account is suspended. Contact the administrator." in str(c.payload)
        for c in world.transport.calls
    )


def test_inbox_lists_kind_patient_and_due_without_internal_ids(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.concierge.inbox import listing_text
    from sanad.contact.bundle import ENGLISH_KINDS
    from store.contact_fixtures import world as contact_world
    from store.test_contact_outage_bundle import deadline

    world = contact_world(store, clock)
    review = deadline(world, clock)
    text = listing_text(store, [review], "en", clock(), 1, 1)
    assert ENGLISH_KINDS[review.review_kind] in text
    patient = world.claims.patient(world.doctor.id, review.patient_id or "")
    assert patient and patient.display_name in text
    assert "Due " + review.review_at.isoformat() in text
    assert "Source:" not in text
    assert review.id not in text and review.source_id not in text
