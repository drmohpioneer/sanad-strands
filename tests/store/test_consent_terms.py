"""Consent offer history, receipt atomicity and invitation recovery on both stores."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from harness import FakeClock, SimulatedCrash

from sanad.accounts.delivery import account_freshness
from sanad.auth.commands import ClaimInvitation, IssueInvitation
from sanad.auth.service import revise
from sanad.channels.telegram import wording
from sanad.presentation import consent_terms
from sanad.scribe.proposal import InvitationWork
from sanad.store import keys
from sanad.store._base import Check, StoreBase, Write
from sanad.store.records import (
    ClaimCallback,
    Consent,
    OutboundIntent,
    PatientClaim,
    from_record,
    record_item,
    to_record,
)
from store.account_fixtures import APPLICANT, PATIENT, callback, update
from store.login_fixtures import CONSENT_POLICY, LoginWorld, browser_login, replace_model
from store.scribe_fixtures import ScribeWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> ScribeWorld:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    return world


def offered(world: LoginWorld) -> PatientClaim:
    return world.claim(world.invite(world.stub()))


def saved(world: LoginWorld, pending: PatientClaim) -> PatientClaim:
    value = world.claims.patient_claim(pending.id)
    assert value is not None
    return value


def offer_intent(world: LoginWorld, pending: PatientClaim) -> OutboundIntent:
    return next(
        i
        for i in world.intents()
        if i.template_id == "consent_request"
        and any(r.id == pending.id and r.version == pending.version for r in i.source_versions)
    )


def token(world: LoginWorld, pending: PatientClaim, index: int) -> str:
    payload = offer_intent(world, pending).payload
    assert payload is not None
    markup = payload["reply_markup"]
    assert isinstance(markup, dict)
    keyboard = markup["inline_keyboard"]
    assert isinstance(keyboard, list)
    buttons = []
    for row in keyboard:
        assert isinstance(row, list)
        buttons.extend(row)
    button = buttons[index]
    assert isinstance(button, dict)
    return str(button["callback_data"])


def tap(world: LoginWorld, raw: str) -> str:
    result = world.claims.callback(keys.digest(raw), world.actor(PATIENT), uuid4().hex)
    assert result is not None
    return result.status


def terms(world: LoginWorld) -> list[OutboundIntent]:
    return [i for i in world.intents() if i.template_id == "consent_terms"]


def test_offer_freezes_exact_render_and_keyboard(world: ScribeWorld) -> None:
    pending = offered(world)
    offer = pending.consent_offers[0]
    short, full = consent_terms.render(
        world.doctor.name, "22:00", "08:00", world.doctor.timezone, "Synthetic clinic contact"
    )
    assert offer.short_text == short and offer.full_text == full
    assert offer.generation == pending.offer_generation == 1
    assert offer.language == "en"
    assert offer.text_version == "consent-terms-2026-09-13-v1"
    payload = offer_intent(world, pending).payload
    assert payload is not None and payload["text"] == short
    markup = payload["reply_markup"]
    assert isinstance(markup, dict)
    keyboard = markup["inline_keyboard"]
    assert isinstance(keyboard, list)
    labels = []
    for row in keyboard:
        assert isinstance(row, list)
        labels.append([b["text"] for b in row if isinstance(b, dict)])
    assert labels == [["Read the full terms"], ["I agree", "I don't agree"]]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_callbacks_refuse_wrong_actor_and_expired_offer(world: ScribeWorld, index: int) -> None:
    pending = offered(world)
    raw = token(world, pending, index)
    result = world.claims.callback(keys.digest(raw), world.actor("50005"), "wrong-actor")
    assert result is not None and result.status == "forbidden"
    assert saved(world, pending) == pending and terms(world) == []
    world.clock.advance(timedelta(days=1))
    assert tap(world, raw) == "forbidden"
    assert saved(world, pending) == pending and terms(world) == []


def test_agreement_rejects_unauthenticated_and_doctor_sessions(world: ScribeWorld) -> None:
    with world.client() as client:
        assert client.get("/api/patient/agreement").status_code == 401
        assert browser_login(client, world.login_path()).status_code == 303
        assert client.get("/api/patient/agreement").status_code == 401


def test_read_uses_frozen_offer_even_when_contact_configuration_disappears(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = offered(world)
    monkeypatch.setattr(wording, "CONSENT_TEXT_VERSION", "synthetic-new-version")
    world.claims.consent_policy = lambda _: None
    assert tap(world, token(world, pending, 0)) == "accepted"
    assert saved(world, pending) == pending
    assert (terms(world)[0].payload or {})["text"] == pending.consent_offers[0].full_text


@pytest.mark.parametrize("accept", [True, False])
def test_read_is_idempotent_and_does_not_consume_decision(world: ScribeWorld, accept: bool) -> None:
    pending = offered(world)
    read, choice = token(world, pending, 0), token(world, pending, 1 if accept else 2)
    assert tap(world, read) == "accepted"
    assert saved(world, pending) == pending
    assert tap(world, read) == "forbidden"
    assert len(terms(world)) == 1
    assert terms(world)[0].payload == {"text": pending.consent_offers[0].full_text}
    assert tap(world, choice) == "accepted"
    result = saved(world, pending)
    assert result.consent_id is not None if accept else result.state == "rejected"
    patient = world.claims.patient(result.doctor_id, result.patient_id)
    assert patient is not None
    rows, _ = world.store.list_records(patient.scope, "consent")
    assert len(rows) == int(accept)
    assert tap(world, choice) == "forbidden"
    reason = account_freshness(world.store, terms(world)[0], world.clock(), world.store._identity)
    assert reason == (None if accept else "claim_closed")


@pytest.mark.parametrize("after", [False, True])
def test_read_receipt_and_outbox_commit_together(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch, after: bool
) -> None:
    pending = offered(world)
    raw = token(world, pending, 0)
    original = world.store._atomic
    captured: list[Write] = []

    def crash(writes: list[Write], checks: list[Check]) -> bool:
        reading = any(
            isinstance(w.item.get("body"), str)
            and json.loads(w.item["body"]).get("template_id") == "consent_terms"
            for w in writes
        )
        if reading:
            captured.extend(writes)
            if not after:
                raise SimulatedCrash()
        result = original(writes, checks)
        if reading and result:
            raise SimulatedCrash()
        return result

    monkeypatch.setattr(world.store, "_atomic", crash)
    with pytest.raises(SimulatedCrash):
        world.post(callback(raw, PATIENT, 110))
    monkeypatch.setattr(world.store, "_atomic", original)
    assert captured
    assert any(w.item.get("entity_type") == "inbound_receipt" for w in captured)
    assert any(w.item.get("entity_type") == "outbound_intent" for w in captured)
    assert len(terms(world)) == int(after)
    assert (world.receipt(110).state == "completed") == after
    assert saved(world, pending) == pending
    if after:
        world.post(callback(raw, PATIENT, 111))
        assert len(terms(world)) == 1


@pytest.mark.parametrize("change", ["version", "doctor", "contact", "quiet"])
def test_changed_offer_refreshes_and_old_buttons_point_to_latest(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    pending = offered(world)
    raw = token(world, pending, 1)
    assert tap(world, token(world, pending, 0)) == "accepted"
    if change == "version":
        monkeypatch.setattr(wording, "CONSENT_TEXT_VERSION", "synthetic-next-version")
    elif change == "doctor":
        replace_model(world, revise(world.doctor, world.clock(), name="Changed Doctor"))
    else:
        field = (
            {"clinic_contact": "Changed clinic"}
            if change == "contact"
            else {"quiet_hours": ("21:00", "07:00")}
        )
        policy = CONSENT_POLICY.model_copy(update=field)
        world.claims.consent_policy = lambda _: policy
    assert tap(world, raw) == "accepted"  # The new offer, never a consent.
    refreshed = saved(world, pending)
    assert refreshed.consent_id is None and refreshed.offer_generation == 2
    assert refreshed.consent_offers[:-1] == pending.consent_offers
    assert refreshed.consent_offers[-1].digest != pending.consent_offers[-1].digest
    assert (
        account_freshness(world.store, terms(world)[0], world.clock(), world.store._identity)
        == "offer_superseded"
    )
    world.post(callback(raw, PATIENT, 112))
    assert world.transport.callback_calls[-1].text == consent_terms.OFFER_SUPERSEDED
    assert tap(world, token(world, pending, 0)) == "forbidden"
    assert tap(world, token(world, refreshed, 1)) == "accepted"
    accepted = saved(world, pending)
    patient = world.claims.patient(accepted.doctor_id, accepted.patient_id)
    assert patient is not None
    consent = world.claims.load(patient.scope, "consent", accepted.consent_id or "", Consent)
    assert consent and consent.policy_digest == refreshed.consent_offers[-1].digest
    assert consent.offer_generation == 2 and consent.offer_claim_id == pending.id


def test_missing_contact_refuses_invitation_and_claim_without_orphans(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    patient = world.stub()
    invitation = world.invite(patient)
    before = world.claims.invitation(keys.digest(invitation.token.get_secret_value()))
    original = world.store._atomic
    writes_seen: list[Write] = []

    def capture(writes: list[Write], checks: list[Check]) -> bool:
        writes_seen.extend(writes)
        return original(writes, checks)

    monkeypatch.setattr(world.store, "_atomic", capture)
    world.claims.consent_policy = lambda _: None
    result = world.claims.issue_invitation(
        IssueInvitation(command_id="missing", actor=world.owner, patient_id=patient.id)
    )
    assert result.status == "forbidden"
    assert world.claims.invitation(keys.digest(invitation.token.get_secret_value())) == before
    result = world.claims.claim_invitation(
        ClaimInvitation(
            command_id="missing-claim",
            actor=world.actor(PATIENT),
            private_chat_id=PATIENT,
            invitation_hash=keys.digest(invitation.token.get_secret_value()),
        )
    )
    assert result.status == "forbidden"
    assert writes_seen == []
    monkeypatch.setattr(world.store, "_atomic", original)
    world.post(update(APPLICANT, "/qr Synthetic Patient", 119))
    replies = world.cards()
    assert any(
        (i.payload or {}).get("text") == consent_terms.INVITE_CONTACT_MISSING for i in replies
    )
    world.claims.consent_policy = lambda _: CONSENT_POLICY
    assert world.claim(invitation, id=120).offer_generation == 1


@pytest.mark.parametrize("changed", [False, True])
def test_temporary_contact_refusal_preserves_token_and_action_identity(
    world: ScribeWorld, changed: bool
) -> None:
    pending = offered(world)
    raw = token(world, pending, 1)
    world.claims.consent_policy = lambda _: None
    world.post(callback(raw, PATIENT, 121))
    assert saved(world, pending) == pending
    button = world.claims.load(
        world.claims.scope, "claim_callback", keys.digest(raw), ClaimCallback
    )
    assert button and button.consumed_at is None
    assert any(
        (i.payload or {}).get("text") == consent_terms.CONTACT_UNAVAILABLE for i in world.intents()
    )
    policy = (
        CONSENT_POLICY.model_copy(update={"clinic_contact": "Changed"})
        if changed
        else CONSENT_POLICY
    )
    world.claims.consent_policy = lambda _: policy
    assert tap(world, raw) == "accepted"
    result = saved(world, pending)
    assert result.consent_id is None if changed else result.consent_id is not None
    assert result.offer_generation == (2 if changed else 1)


def test_accepted_offer_language_and_text_survive_configuration_changes(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = offered(world)
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    replace_model(world, revise(world.doctor, world.clock(), language="ar"))
    assert tap(world, token(world, pending, 1)) == "accepted"
    accepted = saved(world, pending)
    monkeypatch.setattr(wording, "CONSENT_TEXT_VERSION", "synthetic-later-version")
    assert world.claims.confirm(world.confirm_command(accepted)).status == "accepted"
    path = world.login_path(PATIENT)
    with world.client() as client:
        assert browser_login(client, path).status_code == 303
        response = client.get("/api/patient/agreement")
        assert response.status_code == 200
        assert response.json()["text"] == pending.consent_offers[0].full_text
        assert response.json()["version"] == pending.consent_offers[0].text_version
        assert client.get("/api/patient/agreement?patient_id=foreign").status_code == 400


@pytest.mark.parametrize("actions", [(1, 2), (0, 1)])
def test_concurrent_actions_keep_one_consent_outcome(
    world: ScribeWorld, actions: tuple[int, int]
) -> None:
    pending = offered(world)
    buttons = [token(world, pending, i) for i in actions]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda raw: tap(world, raw), buttons))
    assert "accepted" in results
    result = saved(world, pending)
    patient = world.claims.patient(result.doctor_id, result.patient_id)
    assert patient is not None
    consents = world.store.list_records(patient.scope, "consent")[0]
    assert len(consents) == (1 if result.consent_id else 0)
    assert (result.consent_id is not None) != (result.state == "rejected")
    assert len(terms(world)) <= 1


@pytest.mark.parametrize("close", ["confirm", "decline", "expire", "reissue"])
def test_queued_terms_revalidate_claim_lifecycle(world: ScribeWorld, close: str) -> None:
    pending = offered(world)
    assert tap(world, token(world, pending, 0)) == "accepted"
    queued = terms(world)[0]
    reason = "claim_closed"
    if close == "confirm":
        assert tap(world, token(world, pending, 1)) == "accepted"
        assert (
            world.claims.confirm(world.confirm_command(saved(world, pending))).status == "accepted"
        )
        reason = "claim_confirmed"
    elif close == "decline":
        assert tap(world, token(world, pending, 2)) == "accepted"
    elif close == "expire":
        world.clock.advance(timedelta(days=1))
        assert world.claims.expire(pending.invitation_id).status == "accepted"
        reason = "expired"
    else:
        patient = world.claims.patient(pending.doctor_id, pending.patient_id)
        assert patient is not None
        world.invite(patient)
    assert account_freshness(world.store, queued, world.clock(), world.store._identity) == reason
    assert (
        account_freshness(world.store, queued, queued.expires_at, world.store._identity)
        == "expired"
    )


def test_legacy_pending_offer_refreshes_and_legacy_accepted_stays_readable(
    world: ScribeWorld,
) -> None:
    pending = offered(world)
    raw = token(world, pending, 1)
    # Persist an actual pre-release shape, with no offer fields or callback generation.
    row = to_record(pending, pending.scope)
    item = record_item(row)
    body = json.loads(item["body"])
    body.pop("consent_offers")
    body.pop("offer_generation")
    item["body"] = json.dumps(body)
    assert world.store._atomic([Write(item, row.version)], [])
    callback_row = world.store.get(pending.scope, "claim_callback", keys.digest(raw))
    assert callback_row is not None
    item = record_item(callback_row)
    body = json.loads(item["body"])
    body.pop("offer_generation")
    item["body"] = json.dumps(body)
    assert world.store._atomic([Write(item, callback_row.version)], [])
    assert tap(world, raw) == "accepted"
    refreshed = saved(world, pending)
    assert refreshed.consent_id is None and refreshed.offer_generation == 1
    assert tap(world, token(world, refreshed, 1)) == "accepted"
    accepted = saved(world, pending)
    assert world.claims.confirm(world.confirm_command(accepted)).status == "accepted"
    patient = world.claims.patient(accepted.doctor_id, accepted.patient_id)
    assert patient is not None
    consent_row = world.store.get(patient.scope, "consent", accepted.consent_id or "")
    assert consent_row is not None
    item = record_item(consent_row)
    body = json.loads(item["body"])
    for name in ("offer_claim_id", "offer_generation", "language"):
        body.pop(name)
    body["policy_text_version"] = "consent-draft-2026-09-06-v1"
    item["body"] = json.dumps(body)
    assert world.store._atomic([Write(item, consent_row.version)], [])
    with world.client() as client:
        assert browser_login(client, world.login_path(PATIENT)).status_code == 303
        data = client.get("/api/patient/agreement").json()
        assert data["text"] is None
        assert data["version"] == "consent-draft-2026-09-06-v1"


@pytest.mark.parametrize("manual", [False, True])
def test_background_contact_defers_and_recovers_without_duplicate_invitation(
    world: ScribeWorld, manual: bool
) -> None:
    world.claims.consent_policy = lambda _: None
    world.post(update(APPLICANT, "/new Synthetic Patient", 130))
    world.tap(label="✅ Confirm", id=131)
    works = world.store.list_records(world.doctor.scope, "scribe_invitation_work")[0]
    assert len(works) == 1
    work = from_record(works[0], InvitationWork)
    before_messages = len(world.cards())
    world.scribe.invitation_work(work)
    row = world.store.get(work.scope, work.entity_type, work.id)
    assert row is not None
    deferred = from_record(row, InvitationWork)
    assert deferred.status == "pending" and deferred.id == work.id
    assert deferred.work_clock is not None
    assert deferred.work_clock.next_action_at == world.clock() + timedelta(minutes=15)
    assert deferred.work_clock.last_error_code == "clinic_contact_missing"
    assert work.work_clock is not None
    assert deferred.work_clock.attempt_count == work.work_clock.attempt_count + 1
    patient = world.claims.patient(world.doctor.id, work.patient_id)
    assert patient is not None and patient.invitation_id is None
    assert len(world.cards()) == before_messages
    world.claims.consent_policy = lambda _: CONSENT_POLICY
    if manual:
        patient = world.claims.patient(world.doctor.id, work.patient_id)
        assert patient is not None
        world.invite(patient)  # Same fake-clock instant: identity, never timestamp.
    world.clock.advance(timedelta(minutes=15))
    world.scribe.invitation_work(deferred)
    row = world.store.get(work.scope, work.entity_type, work.id)
    assert row is not None
    complete = from_record(row, InvitationWork)
    assert complete.status == ("superseded" if manual else "issued")
    world.scribe.invitation_work(deferred)  # Lost completion replay.
    patient = world.claims.patient(world.doctor.id, work.patient_id)
    assert patient is not None and patient.invitation_generation == 1
    assert patient.invitation_id is not None
    invitation = world.claims.invitation(patient.invitation_id)
    assert invitation is not None and invitation.state == "issued"
