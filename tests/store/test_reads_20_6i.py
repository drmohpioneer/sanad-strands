"""6i: rendered polling and worker access must never issue DynamoDB Scan."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from playwright.sync_api import Route, expect, sync_playwright

from sanad.ops.sweep import sweep_due
from sanad.store.dynamodb import DynamoStore
from store.account_fixtures import APPLICANT
from store.login_fixtures import ORIGIN, browser_login
from store.test_browser_upload import UploadWorld
from store.test_patient_browser_controls import browser as browser
from store.test_patient_browser_controls import headers


class CountClient:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.calls: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        fn = getattr(self.client, name)

        def call(**kwargs: Any) -> Any:
            self.calls[name] += 1
            return fn(**kwargs)

        return call


def test_rendered_journeys_zero_scan(browser: UploadWorld, loopback_network: None) -> None:
    store = browser.world.store
    if not isinstance(store, DynamoStore):
        return
    wrapped = CountClient(store._client)
    store._client = wrapped
    client = browser.client
    doctor_path = browser.world.login_path(APPLICANT, id=990)
    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True)
        page = chromium.new_page()
        page.set_default_timeout(10000)

        def handle(route: Route) -> None:
            request = route.request
            response = client.request(
                request.method,
                request.url,
                content=request.post_data_buffer,
                headers={k: v for k, v in request.headers.items() if k not in {"cookie", "host"}},
            )
            route.fulfill(
                status=response.status_code, headers=dict(response.headers), body=response.content
            )

        page.route("**/*", handle)
        page.goto(ORIGIN + "/pp")
        expect(page.locator("#patient-stop")).to_have_attribute("aria-checked", "true")
        expect(page.locator("#patient-conversation")).not_to_be_empty()
        for _ in range(2):
            page.locator("#refresh").click()
            page.wait_for_timeout(200)
        result = client.post(
            "/api/patient/messages",
            json={"command_id": "scan-rail", "text": "what is my plan"},
            headers=headers(browser),
        )
        assert result.status_code == 200
        with ThreadPoolExecutor() as pool:
            assert (
                pool.submit(
                    sweep_due, browser.world.runtime, store, elapsed_clock=lambda: 0
                ).result()["errors"]
                == []
            )
        assert browser_login(client, doctor_path).status_code == 303
        for path in (
            "/a",
            "/a/patients/" + browser.world.patient_scope.patient_id,
            "/a/inbox",
            "/a/history",
            "/a/preferences",
        ):
            page.goto(ORIGIN + path)
            page.locator('#content[aria-busy="false"]').wait_for()
        chromium.close()
    print("observed Scan calls:", wrapped.calls["scan"])
    assert wrapped.calls["scan"] == 0
    assert wrapped.calls["query"] > 0


@pytest.mark.parametrize("hidden_prefix", [False, True])
def test_timeline_pages_do_not_skip_either_source(
    browser: UploadWorld, hidden_prefix: bool
) -> None:
    from datetime import timedelta

    from sanad.store import keys
    from sanad.store._base import Write
    from sanad.store.records import (
        InboundReceipt,
        OutboundIntent,
        from_record,
        record_item,
        to_record,
    )

    w, client = browser.world, browser.client
    reply = client.post(
        "/api/patient/messages",
        json={"command_id": "history-seed", "text": "what is my plan"},
        headers=headers(browser),
    )
    assert reply.status_code == 200
    source = from_record(w.store.patient_receipts(w.patient_scope)[0][0], InboundReceipt)
    outbound = next(
        from_record(row, OutboundIntent)
        for row in w.rows("outbound_intent")
        if row.body.get("status") == "provider_accepted" and row.body.get("audience") == "patient"
    )
    expected = set()
    photo_ids = set()
    for index in range(225):
        at = w.clock() - timedelta(hours=1, milliseconds=index * 500)
        transport_key = "history-" + str(index)
        id = keys.inbound("web-message", keys.digest(transport_key)).pk
        kind = "callback" if hidden_prefix and index < 70 else "photo" if index % 4 == 0 else "text"
        receipt = InboundReceipt.model_validate(
            source.model_dump()
            | {
                "id": id,
                "version": 1,
                "transport": "web-message",
                "transport_key": transport_key,
                "kind": kind,
                "received_at": at,
                "created_at": at,
                "updated_at": at,
                "state": "completed",
                "work_clock": None,
                "processing_claim": None,
                "payload": {"text": "history " + str(index)},
            }
        )
        assert (
            w.store.accept_inbound(transport_key, to_record(receipt, w.patient_scope)).status
            == "conflict"
        )  # completed input cannot be ingested
        assert w.store._atomic([Write(record_item(to_record(receipt, w.patient_scope)), None)], [])
        if kind != "callback":
            expected.add(id)
        if kind == "photo":
            photo_ids.add(id)
        out_id = "history-out-" + str(index)
        intent = OutboundIntent.model_validate(
            outbound.model_dump()
            | {
                "id": out_id,
                "logical_key": out_id,
                "version": 1,
                "created_at": at,
                "updated_at": at,
                "accepted_at": at - timedelta(hours=1) if hidden_prefix else at,
                "delivered_text": "Reply " + str(index),
            }
        )
        assert w.store._atomic([Write(record_item(to_record(intent, w.patient_scope)), None)], [])
        expected.add(out_id)
    seen = []
    ordering: list[tuple[str, str]] = []
    cursor = None
    for _ in range(30):
        response = client.get(
            "/api/patient/conversation", params={"cursor": cursor} if cursor else {}
        )
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page["items"]) <= 50
        seen += [r["id"] for r in page["items"]]
        ordering.extend((r["at"], r["id"]) for r in reversed(page["items"]))
        cursor = page["cursor"]
        if not cursor:
            break
    assert not cursor and len(seen) == len(set(seen))
    assert expected <= set(seen)
    assert ordering == sorted(ordering, reverse=True)
    seen_uploads = []
    for _ in range(30):
        page = client.get(
            "/api/patient/uploads", params={"cursor": cursor} if cursor else {}
        ).json()
        assert len(page["items"]) <= 30
        seen_uploads += [r["id"] for r in page["items"]]
        cursor = page["cursor"]
        if not cursor:
            break
    assert not cursor and set(seen_uploads) == photo_ids
    assert len(seen_uploads) == len(photo_ids)
    assert client.get("/api/patient/conversation?cursor=invalid").status_code == 400


def test_pointer_snapshot_is_atomic_and_uses_no_receipt_get(
    browser: UploadWorld, monkeypatch: Any
) -> None:
    from datetime import timedelta

    from sanad.store import keys
    from sanad.store.keys import Key
    from sanad.store.records import InboundReceipt, OperationalClock, from_record, to_record

    w = browser.world
    source = from_record(w.store.patient_receipts(w.patient_scope)[0][0], InboundReceipt)
    now = w.clock()
    receipt = InboundReceipt.model_validate(
        source.model_dump()
        | {
            "id": keys.inbound("web-message", keys.digest("pointer-rail")).pk,
            "version": 1,
            "transport": "web-message",
            "transport_key": "pointer-rail",
            "received_at": now,
            "created_at": now,
            "updated_at": now,
            "state": "pending",
            "processing_claim": None,
            "work_clock": OperationalClock(next_action_at=now, work_lane="ingress"),
        }
    )
    assert (
        w.store.accept_inbound(receipt.transport_key, to_record(receipt, w.patient_scope)).status
        == "created"
    )
    original = w.store._read

    def no_receipt(key: Key) -> Any:
        assert not key.pk.startswith("IN#")
        return original(key)

    with monkeypatch.context() as patch:
        patch.setattr(w.store, "_read", no_receipt)
        before, _ = w.store.patient_receipts(w.patient_scope)
    prior = next(r for r in before if r.id == receipt.id)
    assert prior.body["state"] == "pending"
    claim = w.store.claim_work(
        prior.scoped_key(w.patient_scope),
        prior.version,
        "pointer-worker",
        now,
        timedelta(minutes=10),
    )
    assert claim
    current = next(r for r in w.store.patient_receipts(w.patient_scope)[0] if r.id == receipt.id)
    assert current.body["state"] == "processing" and prior.body["state"] == "pending"
    assert current.body["received_at"] == prior.body["received_at"]
    assert w.store.defer_inbound(claim, now)
    assert (
        next(r for r in w.store.patient_receipts(w.patient_scope)[0] if r.id == receipt.id).body[
            "state"
        ]
        == "pending"
    )


def test_all_named_readers_request_bounded_pages(browser: UploadWorld, monkeypatch: Any) -> None:
    from sanad.concierge.plan import load
    from sanad.domain import TenantScope
    from sanad.scribe.patients import panel

    w = browser.world
    doctor_path = w.login_path(APPLICANT, id=995)
    calls: list[tuple[str, int]] = []
    original = w.store._query

    def query(*args: Any, **kwargs: Any) -> Any:
        limit = kwargs.get("limit", 100)
        assert limit <= 200
        calls.append((kwargs.get("prefix", kwargs.get("index", "")), limit))
        return original(*args, **kwargs)

    monkeypatch.setattr(w.store, "_query", query)
    # Force a continuation even on this small fixture. Request readers must
    # ignore it, rather than exhaust arbitrarily many small pages.
    from sanad.store.records import Cursor

    def bounded(method: Any) -> Any:
        def page(*args: Any, **kwargs: Any) -> Any:
            assert kwargs.get("cursor") is None
            # list_records has two required arguments; the index readers one.
            required = 2 if method.__name__ == "list_records" else 1
            assert len(args) <= required or args[required] is None
            assert kwargs.get("limit", 100) <= 200
            rows, _ = method(*args, **kwargs)
            return rows, Cursor(
                query="forced-continuation", position={"PK": "synthetic", "SK": "next"}
            )

        return page

    for name in ("list_records", "list_patients", "list_reviews"):
        monkeypatch.setattr(w.store, name, bounded(getattr(w.store, name)))
    for path in ("/api/patient/conversation", "/api/patient/uploads", "/api/patient/preferences"):
        assert browser.client.get(path).status_code == 200
    assert load(w.store, w.patient_scope, w.clock())
    assert panel(w.store, TenantScope(doctor_id=w.patient_scope.doctor_id))
    assert browser_login(browser.client, doctor_path).status_code == 303
    for path in (
        "/api/patients/" + w.patient_scope.patient_id,
        "/api/browser/reviews",
        "/api/browser/reviews?history=true",
    ):
        assert browser.client.get(path).status_code == 200
    assert any(prefix == "RECEIPT#" and limit == 50 for prefix, limit in calls)
    assert any(prefix == "RECEIPT#" and limit == 30 for prefix, limit in calls)
    assert any(limit == 200 for _, limit in calls)


def test_media_pointer_preserves_concurrent_receipt_state(
    browser: UploadWorld, monkeypatch: Any
) -> None:
    from datetime import timedelta

    from sanad.store.keys import Key
    from sanad.store.records import InboundReceipt, from_record
    from store.processing_fixtures import World
    from store.test_media_adapters import setup, work

    w = browser.world
    world = World.create(w.store, w.clock)
    retriever, receipt_id = setup(world)
    retriever.fetch_media("synthetic-handle", receipt_id=receipt_id)
    media = work(world, receipt_id)
    receipt_row = w.store.get(media.scope, "inbound_receipt", receipt_id)
    assert receipt_row
    receipt = from_record(receipt_row, InboundReceipt)
    original = w.store._read
    advanced = False

    def concurrent(key: Key) -> Any:
        nonlocal advanced
        value = original(key)
        if key == receipt_row.key and not advanced:
            advanced = True
            assert w.store.claim_work(
                receipt_row.scoped_key(media.scope),
                receipt.version,
                "receipt-worker",
                w.clock(),
                timedelta(minutes=10),
            )
        return value

    monkeypatch.setattr(w.store, "_read", concurrent)
    from sanad.store.records import to_record

    assert w.store.claim_work(
        to_record(media, media.scope).scoped_key(media.scope),
        media.version,
        "media-worker",
        w.clock(),
        timedelta(minutes=10),
    )
    assert advanced
    from sanad.domain import PatientScope

    assert isinstance(media.scope, PatientScope)
    snapshot = next(r for r in w.store.patient_receipts(media.scope)[0] if r.id == receipt_id)
    assert snapshot.body["state"] == "processing"
    assert snapshot.version == receipt.version + 1
    assert snapshot.media_snapshot
