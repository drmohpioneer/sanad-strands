"""F14: metadata-only conflict diagnostics at the fenced commit and deferral sites."""

import json
import logging
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from sanad.auth.service import revise
from sanad.media.retrieve import Invocation, MediaRetriever, StoredMedia, invoked
from sanad.store._base import StoreBase
from sanad.store.records import Forbidden, StaleVersion, to_record
from store.conftest import Clock
from store.processing_fixtures import World
from store.test_media_adapters import setup, work
from store.test_retries_20_6i import busy


def lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(r.getMessage().split("media_commit_conflict ", 1)[1])
        for r in caplog.records
        if r.getMessage().startswith("media_commit_conflict ")
    ]


@pytest.mark.parametrize("invocation", ["direct", "tick", "recovery"])
@pytest.mark.parametrize(
    "cause",
    [
        "authority_invalid",
        "commit_forbidden",
        "lease_unavailable",
        "version_conflict",
        "claim_conflict",
        "store_busy",
        "transient_conflict",
        "reread_failed",
    ],
)
def test_f14_every_cause_fields_and_no_content(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    invocation: Invocation,
    cause: str,
) -> None:
    world = World.create(store, clock)
    retriever, receipt = setup(world)
    assert isinstance(retriever.fetch_media("synthetic-handle", receipt_id=receipt), StoredMedia)
    saved = work(world, receipt)
    claim = store.claim_work(
        to_record(saved, saved.scope).scoped_key(saved.scope),
        saved.version,
        "synthetic-loser",
        clock(),
        timedelta(minutes=10),
        start_extraction=True,
    )
    assert claim
    saved = work(world, receipt)
    retriever = replace(retriever, invocation=invocation)
    revised = revise(
        saved, clock(), processing_claim=None, transcript_ref="private-synthetic-content"
    )
    if cause == "authority_invalid":
        monkeypatch.setattr(retriever, "authority_check", lambda: False)
    elif cause == "commit_forbidden":
        monkeypatch.setattr(store, "commit", lambda *args: Forbidden())
    elif cause == "lease_unavailable":
        monkeypatch.setattr(store, "acquire_patient", lambda *args, **kwargs: None)
    elif cause in {"version_conflict", "claim_conflict", "reread_failed"}:
        monkeypatch.setattr(store, "commit", lambda *args: StaleVersion())
        if cause == "claim_conflict":
            assert store.defer_media(claim, clock(), "synthetic")
        elif cause == "reread_failed":
            monkeypatch.setattr(retriever, "_get", lambda *args: (_ for _ in ()).throw(busy()))
    else:

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise busy(
                "TransactionConflictException"
                if cause == "transient_conflict"
                else "ThrottlingException"
            )

        monkeypatch.setattr(store, "commit", fail)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        if cause in {"store_busy", "transient_conflict"}:
            with pytest.raises(ClientError):
                retriever._commit(revised, claim)
        else:
            assert not retriever._commit(revised, claim)
    captured = lines(caplog)
    assert len(captured) == 1
    line = captured[0]
    assert set(line) == {
        "cause",
        "stage",
        "expected_version",
        "current_version",
        "current_claim_owner",
        "current_claim_generation",
        "losing_claim_owner",
        "losing_claim_generation",
        "invocation",
        "reread_failed",
    }
    expected_cause = {
        "reread_failed": "version_conflict",
        "commit_forbidden": "authority_invalid",
    }.get(cause, cause)
    assert line["cause"] == expected_cause
    assert line["expected_version"] == saved.version and line["stage"] == "extract"
    assert (
        line["losing_claim_owner"] == claim.owner
        and line["losing_claim_generation"] == claim.generation
    )
    assert line["invocation"] == invocation
    assert line["reread_failed"] == (cause == "reread_failed")
    if cause == "reread_failed":
        assert (
            line["current_version"]
            is line["current_claim_owner"]
            is line["current_claim_generation"]
            is None
        )
    elif cause != "claim_conflict":
        assert line["current_version"] == saved.version
        assert line["current_claim_owner"] == claim.owner
        assert line["current_claim_generation"] == claim.generation
    assert "private-synthetic-content" not in caplog.text and "synthetic-handle" not in caplog.text


@pytest.mark.parametrize("stage", ["fetch", "normalize", "extract", "failure", "deferral"])
def test_f14_named_sites_log_failed_commits(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stage: str,
) -> None:
    world = World.create(store, clock)
    retriever, receipt = setup(world)
    if stage in {"extract", "failure", "deferral"}:
        assert isinstance(
            retriever.fetch_media("synthetic-handle", receipt_id=receipt), StoredMedia
        )
    original = store.commit

    def refuse(request: Any) -> Any:
        media = next((r for r in request.puts if r.entity_type == "media_work"), None)
        if (
            media
            and media.version > 1
            and (stage != "normalize" or media.body["stage"] == "extract")
        ):
            return StaleVersion()
        return original(request)

    monkeypatch.setattr(store, "commit", refuse)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        if stage in {"fetch", "normalize"}:
            retriever.fetch_media("synthetic-handle", receipt_id=receipt)
        elif stage == "extract":
            assert retriever.extraction_result(receipt, transcript_ref="private-content") is False
        elif stage == "failure":
            retriever.extraction_result(receipt, failure="unreadable")
        else:
            saved = work(world, receipt)
            claim = store.claim_work(
                to_record(saved, saved.scope).scoped_key(saved.scope),
                saved.version,
                "loser",
                clock(),
                timedelta(minutes=10),
            )
            assert claim
            monkeypatch.setattr(store, "defer_media", lambda *args: False)
            retriever._defer(claim, "private-content")
    captured = lines(caplog)
    assert len(captured) == 1
    assert captured[0]["cause"] == "version_conflict"
    assert all(r["invocation"] == "direct" for r in captured)
    assert "private-content" not in caplog.text


def test_f14_tick_context_and_recovery_do_not_leak_invocation(
    store: StoreBase, clock: Clock
) -> None:
    world = World.create(store, clock)

    @invoked("tick")
    def factory() -> MediaRetriever:
        return setup(world)[0]

    retriever = factory()
    assert retriever.invocation == "tick"
    from dataclasses import fields

    values = {
        field.name: getattr(retriever, field.name)
        for field in fields(retriever)
        if field.init and field.name != "invocation"
    }
    assert MediaRetriever(**values).invocation == "direct"


@pytest.mark.parametrize("entry", ["fetch_media", "fetch_telegram_file", "module", "alias"])
def test_f14_successful_stages_and_tick_sweep_log_zero(
    store: StoreBase, clock: Clock, caplog: pytest.LogCaptureFixture, entry: str
) -> None:
    from sanad.media import retrieve

    world = World.create(store, clock)
    retriever, receipt = setup(world)
    with caplog.at_level(logging.INFO):
        if entry in {"module", "alias"}:
            function = retrieve.fetch_media if entry == "module" else retrieve.fetch_telegram_file
            result = function("synthetic-handle", retriever=retriever, receipt_id=receipt)
        else:
            result = getattr(retriever, entry)("synthetic-handle", receipt_id=receipt)
        assert isinstance(result, StoredMedia) and result.stage == "extract"
        assert retriever.extraction_result(receipt, transcript_ref="private-content") is True
        assert replace(retriever, invocation="tick").sweep() == 1
    saved = work(world, receipt)
    assert saved.last_error == "extraction_unresolved"
    assert saved.work_clock and saved.work_clock.attempt_count == 1
    assert lines(caplog) == []


@pytest.mark.parametrize("conflict", [False, True])
def test_f14_nested_failure_and_outer_deferral_count_once(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    conflict: bool,
) -> None:
    from sanad.media.source import MediaFailure

    world = World.create(store, clock)
    retriever, receipt = setup(world)
    assert isinstance(retriever.fetch_media("synthetic-handle", receipt_id=receipt), StoredMedia)
    if conflict:
        monkeypatch.setattr(store, "commit", lambda *args: StaleVersion())
    caplog.clear()
    with caplog.at_level(logging.INFO):
        result = retriever.extraction_result(
            receipt, failure="unreadable" if conflict else "store_busy"
        )
    assert isinstance(result, MediaFailure)
    assert result.reason == ("stale_work" if conflict else "store_busy")
    captured = lines(caplog)
    assert len(captured) == (1 if conflict else 0)
    if conflict:
        assert captured[0]["stage"] == "extract"
        assert captured[0]["cause"] == "version_conflict"
    saved = work(world, receipt)
    assert saved.processing_claim is None and saved.infrastructure_deferrals == 1
    assert saved.work_clock and saved.work_clock.attempt_count == 0


@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize("code", ["ThrottlingException", "TransactionConflictException"])
def test_f14_release_error_keeps_commit_outcome_and_propagates(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    applied: bool,
    code: str,
) -> None:
    world = World.create(store, clock)
    retriever, receipt = setup(world)
    assert isinstance(retriever.fetch_media("synthetic-handle", receipt_id=receipt), StoredMedia)
    saved = work(world, receipt)
    claim = store.claim_work(
        to_record(saved, saved.scope).scoped_key(saved.scope),
        saved.version,
        "synthetic-loser",
        clock(),
        timedelta(minutes=10),
        start_extraction=True,
    )
    assert claim
    saved = work(world, receipt)
    revised = revise(saved, clock(), processing_claim=None, transcript_ref="private-content")
    if not applied:
        monkeypatch.setattr(store, "commit", lambda *args: StaleVersion())
    error = busy(code)

    def fail_release(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(store, "release_patient", fail_release)
    caplog.clear()
    with caplog.at_level(logging.INFO), pytest.raises(ClientError) as caught:
        retriever._commit(revised, claim)
    assert caught.value is error
    captured = lines(caplog)
    assert len(captured) == (0 if applied else 1)
    if not applied:
        assert captured[0]["cause"] == "version_conflict"
    assert work(world, receipt).transcript_ref == ("private-content" if applied else None)


@pytest.mark.parametrize("entry", ["fetch", "extract"])
def test_f14_public_release_error_after_applied_commit_is_silent(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    entry: str,
) -> None:
    from sanad.media.source import MediaFailure

    world = World.create(store, clock)
    retriever, receipt = setup(world)
    if entry == "extract":
        assert isinstance(
            retriever.fetch_media("synthetic-handle", receipt_id=receipt), StoredMedia
        )
    original = store.release_patient

    def fail_release(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise busy()

    if entry == "extract":
        monkeypatch.setattr(store, "release_patient", fail_release)
    else:

        def checkpoint(name: str) -> None:
            if name == "enqueued":
                monkeypatch.setattr(store, "release_patient", fail_release)

        retriever.checkpoint = checkpoint
    caplog.clear()
    with caplog.at_level(logging.INFO):
        if entry == "extract":
            assert retriever.extraction_result(receipt, transcript_ref="private-content") is False
            assert work(world, receipt).transcript_ref == "private-content"
        else:
            result = retriever.fetch_media("synthetic-handle", receipt_id=receipt)
            assert isinstance(result, MediaFailure) and result.reason == "store_busy"
            assert work(world, receipt).stage == "normalize"
    assert lines(caplog) == []


def test_f14_two_row_sweep_does_not_hide_distinct_claim_conflict(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sanad.media.source import MediaFailure
    from sanad.store import keys
    from sanad.store.records import Claim, MediaWork
    from store.fixtures import SCOPE, inbound
    from store.processing_fixtures import PATIENT

    world = World.create(store, clock)
    retriever, receipt = setup(world)
    second = inbound(
        id=keys.inbound("telegram", keys.digest("synthetic-bot:second-media")).pk,
        transport_key="synthetic-bot:second-media",
        provider_media_handle="synthetic-handle",
        kind="photo",
        principal=PATIENT,
        source_subject=PATIENT.subject,
    )
    assert store.accept_inbound(second.transport_key, to_record(second, SCOPE)).status == "created"
    for id in (receipt, second.id):
        assert isinstance(retriever.fetch_media("synthetic-handle", receipt_id=id), StoredMedia)
    failing_key = to_record(work(world, second.id), SCOPE).scoped_key(SCOPE)
    seen: list[Claim] = []
    conflicts: list[Claim | None] = []
    original_defer, original_conflict = store.defer_media, MediaRetriever._conflict

    def defer(claim: Claim, *args: Any, **kwargs: Any) -> bool:
        if claim.record_key == failing_key:
            return False
        return original_defer(claim, *args, **kwargs)

    def failure(
        self: MediaRetriever, current: MediaWork, claim: Claim, reason: str
    ) -> MediaFailure:
        seen.append(claim)
        self._defer(claim, "store_busy")
        return MediaFailure(reason="store_busy", request_resend=False)

    def conflict(
        self: MediaRetriever,
        id: str,
        stage: str,
        expected: int | None,
        claim: Claim | None,
        cause: str | None = None,
    ) -> None:
        conflicts.append(claim)
        original_conflict(self, id, stage, expected, claim, cause)

    monkeypatch.setattr(store, "defer_media", defer)
    monkeypatch.setattr(MediaRetriever, "_failure", failure)
    monkeypatch.setattr(MediaRetriever, "_conflict", conflict)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert replace(retriever, invocation="tick").sweep() == 2
    assert len(seen) == 2 and seen[0].record_key != seen[1].record_key
    assert (seen[0].owner, seen[0].generation, seen[0].version) == (
        seen[1].owner,
        seen[1].generation,
        seen[1].version,
    )
    assert len(conflicts) == 1 and conflicts[0] is not None
    assert conflicts[0].record_key == failing_key
    captured = lines(caplog)
    assert len(captured) == 1
    assert captured[0]["stage"] == "deferral" and captured[0]["invocation"] == "tick"
    assert second.id not in caplog.text and failing_key.pk not in caplog.text


@pytest.mark.parametrize(
    "error_code", [None, "ThrottlingException", "TransactionConflictException"]
)
def test_f14_outer_operations_reset_same_claim_outcome(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_code: str | None,
) -> None:
    world = World.create(store, clock)
    retriever, receipt = setup(world)
    assert isinstance(retriever.fetch_media("synthetic-handle", receipt_id=receipt), StoredMedia)
    saved = work(world, receipt)
    claim = store.claim_work(
        to_record(saved, saved.scope).scoped_key(saved.scope),
        saved.version,
        "same-claim",
        clock(),
        timedelta(minutes=10),
        start_extraction=True,
    )
    assert claim

    def refuse(*args: Any, **kwargs: Any) -> bool:
        if error_code:
            raise busy(error_code)
        return False

    monkeypatch.setattr(store, "defer_media", refuse)

    @invoked("tick")
    def outer() -> None:
        retriever._defer(claim, "store_busy")
        retriever._defer(claim, "store_busy")

    for _ in range(2):
        caplog.clear()
        with caplog.at_level(logging.INFO):
            outer()
        captured = lines(caplog)
        assert len(captured) == 1
        assert (
            captured[0]["cause"]
            == {
                None: "version_conflict",
                "ThrottlingException": "store_busy",
                "TransactionConflictException": "transient_conflict",
            }[error_code]
        )
