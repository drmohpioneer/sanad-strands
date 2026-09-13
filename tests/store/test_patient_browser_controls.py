"""18d: patient controls exercise the shared store on memory and opt-in Local."""

from collections.abc import Iterator

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate

from sanad.store._base import StoreBase
from sanad.store.records import CommitRequest, CommitResult, WebSession
from store import evidence_fixtures as evidence
from store.account_fixtures import PATIENT
from store.login_fixtures import ORIGIN, browser_login
from store.test_browser_upload import UploadWorld, mount


@pytest.fixture
def browser(store: StoreBase, clock: FakeClock) -> Iterator[UploadWorld]:
    world = evidence.world(store, clock)
    world.concierge.model_factory = lambda registry, role: ScriptedModel(
        candidate(
            {"reply": "Please ask your doctor.", "kind": "cannot_answer", "needs_doctor": True}
        )
    )
    with world.client() as client:
        assert browser_login(client, world.login_path(PATIENT)).status_code == 303
        session = world.login.session(client.cookies["sanad_session"])
        assert session
        yield mount(world, client, WebSession.model_validate(session.model_dump()))


def headers(browser: UploadWorld) -> dict[str, str]:
    return {"origin": ORIGIN, "x-csrf-token": browser.client.cookies["sanad_csrf"]}


def test_message_replay_conflict_and_web_delivery(browser: UploadWorld) -> None:
    transport = browser.world.transport
    before = len(transport.calls)
    data = {"text": "what is my plan", "command_id": "message1"}
    result = browser.client.post("/api/patient/messages", json=data, headers=headers(browser))
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "accepted"
    assert len(transport.calls) == before
    history = browser.client.get("/api/patient/conversation").json()["items"]
    assert any(r.get("text") == data["text"] for r in history)
    assert any(r["direction"] == "outbound" and r.get("text") for r in history)
    count = len(browser.world.store.patient_receipts(browser.world.patient_scope)[0])
    assert (
        browser.client.post(
            "/api/patient/messages", json=data, headers=headers(browser)
        ).status_code
        == 200
    )
    assert len(browser.world.store.patient_receipts(browser.world.patient_scope)[0]) == count
    assert (
        browser.client.post(
            "/api/patient/messages", json=data | {"text": "different"}, headers=headers(browser)
        ).status_code
        == 409
    )


def test_stop_quiet_and_two_step_resume_keep_session(browser: UploadWorld) -> None:
    for data in ({"reminders": "stop"}, {"quiet_hours": ["22:00", "07:00"]}):
        response = browser.client.post(
            "/api/patient/preferences",
            json=data | {"command_id": next(iter(data)).replace("_", "-")},
            headers=headers(browser),
        )
        assert response.status_code == 200 and response.json()["status"] == "accepted", (
            response.text
        )
        assert browser.client.get("/api/patient/me").status_code == 200
    prefs = browser.client.get("/api/patient/preferences").json()
    assert prefs["reminders"] == "paused" and prefs["quiet_hours"] == ["22:00", "07:00"]
    response = browser.client.post(
        "/api/patient/preferences",
        json={"reminders": "resume", "command_id": "resume"},
        headers=headers(browser),
    )
    token = response.json()["confirmation_token"]
    assert token and not browser.world.profile.routine_contact_enabled
    response = browser.client.post(
        "/api/patient/preferences/confirm",
        json={"token": token, "command_id": "confirm"},
        headers=headers(browser),
    )
    assert response.status_code == 200 and response.json()["status"] == "accepted", response.text
    assert browser.world.profile.routine_contact_enabled
    assert browser.client.get("/api/patient/me").status_code == 200
    version = browser.world.profile.version
    browser.client.post(
        "/api/patient/preferences/confirm",
        json={"token": token, "command_id": "confirm2"},
        headers=headers(browser),
    )
    assert browser.world.profile.version == version


@pytest.mark.parametrize("action", ["message", "stop", "quiet", "resume"])
def test_mapped_telegram_business_equivalence(
    store: StoreBase, clock: FakeClock, action: str
) -> None:
    from copy import deepcopy
    from typing import Any, cast

    from sanad.store._base import Write
    from sanad.store.memory import MemoryStore
    from store.concierge_fixtures import PatientWorld

    seed = MemoryStore(clock=clock)
    initial = evidence.world(seed, clock)
    if action == "resume":
        initial.send("stop reminders")
    with initial.client() as client:
        assert browser_login(client, initial.login_path(PATIENT)).status_code == 303
        cookies = dict(client.cookies)
    baseline = deepcopy(seed._items)
    outcomes: list[dict[str, Any]] = []
    for channel, target in (("web", store), ("telegram", MemoryStore(clock=clock))):
        for item in baseline.values():
            if item["SK"].startswith(("RECEIPT#", "CONVERSATION#")):
                continue
            assert target._atomic([Write(deepcopy(item), None)], [])
        world = cast(PatientWorld, PatientWorld.create(target, clock))
        world.patient_scope = initial.patient_scope
        world.next_message = 3000
        before = {r.id for r in world.rows("audit_event")}
        text = {
            "message": "/plan",
            "stop": "stop reminders",
            "quiet": "quiet hours 22:00 07:00",
            "resume": "resume reminders",
        }[action]
        if channel == "telegram":
            _, intent = world.send(text)
            if action == "resume":
                world.press(intent)
        else:
            with world.client() as client:
                client.cookies.update(cookies)
                csrf = {"origin": ORIGIN, "x-csrf-token": client.cookies["sanad_csrf"]}
                path = (
                    "/api/patient/messages" if action == "message" else "/api/patient/preferences"
                )
                body: dict[str, Any] = (
                    {"text": text}
                    if action == "message"
                    else {"quiet_hours": ["22:00", "07:00"]}
                    if action == "quiet"
                    else {"reminders": action}
                )
                response = client.post(
                    path, json=body | {"command_id": "same-action"}, headers=csrf
                )
                assert response.status_code == 200 and response.json()["status"] == "accepted"
                if action == "resume":
                    assert not world.profile.routine_contact_enabled
                    result = client.post(
                        "/api/patient/preferences/confirm",
                        json={
                            "token": response.json()["confirmation_token"],
                            "command_id": "confirm-action",
                        },
                        headers=csrf,
                    )
                    assert result.status_code == 200 and result.json()["status"] == "accepted"
        patient = world.rows("patient")[0].body
        profile = world.profile
        outcomes.append(
            {
                "patient": {
                    k: patient[k]
                    for k in ("contact_status", "resume_at", "consent_version", "delivery_epoch")
                },
                "consent": [r.body for r in world.rows("consent")],
                "binding": [r.body for r in world.rows("patient_binding")],
                "profile": profile.model_dump(
                    include={
                        "binding_active",
                        "consent_active",
                        "consent_version",
                        "routine_contact_enabled",
                        "routine_paused_until",
                        "delivery_epoch",
                    }
                ),
                "reviews": sorted(
                    (
                        str(r.body["review_kind"]),
                        str(r.body["state"]),
                        str(r.body["review_at"]),
                        str(r.body["source_version"]),
                    )
                    for r in world.rows("review")
                ),
                "events": sorted(
                    str(r.body["event_type"])
                    for r in world.rows("audit_event")
                    if r.id not in before
                ),
                "replies": sorted(
                    (i.template_id, str((i.payload or {}).get("text")))
                    for i in world.patient_intents()
                    if i.template_id and i.template_id.startswith("patient_")
                ),
            }
        )
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize(
    "state,expected",
    [
        ("unmatched", "needs_doctor_review"),
        ("candidate", "needs_doctor_review"),
        ("accepted_pending_identity", "needs_doctor_review"),
        ("accepted", "accepted"),
        ("detached", "not_used"),
        ("rejected", "rejected"),
    ],
)
def test_upload_evidence_state_projection(browser: UploadWorld, state: str, expected: str) -> None:
    from sanad.store.records import Evidence

    evidence.mission(browser.world)
    evidence.providers(browser.world)
    evidence.upload(browser.world)
    original = evidence.current(browser.world)
    retained = state in {"accepted", "accepted_pending_identity"}
    changed = Evidence.model_validate(
        original.model_dump()
        | {
            "association_state": state,
            "accepted_by": original.accepted_by if retained else None,
            "accepted_at": original.accepted_at if retained else None,
            "flags": ("identity_unverifiable",) if state == "accepted_pending_identity" else (),
            "correction_id": "synthetic-correction" if state == "detached" else None,
            "supersedes_evidence_version": 1 if state == "detached" else None,
            "rejection_reason": "private provider failure must never appear"
            if state in {"rejected", "detached"}
            else None,
        }
    )
    browser.world.seed(changed)
    response = browser.client.get("/api/patient/uploads")
    assert response.status_code == 200
    assert response.json()["items"][0]["state"] == expected
    assert "private provider" not in response.text
    if state == "rejected":
        assert response.json()["items"][0]["category"] == "unreadable"


@pytest.mark.parametrize(
    "reason,category",
    [
        ("too_large", "too_large"),
        ("dimensions_exceeded", "too_large"),
        ("not_a_document", "not_a_document"),
        ("not_document", "not_a_document"),
        ("unsupported", "unsupported"),
        ("unsupported_type", "unsupported"),
        ("unsupported_parameter", "unsupported"),
        ("content_type_mismatch", "unsupported"),
        ("invalid_image", "unreadable"),
        ("conversion_failed", "unreadable"),
        ("secret provider message", "unreadable"),
        (None, "unreadable"),
    ],
)
def test_redacted_rejection_mapping(reason: str | None, category: str) -> None:
    from sanad.media.upload import rejection_category

    assert rejection_category(reason) == category


def test_upload_received_processing_and_media_failure(browser: UploadWorld) -> None:
    from providers.fixtures import png

    from sanad.channels.telegram.router import route_receipt
    from sanad.store.records import MediaWork, from_record, to_record

    response = browser.client.post("/api/patient/uploads", content=png(), headers=browser.headers())
    assert response.status_code == 202
    assert browser.client.get("/api/patient/uploads").json()["items"][0]["state"] == "received"
    stage = browser.stages()[0]
    route_receipt(
        browser.world.runtime, to_record(stage.receipt, stage.scope).scoped_key(stage.scope)
    )
    assert browser.client.get("/api/patient/uploads").json()["items"][0]["state"] == "processing"
    work = from_record(browser.world.rows("media_work")[0], MediaWork)
    browser.world.seed(
        work.model_copy(
            update={
                "state": "needs_attention",
                "last_error": "too_large",
                "resend_intent_id": "synthetic-resend",
                "review_obligation_id": "synthetic-review",
            }
        )
    )
    item = browser.client.get("/api/patient/uploads").json()["items"][0]
    assert item["state"] == "rejected" and item["category"] == "too_large"


def test_rejected_danger_caption_is_a_message_not_an_upload(browser: UploadWorld) -> None:
    caption = "عندي ألم شديد في الصدر"
    response = browser.client.post(
        "/api/patient/uploads", content=b"invalid", headers=browser.headers(caption)
    )
    assert response.status_code == 400 and response.json()["category"] == "unsupported"
    assert browser.client.get("/api/patient/uploads").json()["items"] == []
    items = browser.client.get("/api/patient/conversation").json()["items"]
    assert any(
        i["text"] == caption and i["upload"] is None for i in items if i["direction"] == "inbound"
    )
    assert len(browser.world.rows("incident")) == 1


def test_500_messages_cursor_and_scope_isolation(browser: UploadWorld) -> None:
    from datetime import timedelta

    from sanad.domain import PatientScope
    from sanad.store import keys
    from sanad.store.records import InboundReceipt, OperationalClock

    now = browser.world.clock()
    for n in range(500):
        key = f"synthetic-history-{n}"
        at = now - timedelta(seconds=500 - n)
        receipt = InboundReceipt(
            id=keys.inbound("telegram", keys.digest(key)).pk,
            scope=browser.world.patient_scope,
            transport="telegram",
            transport_key=key,
            source_subject=PATIENT,
            source_chat=PATIENT,
            channel="telegram",
            kind="text",
            payload={"text": f"synthetic message {n}"},
            received_at=at,
            created_at=at,
            updated_at=at,
            work_clock=OperationalClock(next_action_at=at, work_lane="ingress"),
        )
        browser.world.seed(receipt)
    foreign = receipt.model_copy(
        update={
            "scope": PatientScope(doctor_id="foreign-doctor", patient_id="foreign-patient"),
            "id": keys.inbound("telegram", keys.digest("foreign")).pk,
            "transport_key": "foreign",
            "payload": {"text": "foreign secret"},
        }
    )
    browser.world.seed(foreign)
    path = "/api/patient/conversation"
    seen: list[str] = []
    while True:
        response = browser.client.get(path)
        assert response.status_code == 200 and "foreign secret" not in response.text
        data = response.json()
        assert len(data["items"]) <= 50
        times = [(i["at"], i["id"]) for i in data["items"]]
        assert times == sorted(times)
        seen.extend(
            i["id"] for i in data["items"] if str(i.get("text")).startswith("synthetic message")
        )
        if not data["cursor"]:
            break
        path = "/api/patient/conversation?cursor=" + data["cursor"]
    assert len(seen) == len(set(seen)) == 500
    assert browser.client.get("/api/patient/conversation?cursor=bad").status_code == 400


@pytest.mark.parametrize("path", ["conversation", "uploads", "preferences"])
def test_read_routes_require_patient_session(browser: UploadWorld, path: str) -> None:
    browser.client.cookies.clear()
    assert browser.client.get("/api/patient/" + path).status_code == 401


def test_command_reservation_race_and_expired_session(browser: UploadWorld) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta

    store = browser.world.store
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda digest: store.reserve_browser_command(browser.session, "race", digest),
                ("first", "second"),
            )
        )
    assert sorted(outcomes) == ["conflict", "created"]
    browser.world.clock.advance(timedelta(days=1))
    assert (
        browser.client.post(
            "/api/patient/messages",
            json={"text": "/plan", "command_id": "expired"},
            headers=headers(browser),
        ).status_code
        == 401
    )


def test_recovery_worker_selects_web_from_durable_receipt(browser: UploadWorld) -> None:
    from datetime import timedelta

    from sanad.channels.telegram.router import route_receipt
    from sanad.steward.dispatch import Dispatcher
    from sanad.store.records import OutboundIntent, from_record

    world = browser.world
    lease = world.store.acquire_patient(
        world.patient_scope, "synthetic-busy", world.clock(), timedelta(minutes=1)
    )
    assert lease
    response = browser.client.post(
        "/api/patient/messages",
        json={"text": "/plan", "command_id": "recover"},
        headers=headers(browser),
    )
    assert response.status_code == 200 and response.json()["status"] == "received"
    world.store.release_patient(lease)
    receipt = next(
        r
        for r in world.store.patient_receipts(world.patient_scope)[0]
        if r.body.get("transport") == "web-message"
    )
    result = route_receipt(world.runtime, receipt.scoped_key(world.patient_scope))
    assert result.route == "patient"
    dispatcher = Dispatcher(
        world.runtime.steward,
        world.transport,
        settings=world.runtime.settings.identity,
        payload_resolver=world.runtime.patient_payload,
    )
    before = len(world.transport.calls)
    intent = next(
        r
        for r in world.rows("outbound_intent")
        if r.body.get("source_event_ids") == ["patient-turn:" + receipt.id]
    )
    finished = dispatcher.dispatch_one(
        intent.scoped_key(world.patient_scope), "recovered", world.clock()
    )
    assert finished and finished.status == "provider_accepted"
    original = from_record(intent, OutboundIntent)
    assert finished.delivered_text == (original.payload or {})["text"]
    assert len(world.transport.calls) == before


def test_delivered_emergency_exact_and_legacy_history(browser: UploadWorld) -> None:
    from store.account_fixtures import update

    world = browser.world
    assert world.post(update(PATIENT, "عندي ألم شديد في الصدر", 5001)).status_code == 200
    delivered = [i for i in world.patient_intents() if i.template_id == "patient_emergency"]
    assert delivered and delivered[0].status == "provider_accepted"
    intent = delivered[0]
    assert intent.delivered_text in [c.payload.get("text") for c in world.transport.calls]
    items = browser.client.get("/api/patient/conversation").json()["items"]
    assert any(i["id"] == intent.id and i["text"] == intent.delivered_text for i in items)
    doctor_ids = {r.id for r in world.rows("outbound_intent") if r.body["audience"] == "doctor"}
    assert not doctor_ids.intersection(i["id"] for i in items)
    world.seed(intent.model_copy(update={"delivered_text": None}))
    legacy = next(
        i
        for i in browser.client.get("/api/patient/conversation").json()["items"]
        if i["id"] == intent.id
    )
    assert legacy["legacy"] is True and legacy["text"] is None


def test_stale_session_refuses_preference_at_commit(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = browser.world
    original = world.store.commit

    def revoke(request: "CommitRequest") -> "CommitResult":
        if request.command.payload.get("type") == "SetContactPreference":
            current = world.login.session(browser.client.cookies["sanad_session"])
            assert current
            world.seed(
                current.model_copy(
                    update={"revoked_at": world.clock(), "version": current.version + 1}
                )
            )
        return original(request)

    monkeypatch.setattr(world.store, "commit", revoke)
    prior = world.profile.consent_version
    browser.client.post(
        "/api/patient/preferences",
        json={"reminders": "stop", "command_id": "revoked-at-commit"},
        headers=headers(browser),
    )
    assert world.profile.consent_version == prior and world.profile.routine_contact_enabled
    assert browser.client.get("/api/patient/me").status_code == 401


def test_page_has_controls_and_paired_catalog(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.presentation.patient_browser import CATALOG

    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    page = browser.client.get("/pp")
    assert page.status_code == 200
    for id in (
        "patient-message-form",
        "patient-upload-form",
        "patient-quiet-form",
        "patient-confirm",
        "patient-conversation",
    ):
        assert f'id="{id}"' in page.text
    assert 'lang="en"' in page.text
    assert all(set(values) == {"en", "ar"} and all(values.values()) for values in CATALOG.values())
    assert (
        browser.client.post(
            "/api/patient/preferences",
            json={"language": "ar", "command_id": "language"},
            headers=headers(browser),
        ).status_code
        == 422
    )


def test_duplicate_upload_follows_existing_evidence_decision(browser: UploadWorld) -> None:
    evidence.mission(browser.world)
    evidence.providers(browser.world)
    assert evidence.upload(browser.world, id=6100) == "accepted"
    assert evidence.upload(browser.world, id=6101) == "accepted"
    rows = browser.client.get("/api/patient/uploads").json()["items"]
    assert len(rows) == 2 and {r["state"] for r in rows} == {"accepted"}


@pytest.mark.parametrize("tamper", ["command_session", "csrf", "consent"])
def test_session_advancement_has_narrow_write_authority(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    world = browser.world
    original = world.store.commit

    def forged(request: CommitRequest) -> CommitResult:
        if request.command.payload.get("type") == "SetContactPreference":
            if tamper == "command_session":
                request = request.model_copy(
                    update={
                        "command": request.command.model_copy(
                            update={
                                "payload": {
                                    **request.command.payload,
                                    "web_session_id": "foreign-session",
                                }
                            }
                        )
                    }
                )
            else:
                puts = tuple(
                    r.model_copy(
                        update={
                            "body": r.body
                            | (
                                {"csrf_secret_ref": "a" * 64}
                                if tamper == "csrf"
                                else {"consent_version": 999}
                            )
                        }
                    )
                    if r.entity_type == "web_session"
                    else r
                    for r in request.puts
                )
                request = request.model_copy(update={"puts": puts})
        return original(request)

    monkeypatch.setattr(world.store, "commit", forged)
    before = world.profile.consent_version
    browser.client.post(
        "/api/patient/preferences",
        json={"reminders": "stop", "command_id": "forged"},
        headers=headers(browser),
    )
    assert world.profile.consent_version == before and world.profile.routine_contact_enabled
    assert browser.client.get("/api/patient/me").status_code == 200


def test_delivered_text_cannot_be_rewritten_on_completion(browser: UploadWorld) -> None:
    from sanad.store.records import DeliveryResolution, OutboundIntent, from_record, to_record

    world = browser.world
    response = browser.client.post(
        "/api/patient/messages",
        json={"text": "/plan", "command_id": "write-once"},
        headers=headers(browser),
    )
    assert response.status_code == 200
    intent = next(
        i
        for i in world.patient_intents()
        if i.accepted_message_id and i.accepted_message_id.startswith("web:")
    )
    assert intent.delivered_text
    forged = intent.model_copy(
        update={"version": intent.version + 1, "delivered_text": "forged replacement"}
    )
    result = world.store.complete_delivery(
        intent.active_attempt_id or "",
        "provider_accepted",
        intent.accepted_message_id,
        scope=intent.scope,
        resolution=DeliveryResolution(intent=to_record(forged, intent.scope)),
    )
    assert result is None
    row = world.store.get(intent.scope, "outbound_intent", intent.id)
    assert row and from_record(row, OutboundIntent).delivered_text == intent.delivered_text


def test_nine_mib_upload_refused_with_category(browser: UploadWorld) -> None:
    response = browser.client.post(
        "/api/patient/uploads", content=b"x" * (9 * 1024 * 1024), headers=browser.headers()
    )
    assert response.status_code == 413 and response.json()["category"] == "too_large"
    assert browser.client.get("/api/patient/uploads").json()["items"] == []


def test_legacy_outbox_shape_does_not_gain_a_null_field(browser: UploadWorld) -> None:
    from sanad.store.records import to_record

    _, intent = browser.world.send("/plan")
    old = intent.model_copy(update={"delivered_text": None})
    assert "delivered_text" not in to_record(old, old.scope).body


def test_web_danger_response_follows_incident_receipt(browser: UploadWorld) -> None:
    from datetime import timedelta

    world = browser.world
    before = len(world.transport.calls)
    lease = world.store.acquire_patient(
        world.patient_scope, "slow-ordinary-turn", world.clock(), timedelta(minutes=1)
    )
    assert lease
    response = browser.client.post(
        "/api/patient/messages",
        json={"text": "عندي ألم شديد في الصدر", "command_id": "urgent-web"},
        headers=headers(browser),
    )
    world.store.release_patient(lease)
    assert response.status_code == 200
    assert len(world.rows("incident")) == 1
    assert len(world.transport.calls) == before
    emergency = next(i for i in world.patient_intents() if i.template_id == "patient_emergency")
    assert emergency.status == "provider_accepted" and emergency.delivered_text
    assert emergency.accepted_message_id == "web:" + emergency.id
    history = browser.client.get("/api/patient/conversation").json()["items"]
    assert any(i.get("text") == emergency.delivered_text for i in history)
    assert any(
        r.body["audience"] == "doctor" and r.body["status"] == "queued"
        for r in world.rows("outbound_intent")
    )


def test_login_delivery_omits_text_and_projects_credential(browser: UploadWorld) -> None:
    from sanad.store import keys
    from sanad.store.records import to_record

    world = browser.world
    intent = next(i for i in world.patient_intents() if i.template_id == "patient_login_link")
    assert intent.status == "provider_accepted"
    assert intent.accepted_at == world.clock()
    assert intent.accepted_message_id
    assert intent.delivered_text is None
    assert "delivered_text" not in to_record(intent, intent.scope).body
    item = next(
        i
        for i in browser.client.get("/api/patient/conversation").json()["items"]
        if i["id"] == intent.id
    )
    assert item == {
        "id": intent.id,
        "at": keys.instant(intent.accepted_at),
        "direction": "outbound",
        "legacy": False,
        "credential": True,
    }


@pytest.mark.parametrize(
    ("text", "credential"),
    [
        ("https://sanad.example/pl/" + "a" * 43, True),
        ("https://t.me/synthetic_bot?start=" + "b" * 43, True),
        ("https://sourceforge.net/p/name/", False),
        ("https://sanad.example/pl/" + "c" * 42, False),
        ("Your ordinary reply.", False),
    ],
)
def test_stored_history_credential_guard_preserves_row(
    browser: UploadWorld, text: str, credential: bool
) -> None:
    world = browser.world
    _, queued = world.send("/plan")
    accepted = world.dispatch(queued)
    assert accepted.delivered_text
    historical = accepted.model_copy(update={"delivered_text": text})
    world.seed(historical)
    before = world.store.get(historical.scope, "outbound_intent", historical.id)
    item = next(
        i
        for i in browser.client.get("/api/patient/conversation").json()["items"]
        if i["id"] == historical.id
    )
    assert item["legacy"] is False
    if credential:
        assert item["credential"] is True and "text" not in item
    else:
        assert item["text"] == text and "credential" not in item
    assert world.store.get(historical.scope, "outbound_intent", historical.id) == before


@pytest.mark.parametrize(
    ("text", "credential"),
    [
        ("https://sanad.example/pl/" + "a" * 43, True),
        ("https://t.me/synthetic_bot?start=" + "b" * 43, True),
        ("https://sourceforge.net/p/name/", False),
        ("Your ordinary reply.", False),
    ],
)
def test_delivery_text_guard_and_ordinary_history(
    browser: UploadWorld, text: str, credential: bool
) -> None:
    from unittest.mock import patch

    world = browser.world
    with patch.object(world.runtime.dispatcher, "dispatch_one", return_value=None):
        _, queued = world.send("/plan")
    assert queued.status == "queued" and queued.delivered_text is None
    world.seed(queued.model_copy(update={"payload": {"text": text}}))
    accepted = world.dispatch(queued)
    assert accepted.status == "provider_accepted" and accepted.accepted_at == world.clock()
    assert accepted.delivered_text == (None if credential else text)
    assert any(call.payload.get("text") == text for call in world.transport.calls)
    item = next(
        i
        for i in browser.client.get("/api/patient/conversation").json()["items"]
        if i["id"] == accepted.id
    )
    if credential:
        # An unmarked template has no stored text to classify after completion.
        assert item["legacy"] is True and item["text"] is None
    else:
        assert item["legacy"] is False and item["text"] == text
