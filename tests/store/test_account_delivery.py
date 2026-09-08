from datetime import timedelta

import pytest
from domain_fixtures import POLICY
from pydantic import JsonValue

from sanad.accounts.commands import ReinstateDoctor, RejectDoctor, SuspendDoctor
from sanad.channels.telegram import wording
from sanad.channels.transport import CapturedTransport, SendOutcome
from sanad.domain import VersionRef
from sanad.steward.apply import make_intent
from sanad.steward.dispatch import Dispatcher, freshness
from sanad.steward.types import StewardPolicy
from sanad.store.records import DoctorAuthority, IdentityConfig, OutboundIntent, from_record
from store.account_fixtures import APPLICANT, BOT, PATIENT, AccountWorld, callback, update
from store.test_accounts import accounts as accounts


def test_approval_http_to_patient_danger_and_captured_delivery(accounts: AccountWorld) -> None:
    assert accounts.post(update()).status_code == 200
    for intent in accounts.intents():
        assert accounts.dispatch(intent).status == "provider_accepted"
    admin_send = next(call for call in accounts.transport.calls if "reply_markup" in call.payload)
    assert admin_send.payload["text"] == wording.render(
        "admin_new_application", "en", name="", specialty="", city=""
    )
    token = accounts.token()
    assert accounts.post(callback(token)).status_code == 200
    assert accounts.transport.callback_calls[-1].text == ""
    approved = next(i for i in accounts.intents() if i.template_id == "doctor_approved")
    assert accounts.dispatch(approved).status == "provider_accepted"
    actor = accounts.actor(APPLICANT)
    assert actor.doctor_id and actor.verified_roles == frozenset({"doctor"})
    doctor = accounts.runtime.accounts.doctor(actor.doctor_id)
    assert doctor
    scope = accounts.patient(doctor)
    assert accounts.post(update(PATIENT, "صدري واجعني", id=3)).status_code == 200
    rows, _ = accounts.store.list_records(scope, "outbound_intent")
    assert len(rows) == 2
    for row in rows:
        assert accounts.dispatch(from_record(row, OutboundIntent)).status == "provider_accepted"
    danger = accounts.transport.calls[-2:]
    assert {call.recipient_ref for call in danger} == {APPLICANT, PATIENT}
    assert all(isinstance(call.payload["text"], str) for call in danger)
    assert len(accounts.store.list_records(scope, "incident")[0]) == 1
    assert accounts.receipt(3).state == "completed"


def test_suspension_suppresses_routine_but_both_old_and_new_danger_survive(
    accounts: AccountWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Preserve this test's pre-send suspension window; 11b tests cover inline sends.
    monkeypatch.setattr("sanad.api.internal.dispatch_inline", lambda *args: None)
    doctor = accounts.approve()
    scope = accounts.patient(doctor)
    profile = accounts.store.get_patient_profile(scope)
    row = accounts.store.get(doctor.scope, "doctor_authority", doctor.id)
    assert profile and row
    authority = from_record(row, DoctorAuthority)
    prompt = make_intent(
        scope,
        "routine",
        (),
        "routine_prompt",
        "synthetic-plan",
        accounts.clock(),
        StewardPolicy(POLICY),
        authority,
        profile,
        audience="patient",
        slot="synthetic-slot",
    )
    accounts.put_patient(doctor, prompt)
    assert accounts.post(update(PATIENT, "صدري واجعني")).status_code == 200
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
    result = accounts.dispatch(prompt)
    assert result.status == "suppressed" and result.suppression_reason == "doctor_coverage"
    rows, _ = accounts.store.list_records(scope, "outbound_intent")
    danger = next(
        from_record(r, OutboundIntent) for r in rows if r.body["notification_purpose"] == "DANGER"
    )
    assert accounts.dispatch(danger).status == "provider_accepted"
    assert accounts.post(update(PATIENT, "صدري واجعني", id=2)).status_code == 200
    rows, _ = accounts.store.list_records(scope, "outbound_intent")
    new_danger = next(
        from_record(r, OutboundIntent)
        for r in rows
        if r.body["notification_purpose"] == "DANGER" and r.id != danger.id
    )
    assert accounts.dispatch(new_danger).status == "provider_accepted"
    notice = next(i for i in accounts.intents() if i.template_id == "doctor_suspended_notice")
    assert accounts.dispatch(notice).status == "provider_accepted"
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
    count = len(accounts.transport.calls)
    assert accounts.dispatch(result).status == "suppressed"
    assert len(accounts.transport.calls) == count


@pytest.mark.parametrize(
    "change,reason",
    [
        ("admin", "admin_changed"),
        ("recipient", "recipient_authority"),
        ("version", "source_version"),
        ("decided", "application_state"),
        ("expired", "expired"),
    ],
)
def test_account_freshness_suppresses_with_specific_reason(
    accounts: AccountWorld, change: str, reason: str
) -> None:
    app = accounts.apply()
    intent = next(i for i in accounts.intents() if i.template_id == "admin_new_application")
    config = accounts.runtime.settings.identity
    if change == "admin":
        config = IdentityConfig(bot_id=BOT, admin_user_id="10009")
    elif change == "recipient":
        intent = OutboundIntent.model_validate(
            intent.model_dump() | {"audience": "applicant", "recipient_ref": "other"}
        )
    elif change == "version":
        intent = OutboundIntent.model_validate(
            intent.model_dump()
            | {
                "source_versions": (VersionRef(entity_type="application", id=app.id, version=2),),
            }
        )
    elif change == "decided":
        assert (
            accounts.runtime.accounts.reject(
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
    else:
        accounts.clock.advance(timedelta(days=8))
    assert freshness(accounts.store, intent, accounts.clock(), settings=config) == reason
    if change in {"admin", "decided", "expired"}:
        accounts.runtime.dispatcher.settings = config
        result = accounts.dispatch(intent)
        assert result.status == "suppressed" and result.suppression_reason == reason
        assert not accounts.transport.calls


def test_stale_doctor_epoch_suppresses_unsent_decision(accounts: AccountWorld) -> None:
    doctor = accounts.approve()
    intent = next(i for i in accounts.intents() if i.template_id == "doctor_approved")
    accounts.runtime.accounts.suspend(
        SuspendDoctor(
            command_id="suspend",
            actor=accounts.actor(),
            doctor_id=doctor.id,
            expected_doctor_version=1,
            reason_code="coverage",
        )
    )
    assert accounts.dispatch(intent).suppression_reason == "recipient_authority"
    accounts.runtime.accounts.reinstate(
        ReinstateDoctor(
            command_id="reinstate",
            actor=accounts.actor(),
            doctor_id=doctor.id,
            expected_doctor_version=2,
        )
    )
    assert accounts.dispatch(intent).status == "suppressed"
    assert not accounts.transport.calls


def test_account_rate_limit_uses_provider_delay_and_bounded_failure_review(
    accounts: AccountWorld,
) -> None:
    accounts.apply()
    intent = next(i for i in accounts.intents() if i.audience == "applicant")
    transport = CapturedTransport(
        (
            SendOutcome(status="failed", code="rate_limit", retryable=True, retry_after_seconds=7),
            SendOutcome(status="failed", code="blocked", retryable=False),
        )
    )
    accounts.runtime.dispatcher = Dispatcher(
        accounts.runtime.steward, transport, settings=accounts.runtime.settings.identity
    )
    retry = accounts.dispatch(intent)
    assert retry.status == "queued" and retry.work_clock
    assert retry.work_clock.next_action_at == accounts.clock() + timedelta(seconds=7)
    accounts.clock.advance(timedelta(seconds=6))
    assert accounts.dispatch(retry).status == "queued" and len(transport.calls) == 1
    accounts.clock.advance(timedelta(seconds=1))
    failed = accounts.dispatch(retry)
    assert failed.status == "failed" and failed.work_clock is None and failed.review_obligation_id
    issue = accounts.store.get(intent.scope, "operational_issue", failed.review_obligation_id)
    assert issue and issue.body["kind"] == "delivery_failure"


def test_account_send_outliving_lease_is_uncertain(accounts: AccountWorld) -> None:
    accounts.apply()
    intent = next(i for i in accounts.intents() if i.audience == "applicant")

    class SlowTransport(CapturedTransport):
        def send(self, recipient_ref: str, payload: dict[str, JsonValue]) -> SendOutcome:
            result = super().send(recipient_ref, payload)
            accounts.clock.advance(timedelta(seconds=intent.delivery_lease_seconds + 1))
            return result

    transport = SlowTransport()
    accounts.runtime.dispatcher = Dispatcher(
        accounts.runtime.steward, transport, settings=accounts.runtime.settings.identity
    )
    outcome = accounts.dispatch(intent)
    assert outcome.status == "uncertain" and outcome.accepted_message_id is None
    outcome = accounts.dispatch(outcome)
    assert outcome.status == "uncertain" and outcome.review_obligation_id
    assert len(transport.calls) == 1


def test_real_adapter_with_mock_http_persists_provider_message_id(accounts: AccountWorld) -> None:
    import httpx

    from sanad.channels.telegram.transport import TelegramTransport
    from sanad.store.records import DeliveryAttempt

    accounts.apply()
    intent = next(i for i in accounts.intents() if i.audience == "applicant")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 12345}})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        accounts.runtime.dispatcher = Dispatcher(
            accounts.runtime.steward,
            TelegramTransport(accounts.runtime.settings, client),
            settings=accounts.runtime.settings.identity,
        )
        result = accounts.dispatch(intent)
    assert result.status == "provider_accepted" and result.accepted_message_id == "12345"
    assert result.active_attempt_id
    row = accounts.store.get(intent.scope, "delivery_attempt", result.active_attempt_id)
    assert row and from_record(row, DeliveryAttempt).provider_message_id == "12345"


def test_admin_doctor_recipient_retains_both_roles_and_epoch(accounts: AccountWorld) -> None:
    from store.account_fixtures import ADMIN

    accounts.approve(ADMIN)
    app = accounts.apply()
    intent = next(
        i
        for i in accounts.intents()
        if i.template_id == "admin_new_application" and i.source_versions[0].id == app.id
    )
    assert intent.recipient_auth_epoch_seen == 1
    assert accounts.dispatch(intent).status == "provider_accepted"


def test_revoked_identity_suppresses_suspension_notice(accounts: AccountWorld) -> None:
    from sanad.accounts.records import SubjectBinding
    from sanad.store._base import Write
    from sanad.store.records import record_item, to_record

    doctor = accounts.approve()
    accounts.runtime.accounts.suspend(
        SuspendDoctor(
            command_id="suspend",
            actor=accounts.actor(),
            doctor_id=doctor.id,
            expected_doctor_version=1,
            reason_code="coverage",
        )
    )
    auth = accounts.store.authorize(BOT, APPLICANT)
    assert auth.binding
    revoked = SubjectBinding.model_validate(
        auth.binding.model_dump()
        | {
            "version": auth.binding.version + 1,
            "status": "revoked",
        }
    )
    assert accounts.store._atomic(
        [Write(record_item(to_record(revoked, revoked.scope)), auth.binding.version)], []
    )
    notice = next(i for i in accounts.intents() if i.template_id == "doctor_suspended_notice")
    assert accounts.dispatch(notice).suppression_reason == "recipient_authority"
    assert not accounts.transport.calls
