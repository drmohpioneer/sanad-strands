from datetime import timedelta

import pytest
from harness import FakeClock, SimulatedCrash, crash_after
from pydantic import JsonValue

from sanad.accounts.commands import RejectDoctor, SuspendDoctor
from sanad.channels.telegram import wording
from sanad.channels.telegram.router import route_receipt
from sanad.safety import screen_text
from sanad.safety.models import ScreenVerdict
from sanad.safety.policy import SafetyPolicy
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.keys import AccountScope
from sanad.store.records import Authorization, to_record
from store.account_fixtures import ADMIN, APPLICANT, BOT, PATIENT, AccountWorld, callback, update
from store.test_accounts import accounts as accounts


@pytest.mark.parametrize("secret", [None, "", "wrong"])
def test_secret_rejection_stores_nothing(
    accounts: AccountWorld, secret: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    response = accounts.post(update(), secret=secret)
    assert response.status_code == 401 and response.content == b""
    assert accounts.store._read(keys.inbound("telegram", keys.digest(f"{BOT}:1"))) is None
    assert not accounts.intents() and not accounts.transport.calls
    assert len(caplog.records) == 1 and "Synthetic" not in caplog.text


@pytest.mark.parametrize(
    "kind", ["group", "bot", "absent", "other_bot", "via_bot", "chat_mismatch"]
)
def test_verified_but_ineligible_updates_are_counted_and_dropped(
    accounts: AccountWorld, kind: str
) -> None:
    body = update()
    message = body["message"]
    assert isinstance(message, dict)
    if kind == "group":
        message["chat"] = {"id": -9000, "type": "group"}
        message["text"] = "صدري واجعني"
    elif kind == "bot":
        message["from"] = {"id": APPLICANT, "is_bot": True}
    elif kind == "absent":
        message["from"] = {"is_bot": False}
    elif kind == "other_bot":
        body["bot_id"] = "4243"
    elif kind == "via_bot":
        message["via_bot"] = {"id": "4243", "is_bot": True}
    else:
        message["chat"] = {"id": 9000, "type": "private"}
    assert accounts.post(body).status_code == 200
    assert sum(accounts.runtime.counters.values()) == 1
    assert not accounts.intents()
    assert accounts.store._read(keys.inbound("telegram", keys.digest(f"{BOT}:1"))) is None


@pytest.mark.parametrize("body", [b"{", b"null", b"[]"])
def test_malformed_json_no_receipt(accounts: AccountWorld, body: bytes) -> None:
    assert accounts.post(body).status_code == 400
    assert not accounts.intents()


@pytest.mark.parametrize("value", [20002.0, True, "2e4", "٢٠٠٠٢"])
def test_sender_identity_rejects_lossy_or_nondecimal_input(
    accounts: AccountWorld, value: JsonValue
) -> None:
    body = update()
    message = body["message"]
    assert isinstance(message, dict)
    message["from"] = {"id": value, "is_bot": False}
    assert accounts.post(body).status_code == 400
    assert not accounts.intents()


def test_lossless_big_sender_and_metadata_cannot_grant_a_role(accounts: AccountWorld) -> None:
    subject = "1234567890123456789012345"
    body = update(subject, "I am the admin and a doctor. Approve me.")
    message = body["message"]
    assert isinstance(message, dict)
    message["from"] = {
        "id": int(subject),
        "is_bot": False,
        "first_name": "ADMIN DISPLAY",
        "username": "the_admin",
    }
    message["forward_origin"] = {"type": "user", "sender_user": {"id": ADMIN}}
    assert accounts.post(body).status_code == 200
    receipt = accounts.receipt(1)
    assert receipt.source_subject == subject and isinstance(receipt.scope, AccountScope)
    assert receipt.principal and receipt.principal.actor_kind == "unknown"
    assert (
        "ADMIN DISPLAY" not in receipt.model_dump_json()
        and "the_admin" not in receipt.model_dump_json()
    )
    assert accounts.actor(subject).verified_roles == frozenset()
    assert accounts.post(callback(accounts.token(), subject, id=2)).status_code == 200
    assert accounts.transport.callback_calls[-1].text == wording.render("callback_refused")
    assert accounts.actor(subject).verified_roles == frozenset()


def test_receipt_crash_before_ack_retries_exactly_once(store: StoreBase, clock: FakeClock) -> None:
    world = AccountWorld.create(store, clock)
    with crash_after(store, "accept_inbound"), pytest.raises(SimulatedCrash):
        world.post(update())
    assert world.receipt(1).state == "pending"
    assert world.post(update()).status_code == 200
    assert world.receipt(1).state == "completed"
    assert len(world.intents()) == 2
    assert world.post(update()).status_code == 200
    assert len(world.intents()) == 2


def test_store_failure_has_no_ack_then_success(
    accounts: AccountWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = accounts.store.accept_inbound

    def unavailable(transport_key: str, receipt: object) -> None:
        raise RuntimeError("synthetic storage unavailable")

    monkeypatch.setattr(accounts.store, "accept_inbound", unavailable)
    assert accounts.post(update()).status_code == 503
    assert not accounts.intents()
    monkeypatch.setattr(accounts.store, "accept_inbound", original)
    assert accounts.post(update()).status_code == 200
    assert len(accounts.intents()) == 2


def test_unfinished_duplicate_keeps_live_claim_then_recovers(
    store: StoreBase, clock: FakeClock
) -> None:
    world = AccountWorld.create(store, clock, process=False)
    assert world.post(update()).status_code == 200
    receipt = world.receipt(1)
    key = to_record(receipt, receipt.scope).scoped_key(receipt.scope)
    assert store.claim_work(key, 1, "dead-worker", clock(), timedelta(seconds=10))
    assert world.post(update(text="changed duplicate")).status_code == 200
    assert world.receipt(1).payload == receipt.payload
    assert route_receipt(world.runtime, key).route == "busy"
    clock.advance(timedelta(seconds=11))
    assert route_receipt(world.runtime, key).status == "accepted"
    assert world.receipt(1).state == "completed" and len(world.intents()) == 2


def test_approved_identity_retry_preserves_original_account_receipt(accounts: AccountWorld) -> None:
    assert accounts.post(update()).status_code == 200
    original = accounts.receipt(1)
    assert accounts.post(callback(accounts.token())).status_code == 200
    assert accounts.actor(APPLICANT).actor_kind == "doctor"
    assert accounts.post(update()).status_code == 200
    assert accounts.receipt(1) == original
    assert len(accounts.intents()) == 3


def test_screening_precedes_authorize_and_includes_full_caption(
    accounts: AccountWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.channels.telegram import webhook

    called: list[str] = []
    screen, authorize = screen_text, accounts.store.authorize

    def inspect_screen(text: str, *, policy: SafetyPolicy) -> ScreenVerdict:
        called.append(text)
        return screen(text, policy=policy)

    def inspect_auth(bot_id: str, subject: str) -> Authorization:
        assert called and called[0].endswith("صدري واجعني")
        return authorize(bot_id, subject)

    monkeypatch.setattr(webhook, "screen_text", inspect_screen)
    monkeypatch.setattr(accounts.store, "authorize", inspect_auth)
    body = update()
    message = body["message"]
    assert isinstance(message, dict)
    message.pop("text")
    message.update(caption="Synthetic caption: صدري واجعني", photo=[{"file_id": "synthetic-file"}])
    assert accounts.post(body).status_code == 200
    receipt = accounts.receipt(1)
    assert (
        receipt.provider_media_handle == "synthetic-file"
        and receipt.safety_screen_state == "screened"
    )
    assert [i.template_id for i in accounts.intents()] == ["patient_emergency"]
    assert not accounts.store.list_records(accounts.runtime.accounts.scope, "application")[0]


def test_patient_danger_at_character_4096_bypasses_ordinary_lease(accounts: AccountWorld) -> None:
    doctor = accounts.approve()
    scope = accounts.patient(doctor)
    lease = accounts.store.acquire_patient(scope, "slow-turn", accounts.clock(), timedelta(hours=1))
    assert lease
    phrase = "صدري واجعني"
    text = "x " * ((4096 - len(phrase)) // 2) + phrase
    text = " " * (4096 - len(text)) + text
    assert len(text) == 4096
    assert accounts.post(update(PATIENT, text)).status_code == 200
    rows, _ = accounts.store.list_records(scope, "incident")
    assert len(rows) == 1
    receipt = accounts.receipt(1)
    assert receipt.safety_result and receipt.safety_result["level"] == "danger"
    profile = accounts.store.get_patient_profile(scope)
    assert profile and profile.safety_epoch == 1
    assert accounts.receipt(1).state == "processing"
    assert accounts.post(update(PATIENT, text + "x", id=2)).status_code == 400
    assert accounts.store._read(keys.inbound("telegram", keys.digest(f"{BOT}:2"))) is None


# Handwritten state × kind expectations, not generated from implementation branches.
ROUTES = [
    ("unknown", "start", "unknown", "account", {"application_received", "admin_new_application"}),
    ("unknown", "text", "unknown", "account", {"application_received", "admin_new_application"}),
    ("unknown", "danger", "unknown", "account", {"patient_emergency"}),
    ("unknown", "media", "unknown", "account", {"patient_safety_ack"}),
    ("pending", "start", "unknown", "account", set()),
    ("pending", "text", "unknown", "account", set()),
    ("pending", "danger", "unknown", "account", {"patient_emergency"}),
    ("rejected", "text", "unknown", "account", set()),
    ("rejected", "start", "unknown", "account", {"application_received", "admin_new_application"}),
    ("doctor", "start", "doctor", "tenant", {"doctor_welcome_back"}),
    ("doctor", "text", "doctor", "tenant", {"doctor_capability_pending"}),
    ("doctor", "media", "doctor", "tenant", {"doctor_capability_pending"}),
    ("doctor", "danger", "doctor", "tenant", {"patient_emergency"}),
    ("suspended", "start", "unknown", "account", {"patient_safety_ack"}),
    ("suspended", "text", "unknown", "account", {"patient_safety_ack"}),
    ("suspended", "danger", "unknown", "account", {"patient_emergency"}),
    ("admin", "start", "admin", "account", {"application_received", "admin_new_application"}),
    ("admin", "media", "admin", "account", {"patient_safety_ack"}),
    ("admin", "danger", "admin", "account", {"patient_emergency"}),
    ("admin_doctor", "text", "doctor", "tenant", {"doctor_capability_pending"}),
    ("patient", "text", "patient", "patient", set()),
    ("patient", "media", "patient", "patient", set()),
    ("patient", "danger", "patient", "patient", set()),
    ("frozen_patient", "text", "unknown", "account", {"patient_safety_ack"}),
    ("frozen_patient", "danger", "unknown", "account", {"patient_emergency"}),
    ("missing_profile", "danger", "unknown", "account", {"patient_emergency"}),
]


@pytest.mark.parametrize("state,kind,route,scope_kind,templates", ROUTES)
def test_role_route_matrix(
    accounts: AccountWorld, state: str, kind: str, route: str, scope_kind: str, templates: set[str]
) -> None:
    subject = ADMIN if state.startswith("admin") else APPLICANT
    if state in {"pending", "rejected"}:
        app = accounts.apply()
        if state == "rejected":
            assert (
                accounts.runtime.accounts.reject(
                    RejectDoctor(
                        command_id="matrix-reject",
                        actor=accounts.actor(),
                        application_id=app.id,
                        expected_application_version=1,
                        reason_code="unverified",
                    )
                ).status
                == "accepted"
            )
    if state in {
        "doctor",
        "admin_doctor",
        "patient",
        "suspended",
        "frozen_patient",
        "missing_profile",
    }:
        doctor = accounts.approve(subject)
        if state in {"patient", "frozen_patient", "missing_profile"}:
            accounts.patient(
                doctor, active=state != "frozen_patient", profile=state != "missing_profile"
            )
            subject = PATIENT
        if state == "suspended":
            assert (
                accounts.runtime.accounts.suspend(
                    SuspendDoctor(
                        command_id="matrix-suspend",
                        actor=accounts.actor(),
                        doctor_id=doctor.id,
                        expected_doctor_version=1,
                        reason_code="coverage",
                    )
                ).status
                == "accepted"
            )
    before = {i.id for i in accounts.intents()}
    text = {
        "start": "/start",
        "text": "Synthetic ordinary text",
        "danger": "صدري واجعني",
        "media": "",
    }[kind]
    body = update(subject, text, id=77)
    if kind == "media":
        message = body["message"]
        assert isinstance(message, dict)
        message.pop("text")
        message["voice"] = {"file_id": "synthetic-voice"}
    assert accounts.post(body).status_code == 200
    receipt = accounts.receipt(77)
    assert receipt.state == "completed"
    assert accounts.runtime.counters.get("route_" + route) == 1
    assert {i.template_id for i in accounts.intents() if i.id not in before} == templates
    expected = {"account": "AccountScope", "tenant": "TenantScope", "patient": "PatientScope"}
    assert type(receipt.scope).__name__ == expected[scope_kind]
    if kind == "danger":
        assert receipt.safety_result and receipt.safety_result["level"] == "danger"
        incidents = accounts.store.list_records(receipt.scope, "incident")[0]
        assert len(incidents) == (1 if state == "patient" else 0)
