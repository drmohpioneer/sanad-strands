"""Worker entry dispositions and immediate, fenced media-stage recovery in both stores."""

from datetime import timedelta
from typing import Any

import pytest
from store.account_fixtures import AccountWorld, update
from store.conftest import Clock
from store.processing_fixtures import World
from store.test_media_adapters import setup, work

from sanad.api.failures import RequestFailure
from sanad.api.internal import process_event
from sanad.channels.telegram.router import RouteResult
from sanad.media.retrieve import StoredMedia
from sanad.store._base import StoreBase
from sanad.store.records import AuthorizationUnavailable, to_record
from system.test_stability_20_6g import conflict


@pytest.mark.parametrize("failure", ["refusal", "conflict", "authorization", "unexpected", "busy"])
def test_worker_disposes_own_claim(
    store: StoreBase, clock: Clock, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from sanad.steward.apply import EffectsRejected

    w = AccountWorld.create(store, clock, process=False)
    assert w.post(update()).status_code == 200
    receipt = w.receipt(1)
    key = to_record(receipt, receipt.scope).scoped_key(receipt.scope)
    event = {"type": "process_receipt", "receipt": key.model_dump(mode="json")}

    def route(*args: Any, **kwargs: Any) -> RouteResult:
        claim = store.claim_work(
            key,
            receipt.version,
            "fixture-worker",
            clock(),
            w.runtime.accounts.policy.operations.claim_ttl,
        )
        assert claim
        if failure == "refusal":
            raise EffectsRejected("invalid_input")
        if failure == "authorization":
            raise AuthorizationUnavailable("private")
        if failure == "conflict":
            raise conflict()
        if failure == "unexpected":
            raise RuntimeError("private")
        return RouteResult(route="busy", status="patient_busy")

    with monkeypatch.context() as patch:
        patch.setattr("sanad.api.internal.route_receipt", route)
        if failure in {"authorization", "conflict"}:
            with pytest.raises(RequestFailure) as error:
                process_event(w.runtime, event)
            assert error.value.reason == (
                "authorization_unavailable" if failure == "authorization" else "ingress_conflict"
            )
        elif failure == "unexpected":
            with pytest.raises(RuntimeError):
                process_event(w.runtime, event)
        else:
            process_event(w.runtime, event)
    saved = w.receipt(1)
    assert saved.processing_claim is None
    assert saved.state == ("completed" if failure == "refusal" else "pending")
    if failure == "refusal":
        replies = store.list_records(w.runtime.accounts.scope, "outbound_intent")[0]
        assert any(r.body["template_id"] == "worker_refused" for r in replies)
    else:
        assert saved.work_clock and saved.work_clock.next_action_at == clock()
    process_event(w.runtime, event)
    assert w.receipt(1).state == "completed"


@pytest.mark.parametrize("stage", ["fetch", "normalize", "extract"])
@pytest.mark.parametrize("failure", ["storage", "commit", "exception"])
def test_media_exit_releases_claim_for_scheduled_retry(
    store: StoreBase, clock: Clock, monkeypatch: pytest.MonkeyPatch, stage: str, failure: str
) -> None:
    from sanad.media.retrieve import _StorageUnavailable

    w = World.create(store, clock)
    retriever, receipt_id = setup(w)
    if stage == "normalize":

        class Pause(Exception):
            pass

        def pause(name: str) -> None:
            if name == "committed_normalize":
                raise Pause()

        retriever.checkpoint = pause
        with pytest.raises(Pause):
            retriever.fetch_media("synthetic-handle", receipt_id=receipt_id)
        retriever.checkpoint = lambda name: None
        assert work(w, receipt_id).stage == "normalize"
    elif stage == "extract":
        assert isinstance(
            retriever.fetch_media("synthetic-handle", receipt_id=receipt_id), StoredMedia
        )
    original = retriever._commit

    def commit(value: Any, claim: Any = None, **kwargs: Any) -> bool:
        if claim:
            if failure == "exception":
                raise RuntimeError("private payload")
            return False
        return original(value, claim, **kwargs)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise _StorageUnavailable()

    with monkeypatch.context() as patch:
        if failure == "storage" and stage != "extract":
            patch.setattr(retriever, "_put_blob" if stage == "fetch" else "_get_blob", unavailable)
        else:
            patch.setattr(retriever, "_commit", commit)

        def run() -> object:
            if stage == "extract":
                return retriever.extraction_result(
                    receipt_id, transcript_ref="synthetic transcript"
                )
            return retriever.fetch_media("synthetic-handle", receipt_id=receipt_id)

        if failure == "exception":
            with pytest.raises(RuntimeError):
                run()
        else:
            run()
    saved = work(w, receipt_id)
    assert saved.processing_claim is None and saved.last_error
    assert saved.work_clock and saved.work_clock.attempt_count == 0
    assert saved.work_clock.next_action_at == clock() + timedelta(minutes=1)
    assert saved.state == "pending"
    clock.now = saved.work_clock.next_action_at
    if stage == "extract":
        assert retriever.extraction_result(receipt_id, transcript_ref="synthetic transcript")
    else:
        retriever.sweep()
        assert work(w, receipt_id).stage == "extract"
