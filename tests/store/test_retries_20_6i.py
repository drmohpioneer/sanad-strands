"""Infrastructure failure is deferred, without charging a provider/content attempt."""

from datetime import timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from sanad.media.retrieve import StoredMedia
from sanad.media.source import MediaFailure
from sanad.store._base import StoreBase
from sanad.store.records import MediaWork
from store.conftest import Clock
from store.processing_fixtures import World
from store.test_browser_upload import UploadWorld
from store.test_media_adapters import setup, work
from store.test_patient_browser_controls import browser as browser
from store.test_patient_browser_controls import headers

CODES = (
    "ThrottlingException",
    "ProvisionedThroughputExceededException",
    "InternalServerError",
    "ServiceUnavailable",
)


def busy(code: str = "ThrottlingException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "private synthetic text"}}, "GetItem")


@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize("persistent", [False, True])
def test_request_store_failure_retries_and_classifies(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, code: str, persistent: bool
) -> None:
    import json

    import boto3  # type: ignore[import-untyped]
    from botocore.awsrequest import AWSResponse  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]
    from botocore.retries.bucket import TokenBucket  # type: ignore[import-untyped]

    from sanad.store.dynamodb import DynamoStore

    calls = 0

    class Body:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            yield self.body

    def reply_http(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        failed = persistent or calls == 1
        body: dict[str, Any] = (
            {"__type": code, "message": "private synthetic text"} if failed else {"Items": []}
        )
        return AWSResponse(
            "http://127.0.0.1:9",
            (500 if code in {"InternalServerError", "ServiceUnavailable"} else 400)
            if failed
            else 200,
            {"content-type": "application/x-amz-json-1.0"},
            Body(json.dumps(body).encode()),
        )

    monkeypatch.setattr("botocore.endpoint.time.sleep", lambda _: None)
    monkeypatch.setattr(TokenBucket, "acquire", lambda *args, **kwargs: True)
    client = boto3.client(
        "dynamodb",
        region_name="us-east-1",
        endpoint_url="http://127.0.0.1:9",
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",
        config=Config(
            connect_timeout=2, read_timeout=3, retries={"mode": "adaptive", "total_max_attempts": 4}
        ),
    )
    client.meta.events.register("before-send.dynamodb.Query", reply_http)
    probe = DynamoStore(client, "synthetic")
    monkeypatch.setattr(browser.world.store, "patient_receipts", probe.patient_receipts)
    reply = browser.client.get("/api/patient/conversation")
    assert calls == (4 if persistent else 2)
    assert reply.status_code == (409 if persistent else 200)
    if persistent:
        assert reply.json() == {
            "reason": "store_busy",
            "detail": "The service is busy. Try again in a moment.",
        }
    assert "private synthetic" not in reply.text


@pytest.mark.parametrize("code", CODES)
def test_receipt_persist_classification(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise busy(code)

    monkeypatch.setattr(browser.world.store, "accept_inbound", fail)
    reply = browser.client.post(
        "/api/patient/messages",
        json={"command_id": "busy", "text": "hello"},
        headers=headers(browser),
    )
    assert reply.status_code == 409 and reply.json()["reason"] == "store_busy"


@pytest.mark.parametrize("failure", ["throttle", "contention"])
def test_media_returns_attempt_and_uses_durable_ladder(
    store: StoreBase, clock: Clock, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    world = World.create(store, clock)
    retriever, receipt_id = setup(world)
    original = retriever._commit

    def conflict(media: MediaWork, *args: Any, **kwargs: Any) -> bool:
        if media.version > 1:
            if failure == "throttle":
                raise busy()
            return False
        return original(media, *args, **kwargs)

    monkeypatch.setattr(retriever, "_commit", conflict)
    for delay in (1, 5, 15, 15):
        result = retriever.fetch_media("synthetic-handle", receipt_id=receipt_id)
        assert isinstance(result, MediaFailure) and not result.request_resend
        saved = work(world, receipt_id)
        assert saved.state == "pending" and saved.processing_claim is None
        assert saved.work_clock and saved.work_clock.attempt_count == 0
        assert saved.work_clock.next_action_at == clock() + timedelta(minutes=delay)
        assert not saved.review_obligation_id and not saved.resend_intent_id
        clock.now = saved.work_clock.next_action_at
    monkeypatch.setattr(retriever, "_commit", original)
    assert isinstance(retriever.fetch_media("synthetic-handle", receipt_id=receipt_id), StoredMedia)


def test_failed_deferral_expires_and_recovers_without_charging(
    store: StoreBase, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World.create(store, clock)
    retriever, receipt_id = setup(world)
    original_commit, original_defer = retriever._commit, store.defer_media

    def conflict(media: MediaWork, *args: Any, **kwargs: Any) -> bool:
        return False if media.version > 1 else original_commit(media, *args, **kwargs)

    def fail(*args: Any, **kwargs: Any) -> bool:
        raise busy()

    monkeypatch.setattr(retriever, "_commit", conflict)
    monkeypatch.setattr(store, "defer_media", fail)
    assert isinstance(
        retriever.fetch_media("synthetic-handle", receipt_id=receipt_id), MediaFailure
    )
    saved = work(world, receipt_id)
    assert saved.processing_claim and saved.work_clock and saved.work_clock.attempt_count == 1
    assert saved.processing_claim.expires_at == clock() + timedelta(minutes=10)
    monkeypatch.setattr(retriever, "_commit", original_commit)
    monkeypatch.setattr(store, "defer_media", original_defer)
    clock.now = saved.processing_claim.expires_at
    retriever.sweep()
    saved = work(world, receipt_id)
    assert saved.state == "pending" and saved.work_clock and saved.work_clock.attempt_count == 0
    clock.now = saved.work_clock.next_action_at
    assert isinstance(retriever.fetch_media("synthetic-handle", receipt_id=receipt_id), StoredMedia)


@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize("refusal", [False, True])
def test_worker_busy_releases_durable_receipt(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, code: str, refusal: bool
) -> None:
    from sanad.api.internal import run_receipt
    from sanad.channels.telegram.router import RouteResult
    from sanad.store.records import InboundReceipt, from_record

    w = browser.world
    monkeypatch.setattr(
        "sanad.web.api_patient.route_receipt",
        lambda *a, **k: RouteResult(route="busy", status="queued"),
    )
    assert (
        browser.client.post(
            "/api/patient/messages",
            json={"command_id": "worker-busy", "text": "hello"},
            headers=headers(browser),
        ).status_code
        == 200
    )
    row = next(
        r
        for r in w.store.patient_receipts(w.patient_scope)[0]
        if (from_record(r, InboundReceipt).payload or {}).get("text") == "hello"
    )
    key = row.scoped_key(w.patient_scope)

    def claimed_then_busy(*args: Any, **kwargs: Any) -> Any:
        assert w.store.claim_work(
            key, row.version, "synthetic-worker", w.clock(), timedelta(minutes=10)
        )
        if refusal:
            return RouteResult(route="refused", status="unsupported")
        raise busy(code)

    def refusal_busy(*args: Any, **kwargs: Any) -> None:
        raise busy(code)

    monkeypatch.setattr("sanad.api.internal.complete_refusal", refusal_busy)
    monkeypatch.setattr("sanad.api.internal.route_receipt", claimed_then_busy)
    result = run_receipt(w.runtime, key)
    assert result.status == "store_busy"
    saved = w.store.get(w.patient_scope, "inbound_receipt", row.id)
    assert saved
    receipt = from_record(saved, InboundReceipt)
    assert receipt.state == "pending" and receipt.processing_claim is None
    assert receipt.work_clock and receipt.work_clock.next_action_at == w.clock()


def test_normalization_storage_failure_keeps_attempts_available(
    store: StoreBase, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World.create(store, clock)
    retriever, receipt_id = setup(world)
    original = retriever._put_blob
    calls = 0

    def fail_normalized(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise busy("InternalServerError")
        return original(*args, **kwargs)

    monkeypatch.setattr(retriever, "_put_blob", fail_normalized)
    for delay in (1, 5, 15):
        assert isinstance(
            retriever.fetch_media("synthetic-handle", receipt_id=receipt_id), MediaFailure
        )
        saved = work(world, receipt_id)
        assert saved.stage == "normalize" and saved.state == "pending"
        assert saved.work_clock and saved.work_clock.attempt_count == 0
        assert saved.work_clock.next_action_at == clock() + timedelta(minutes=delay)
        clock.now = saved.work_clock.next_action_at
    monkeypatch.setattr(retriever, "_put_blob", original)
    assert isinstance(retriever.fetch_media("synthetic-handle", receipt_id=receipt_id), StoredMedia)


@pytest.mark.parametrize("code", ["ProvisionedThroughputExceeded", "ThrottlingError"])
def test_transaction_capacity_error_keeps_aws_classification(code: str) -> None:
    from sanad.api.failures import store_busy
    from sanad.store._base import Write
    from sanad.store.dynamodb import DynamoStore

    class Capacity:
        def transact_write_items(self, **kwargs: Any) -> Any:
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [{"Code": code}],
                },
                "TransactWriteItems",
            )

    with pytest.raises(ClientError) as raised:
        DynamoStore(Capacity(), "synthetic")._atomic(
            [Write({"PK": "synthetic", "SK": "synthetic", "version": 1}, None)], []
        )
    assert store_busy(raised.value)
