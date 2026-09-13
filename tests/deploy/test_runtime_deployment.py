import asyncio
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from domain_fixtures import NOW
from store.account_fixtures import AccountWorld, settings, update
from store.conftest import Clock
from store.conftest import clock as clock
from store.conftest import ddb_server as ddb_server
from store.conftest import pytest_generate_tests as pytest_generate_tests
from store.conftest import store as store

from sanad.api.app import create_app
from sanad.api.internal import process_event
from sanad.channels.transport import CapturedTransport
from sanad.ops.nonce_store import NonceStore, TickVerifier
from sanad.ops.sweep import sweep_due
from sanad.ops.tick_signing import authentic, sign, signed_headers
from sanad.ops.worker import AsyncReceiptInvoker
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.records import to_record

SECRET = "synthetic-tick-secret"
NONCE = "a1" * 32


class FakeInvoker:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, int]:
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise TimeoutError("synthetic provider unavailable")
        return {"StatusCode": 202}


def post(app: Any, path: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
    async def run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://sanad.test"
        ) as http:
            return await http.post(path, content=body, headers=headers)

    return asyncio.run(run())


def test_signing_is_byte_exact_and_not_reencoded() -> None:
    timestamp = str(int(NOW.timestamp()))
    body = b'{ "different": "spacing" }\n'
    expected = hmac.new(
        SECRET.encode(), (timestamp + "." + NONCE + ".").encode() + body, hashlib.sha256
    ).hexdigest()
    assert sign(SECRET, timestamp, NONCE, body) == expected
    headers = signed_headers(SECRET, timestamp, NONCE, body)
    assert authentic(SECRET, headers, body, NOW.timestamp())
    assert not authentic(SECRET, headers, body.strip(), NOW.timestamp())


@pytest.mark.parametrize(
    "change", ["missing", "past", "future", "short_nonce", "bad_ts", "nan", "forged", "body"]
)
def test_tick_authentication_rejects_before_nonce_write(
    store: StoreBase, clock: Clock, change: str
) -> None:
    nonces = NonceStore(store, "test")
    verifier = TickVerifier(SECRET, nonces, clock)
    timestamp = str(int(clock().timestamp()))
    body = b"{}"
    headers = signed_headers(SECRET, timestamp, NONCE, body)
    if change == "missing":
        headers.pop("x-sanad-sig")
    elif change in {"past", "future"}:
        timestamp = str(int(clock().timestamp()) + (-301 if change == "past" else 301))
        headers = signed_headers(SECRET, timestamp, NONCE, body)
    elif change == "short_nonce":
        headers = signed_headers(SECRET, timestamp, "ab", body)
    elif change in {"bad_ts", "nan"}:
        headers["x-sanad-ts"] = "NaN" if change == "nan" else "-"
    elif change == "forged":
        headers["x-sanad-sig"] = "0" * 64
    else:
        body = b"changed"
    assert verifier.verify(headers, body) == 401
    assert nonces.accept(NONCE, int(clock().timestamp()))


def test_nonce_shared_across_instances_replay_expiration_and_cleanup(
    store: StoreBase, clock: Clock
) -> None:
    one, two = NonceStore(store, "dev"), NonceStore(store, "dev")
    now = int(clock().timestamp())
    assert one.accept(NONCE, now)
    assert not two.accept(NONCE, now)
    key = keys.operational("dev", "NONCE", keys.digest(NONCE))
    assert store._read(key)["ttl"] == now + 600  # type: ignore[index]
    assert two.cleanup(now + 599) == 0
    assert two.cleanup(now + 600) == 1
    assert store._read(key) is None
    assert one.accept(NONCE, now + 600)
    assert NonceStore(store, "judge").accept(NONCE, now + 600)


def test_nonce_overlap_has_one_winner_and_stale_cleanup_cannot_remove_refresh(
    store: StoreBase, clock: Clock
) -> None:
    nonces = NonceStore(store, "race")
    now = int(clock().timestamp())
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: nonces.accept(NONCE, now), range(2))) == [False, True]
    key = keys.operational("race", "NONCE", keys.digest(NONCE))
    assert nonces.accept(NONCE, now + 600)
    assert not store._delete_nonce(key, 1)
    assert store._read(key)["ttl"] == now + 1200  # type: ignore[index]
    assert not store._delete_nonce(keys.Key("D#doctor", "PROFILE"), 1)


@pytest.mark.parametrize("failures", [0, 2, 4])
def test_ack_has_pending_receipt_worker_or_sweep_completes(
    store: StoreBase, clock: Clock, failures: int
) -> None:
    fake = FakeInvoker(failures)
    sleeps: list[float] = []
    invoker = AsyncReceiptInvoker(
        fake, "sanad-dev-app", SECRET, clock=lambda: clock().timestamp(), sleep=sleeps.append
    )
    app = create_app(
        store=store,
        clock=clock,
        telegram_settings=settings(),
        transport=CapturedTransport(),
        receipt_submit=invoker,
    )
    world = AccountWorld(store, clock, app.state.telegram, app, app.state.telegram.transport)
    assert world.post(update()).status_code == 200
    receipt = world.receipt(1)
    assert receipt.state == "pending"
    assert len(fake.calls) == min(failures + 1, 4)
    assert sleeps == [0.1 * 2**i for i in range(min(failures, 3))]
    for call in fake.calls:
        assert call["InvocationType"] == "Event" and call["FunctionName"] == "sanad-dev-app"
        assert "text" not in json.loads(call["Payload"])
    if failures < 4:
        event = json.loads(fake.calls[-1]["Payload"])
        result = process_event(world.runtime, event)
        assert result["route"] == "unknown"
    else:
        assert sweep_due(world.runtime, store)["handled"] >= 1
    assert world.receipt(1).state == "completed"
    applications = store.list_records(world.runtime.accounts.scope, "application")[0]
    assert len(applications) == 1
    assert world.post(update()).status_code == 200
    assert world.receipt(1).state == "completed"


def test_worker_crash_after_claim_is_recovered_by_existing_sweep(
    store: StoreBase, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = AccountWorld.create(store, clock, process=False)
    assert world.post(update()).status_code == 200
    receipt = world.receipt(1)
    key = to_record(receipt, receipt.scope).scoped_key(receipt.scope)
    event = {"type": "process_receipt", "receipt": key.model_dump(mode="json")}
    original = world.runtime.accounts.finish_receipt

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic crash after side effects, before receipt completion")

    monkeypatch.setattr(world.runtime.accounts, "finish_receipt", crash)
    with pytest.raises(RuntimeError):
        process_event(world.runtime, event)
    assert world.receipt(1).state == "pending"
    assert world.receipt(1).processing_claim is None
    monkeypatch.setattr(world.runtime.accounts, "finish_receipt", original)
    report = sweep_due(world.runtime, store)
    assert report["handled"] >= 1 and not report["errors"]
    assert world.receipt(1).state == "completed"
    assert len(store.list_records(world.runtime.accounts.scope, "application")[0]) == 1


def test_internal_endpoint_auth_replay_and_worker_no_public_bypass(
    store: StoreBase, clock: Clock
) -> None:
    calls: list[bool] = []

    def sweep() -> dict[str, int]:
        calls.append(True)
        return {"handled": 0}

    verifier = TickVerifier(SECRET, NonceStore(store, "endpoint"), clock)
    app = create_app(
        store=store,
        clock=clock,
        telegram_settings=settings(),
        transport=CapturedTransport(),
        tick_verifier=verifier,
        tick_sweep=sweep,
    )
    headers = signed_headers(SECRET, str(int(clock().timestamp())), NONCE, b"{}")
    assert post(app, "/internal/tick", b"{}", {}).status_code == 401
    accepted = post(app, "/internal/tick", b"{}", headers)
    assert accepted.json() == {"accepted": True, "nonce": NONCE, "sweep": {"handled": 0}}
    assert post(app, "/internal/tick", b"{}", headers).status_code == 409
    assert calls == [True]
    assert (
        post(
            app, "/events", b'{"type":"process_receipt","receipt":{},"headers":{}}', {}
        ).status_code
        == 401
    )
