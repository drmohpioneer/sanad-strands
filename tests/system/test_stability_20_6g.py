"""Request source attribution, durable ACKs and concurrent browser/tick parity."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from providers.fixtures import png
from store.account_fixtures import PATIENT, update
from store.login_fixtures import (  # type: ignore[attr-defined]
    ORIGIN,
    TestClient,
    browser_login,
)
from store.test_browser_upload import UploadWorld
from store.test_patient_browser_controls import headers

from sanad.api.app import create_app
from sanad.api.failures import REASONS
from sanad.store._base import Check, Write
from sanad.store.records import AuthorizationUnavailable, InboundAccept
from sanad.store.retry import BACKOFF
from system.test_walkthrough_20_6e import browser as browser
from system.test_walkthrough_20_6e import clock as clock


def conflict() -> ClientError:
    return ClientError({"Error": {"Code": "TransactionConflictException"}}, "GetItem")


def test_parallel_dashboard_poll_and_tick(browser: UploadWorld) -> None:
    """Real parallel HTTP dependency/touch/projection calls and tick CAS writes, both stores."""
    w = browser.world
    with w.client() as doctor:
        assert browser_login(doctor, w.login_path(w.owner.subject, id=8010)).status_code == 303
        doctor_cookie = doctor.cookies["sanad_session"]
        patient_cookie = browser.client.cookies["sanad_session"]
        urls = [
            (doctor, p)
            for p in (
                "/api/me",
                "/api/patients",
                "/api/browser/reviews",
                "/api/questions",
                "/api/preferences",
                "/api/patients/" + w.patient_scope.patient_id,
            )
        ] + [
            (browser.client, p)
            for p in (
                "/api/patient/me",
                "/api/patient/plan",
                "/api/patient/conversation",
                "/api/patient/uploads",
                "/api/patient/preferences",
            )
        ]
        barrier = Barrier(len(urls) + 1)
        writes = []

        def tick() -> None:
            barrier.wait(timeout=15)
            # Tick-owned patient fencing mutates the profile while page calls pin authority.
            for _ in range(12):
                lease = w.store.acquire_patient(
                    w.patient_scope,
                    "simulated-tick",
                    w.clock(),
                    w.runtime.accounts.policy.operations.lease_ttl,
                )
                if lease:
                    writes.append(lease.generation)
                    w.store.release_patient(lease)

        def page(client: TestClient, path: str) -> list[int]:
            barrier.wait(timeout=15)
            return [client.get(path).status_code for _ in range(3)]

        with ThreadPoolExecutor(max_workers=len(urls) + 1) as pool:
            tick_run = pool.submit(tick)
            reads = [pool.submit(page, c, p) for c, p in urls]
            results = [future.result(timeout=120) for future in reads]
            tick_run.result(timeout=120)
        assert writes
        assert all(code == 200 for codes in results for code in codes), results
        for cookie in (doctor_cookie, patient_cookie):
            saved = w.login.session(cookie)
            assert saved and saved.revoked_at is None


@pytest.mark.parametrize("method", ["authorize", "web_session_snapshot"])
@pytest.mark.parametrize("exhausted", [False, True])
def test_authority_sources_retry_exact_backoff(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, method: str, exhausted: bool
) -> None:
    w = browser.world
    original = w.store._atomic
    from sanad.store.records import to_record

    target = to_record(browser.session, browser.session.scope).key
    remaining = 4 if exhausted else 3
    delays: list[float] = []
    monkeypatch.setattr("sanad.store.retry.sleep", delays.append)

    def busy(writes: list[Write], checks: list[Check]) -> bool:
        nonlocal remaining
        if (
            not writes
            and remaining
            and (
                checks[0].key == target
                if method == "web_session_snapshot"
                else checks[0].key.pk.startswith("SUBJECT#")
            )
        ):
            remaining -= 1
            return False
        return original(writes, checks)

    monkeypatch.setattr(w.store, "_atomic", busy)

    def read() -> object:
        if method == "authorize":
            return w.store.authorize(w.runtime.settings.bot_id, PATIENT)
        return w.store.web_session_snapshot(browser.session)

    if exhausted:
        with pytest.raises(AuthorizationUnavailable):
            read()
    else:
        assert read()
    assert delays == list(BACKOFF) and remaining == 0
    session = w.login.session(browser.client.cookies["sanad_session"])
    assert session and not session.revoked_at


@pytest.mark.parametrize(
    "path", ["/api/patient/messages", "/api/patient/preferences", "/api/preferences"]
)
@pytest.mark.parametrize("exhausted", [False, True])
def test_receipt_sources_retry_then_truthful_reply(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, path: str, exhausted: bool
) -> None:
    w = browser.world
    original = w.store.accept_inbound
    delays: list[float] = []
    attempts = 0
    monkeypatch.setattr("sanad.web.receipts.sleep", delays.append)

    def refuse(*args: Any, **kwargs: Any) -> InboundAccept:
        nonlocal attempts
        attempts += 1
        if attempts <= (4 if exhausted else 3):
            return InboundAccept(status="conflict")
        return original(*args, **kwargs)

    body: dict[str, Any] = (
        {"command_id": "receipt6g", "text": "/plan"}
        if path.endswith("messages")
        else {"command_id": "receipt6g", "reminders": "stop"}
    )
    with w.client() as doctor:
        client = browser.client
        request_headers = headers(browser)
        if path == "/api/preferences":
            assert browser_login(doctor, w.login_path(w.owner.subject, id=8020)).status_code == 303
            client = doctor
            request_headers = {
                "Origin": ORIGIN,
                "X-CSRF-Token": doctor.cookies["sanad_csrf"],
            }
            body = {
                "command_id": "receipt6g",
                "language": "en",
                "expected_version": w.doctor.version,
            }
        monkeypatch.setattr(w.store, "accept_inbound", refuse)
        response = client.post(path, json=body, headers=request_headers)
    assert attempts == 4 and delays == list(BACKOFF)
    if exhausted:
        assert response.status_code == 409 and response.json() == {
            "reason": "receipt_persist_failed",
            "detail": "Not received. Try again.",
        }
    else:
        assert response.status_code == 200


@pytest.mark.parametrize("exhausted", [False, True])
def test_browser_reservation_retries_concurrent_poll(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, exhausted: bool
) -> None:
    w = browser.world
    original = w.store._atomic
    attempts = 0
    delays: list[float] = []
    monkeypatch.setattr("sanad.store.retry.sleep", delays.append)

    def busy(writes: list[Write], checks: list[Check]) -> bool:
        nonlocal attempts
        if writes and writes[0].key.sk.startswith("WEB_COMMAND#"):
            attempts += 1
            if attempts <= (4 if exhausted else 3):
                # A real poll touches the session after its authority was read.
                assert w.login.require(browser.client.cookies["sanad_session"], "patient")
        return original(writes, checks)

    monkeypatch.setattr(w.store, "_atomic", busy)
    response = browser.client.post(
        "/api/patient/preferences",
        json={"quiet_hours": ["22:00", "07:00"], "command_id": "poll-reservation"},
        headers=headers(browser),
    )
    assert attempts == 4 and delays == list(BACKOFF)
    if exhausted:
        assert response.status_code == 409
        assert response.json()["reason"] == "authorization_unavailable"
    else:
        assert response.status_code == 200
        assert browser.client.get("/api/patient/preferences").json()["quiet_hours"] == [
            "22:00",
            "07:00",
        ]
    session = w.login.session(browser.client.cookies["sanad_session"])
    assert session and session.revoked_at is None


@pytest.mark.parametrize("path", ["/a", "/admin", "/api/patient/uploads"])
def test_session_guards_are_handled(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    def busy(*args: Any, **kwargs: Any) -> None:
        raise AuthorizationUnavailable("private subject")

    monkeypatch.setattr(browser.world.login, "require", busy)
    response = (
        browser.client.post(path, content=png(), headers=browser.headers())
        if path.endswith("uploads")
        else browser.client.get(path)
    )
    assert response.status_code == 409
    assert response.json()["reason"] == "authorization_unavailable"
    assert "private subject" not in response.text


def test_pre_session_failure_source(browser: UploadWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser.world.login, "pre_session", lambda: None)
    result = browser.client.get("/d/opaque")
    assert result.status_code == 409 and result.json()["reason"] == "authorization_unavailable"


def test_upload_storage_exception_source(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("private file")

    monkeypatch.setattr(browser.ingress, "stage", fail)
    response = browser.client.post("/api/patient/uploads", content=png(), headers=browser.headers())
    assert response.status_code == 409 and response.json()["reason"] == "media_storage_unavailable"


@pytest.mark.parametrize("mode", ["exception", "conflict", "missing_readback"])
def test_telegram_ack_requires_verified_receipt(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    w = browser.world
    original, get = w.store.accept_inbound, w.store.get

    def accept(*args: Any, **kwargs: Any) -> InboundAccept:
        if mode == "exception":
            raise RuntimeError("private content")
        if mode == "conflict":
            return InboundAccept(status="conflict")
        return original(*args, **kwargs)

    def read(scope: Any, kind: str, id: str) -> Any:
        return (
            None
            if mode == "missing_readback" and kind == "inbound_receipt"
            else get(scope, kind, id)
        )

    monkeypatch.setattr(w.store, "accept_inbound", accept)
    monkeypatch.setattr(w.store, "get", read)
    response = w.post(update(PATIENT, "/plan", 8030))
    assert response.status_code == 409
    assert response.json()["reason"] == (
        "ingress_exception" if mode == "exception" else "ingress_conflict"
    )
    monkeypatch.setattr(w.store, "accept_inbound", original)
    monkeypatch.setattr(w.store, "get", get)
    assert w.post(update(PATIENT, "/plan", 8030)).status_code == 200
    receipt = w.receipt(8030)
    assert receipt.state == "completed"
    version = receipt.version
    assert w.post(update(PATIENT, "/plan", 8030)).status_code == 200
    assert w.receipt(8030).version == version


def test_missing_runtime_source() -> None:
    with TestClient(create_app()) as client:
        response = client.post("/tg", json={})
    assert response.status_code == 409 and response.json()["reason"] == "ingress_exception"


@pytest.mark.parametrize("exhausted", [False, True])
def test_read_only_page_retries_store_conflict(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, exhausted: bool
) -> None:
    store = browser.world.store
    original = store.patient_receipts
    attempts = 0

    async def sleep(delay: float) -> None:
        delays.append(delay)

    delays: list[float] = []
    monkeypatch.setattr("sanad.api.failures.asyncio.sleep", sleep)

    def read(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts <= (4 if exhausted else 3):
            raise conflict()
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "patient_receipts", read)
    response = browser.client.get("/api/patient/conversation")
    assert response.status_code == (409 if exhausted else 200)
    if exhausted:
        assert response.json()["reason"] == "ingress_conflict"
    assert attempts == 4 and delays == list(BACKOFF)


@pytest.mark.parametrize("reason", list(REASONS))
def test_fixed_reasons_logged_without_payload(
    reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    from sanad.api.failures import RequestFailure

    app = create_app()

    @app.get("/api/patient/private-id")
    def fail() -> None:
        if reason == "unhandled":
            raise RuntimeError("private clinical text")
        raise RequestFailure(reason)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.get("/api/patient/private-id")
    assert response.status_code == (
        503 if reason == "configuration" else 500 if reason == "unhandled" else 409
    )
    assert response.json() == {"reason": reason, "detail": REASONS[reason]}
    assert f"request_failed reason={reason} route_family=patient" in caplog.text
    assert "private-id" not in caplog.text and "private clinical text" not in caplog.text


@pytest.mark.parametrize("missing", [False, True])
def test_record_media_storage_sources(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    from store import evidence_fixtures as f

    w = browser.world
    f.mission(w)
    f.providers(w)
    f.upload(w)
    e = f.current(w)

    class BrokenStorage:
        def get(self, *args: Any) -> bytes:
            raise RuntimeError("private storage path")

    if not missing:
        w.app.state.media_store = BrokenStorage()
    with w.client() as doctor:
        assert browser_login(doctor, w.login_path(w.owner.subject, id=8040)).status_code == 303
        response = doctor.get(f"/api/patients/{w.patient_scope.patient_id}/media/{e.media_id}")
    assert response.status_code == 409 and response.json()["reason"] == "media_storage_unavailable"


def test_session_pin_failure_source(browser: UploadWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    from sanad.store.records import Forbidden

    assert browser.session.consent_version is not None
    snapshot = browser.session.model_copy(
        update={"consent_version": browser.session.consent_version + 1}
    )
    monkeypatch.setattr(browser.world.store, "web_session_snapshot", lambda session: snapshot)
    monkeypatch.setattr(browser.world.login, "commit", lambda *a, **k: Forbidden())
    delays: list[float] = []
    monkeypatch.setattr("sanad.store.retry.sleep", delays.append)
    result = browser.client.get("/api/patient/preferences")
    assert result.status_code == 409 and result.json()["reason"] == "authorization_unavailable"
    assert delays == list(BACKOFF)


@pytest.mark.parametrize("source", ["session", "http"])
def test_legacy_exception_mappers_never_leak_unlabelled_5xx(
    browser: UploadWorld, source: str
) -> None:
    from fastapi import HTTPException

    from sanad.web.security import SessionRefused

    @browser.world.app.get("/api/mapper-test")
    def fail() -> None:
        if source == "session":
            raise SessionRefused("session_busy", 503)
        raise HTTPException(503)

    response = browser.client.get("/api/mapper-test")
    assert response.status_code == (409 if source == "session" else 500)
    assert response.json()["reason"] == (
        "authorization_unavailable" if source == "session" else "unhandled"
    )


def test_answer_busy_defers_then_delivers_exact_answer(browser: UploadWorld) -> None:
    from datetime import timedelta

    from store.executors_15_fixtures import doctor

    from sanad.api.internal import process_event
    from sanad.store.records import to_record

    w = browser.world
    w.send("can I double the dose?")
    doctor(w, "/questions", 8050)
    lease = w.store.acquire_patient(
        w.patient_scope, "parallel-turn", w.clock(), timedelta(minutes=5)
    )
    assert lease
    answer = "Please follow the recorded plan."
    assert w.post(update(w.owner.subject, "/answer 1 " + answer, 8051)).status_code == 200
    receipt = w.receipt(8051)
    assert receipt.state == "pending" and receipt.processing_claim is None
    assert any(r.body["state"] == "open" for r in w.rows("mission") if r.body["kind"] == "QUESTION")
    w.store.release_patient(lease)
    key = to_record(receipt, receipt.scope).scoped_key(receipt.scope)
    process_event(w.runtime, {"type": "process_receipt", "receipt": key.model_dump(mode="json")})
    assert w.receipt(8051).state == "completed"
    assert any(answer in str(i.payload) for i in w.patient_intents())
    assert any(
        r.body["state"] == "fulfilled" for r in w.rows("mission") if r.body["kind"] == "QUESTION"
    )
