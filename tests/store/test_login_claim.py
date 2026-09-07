from datetime import timedelta

import pytest
from harness import FakeClock

from sanad.store._base import StoreBase
from store.account_fixtures import PATIENT, callback
from store.login_fixtures import LoginWorld, browser_login


@pytest.fixture
def enrollment(store: StoreBase, clock: FakeClock) -> LoginWorld:
    world = LoginWorld.create(store, clock)
    world.approve()
    return world


def test_full_doctor_claim_patient_browser_journey(enrollment: LoginWorld) -> None:
    world = enrollment
    doctor_path = world.login_path()
    with world.client() as browser:
        result = browser_login(browser, doctor_path)
        assert result.status_code == 303 and result.headers["location"] == "/a"
        assert browser.get("/api/me").json() == {
            "doctor_id": world.doctor.id,
            "name": "Synthetic Doctor",
            "status": "approved",
        }
        assert browser.get("/a").status_code == 200
    patient = world.stub()
    invitation = world.invite(patient)
    with world.client() as browser:
        landing = browser.get(invitation.qr_payload.get_secret_value())
        assert landing.status_code == 200 and "Synthetic Doctor" in landing.text
        assert patient.id not in landing.text and patient.display_name not in landing.text
        assert "https://t.me/synthetic_sanad_bot?start=" in landing.text
    pending = world.claim(invitation)
    assert world.actor(PATIENT).actor_kind == "unknown"
    request = next(i for i in world.intents() if i.template_id == "consent_request")
    assert world.dispatch(request).status == "provider_accepted"
    assert world.store.get_patient_profile(patient.scope).binding_active is False  # type: ignore[union-attr]
    pending = world.consent(pending)
    token = world.action_token(pending.id, "claim_awaiting_doctor")
    assert world.post(callback(token, world.owner.subject, 102)).status_code == 200
    assert world.claims.patient_claim(pending.id).state == "approved"  # type: ignore[union-attr]
    assert world.actor(PATIENT).actor_kind == "patient"
    from sanad.store.records import OutboundIntent, from_record

    rows, _ = world.store.list_records(patient.scope, "outbound_intent")
    confirmed = from_record(rows[0], OutboundIntent)
    assert confirmed.template_id == "binding_confirmed"
    assert confirmed.status == "provider_accepted"
    assert world.dispatch(confirmed).status == "provider_accepted"
    assert len([c for c in world.transport.calls if c.recipient_ref == PATIENT]) == 3
    patient_path = world.login_path(PATIENT, id=201)
    with world.client() as browser:
        result = browser_login(browser, patient_path)
        assert result.status_code == 303 and result.headers["location"] == "/pp"
        data = browser.get("/api/patient/me").json()
        assert data["display_name"] == "Synthetic Patient" and data["consent_version"] == 1
        assert data["plan"] == browser.get("/api/patient/plan").json()
        assert data["plan"]["orders"] == []
        assert browser.get("/pp").status_code == 200


def test_login_expires_at_exact_boundary(enrollment: LoginWorld) -> None:
    path = enrollment.login_path()
    enrollment.clock.advance(timedelta(minutes=10))
    with enrollment.client() as client:
        assert browser_login(client, path).status_code == 403
