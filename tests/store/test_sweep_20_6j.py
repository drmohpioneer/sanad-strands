"""F26: durable progress over foreign and held rows, with measured read bounds."""

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from math import ceil
from typing import Any

import pytest
from pydantic import BaseModel

from sanad.auth.service import revise
from sanad.channels.transport import SendOutcome
from sanad.domain import PatientScope, Principal, VersionRef
from sanad.ops.sweep import LANES, sweep_due
from sanad.steward.sweep import SweepBudget
from sanad.store import keys
from sanad.store._base import StoreBase, Write
from sanad.store.dynamodb import DynamoStore
from sanad.store.keys import AccountScope
from sanad.store.records import (
    OperationalClock,
    OutboundIntent,
    UploadStage,
    from_record,
    model_scope,
    record_item,
    to_record,
)
from store.account_fixtures import ADMIN, APPLICANT
from store.conftest import Clock
from store.fixtures import inbound
from store.scribe_fixtures import ScribeWorld
from store.test_reads_20_6i import CountClient


def put(store: StoreBase, model: BaseModel, before: int | None = None) -> None:
    assert store._atomic([Write(record_item(to_record(model, model_scope(model))), before)], [])


def quiet(world: ScribeWorld) -> None:
    """Move setup's own fake work out of this measured tick, never delete rows."""
    for lane in LANES:
        cursor = None
        while True:
            rows, cursor = world.store._query(f"{lane}#0", index="GSI_DUE", cursor=cursor)
            for row in rows:
                body = json.loads(row["body"])
                body["work_clock"]["next_action_at"] = (
                    world.clock() + timedelta(days=2)
                ).isoformat()
                body["version"] += 1
                row["body"] = json.dumps(body)
                row["version"] += 1
                row["due_sort"] = (
                    keys.instant(world.clock() + timedelta(days=2)) + row["due_sort"][27:]
                )
                assert world.store._atomic([Write(row, row["version"] - 1)], [])
            if cursor is None:
                break


def intent(world: ScribeWorld, i: int, *, foreign: str | None = None) -> OutboundIntent:
    base = world.scribe.repo.intent(world.doctor, "scribe_help", {}, f"rail-{i}")
    values: dict[str, Any] = {
        "id": f"rail-{i:04}",
        "work_clock": OperationalClock(
            next_action_at=world.clock() - timedelta(seconds=2000 - i), work_lane="delivery"
        ),
    }
    if foreign:
        values.update(
            scope=AccountScope(bot_id=foreign),
            scope_kind="account",
            audience="applicant",
            bot_id=foreign,
            recipient_subject="999999999999999901",
            recipient_ref="999999999999999901",
            doctor_auth_epoch_seen=None,
            recipient_auth_epoch_seen=None,
            source_versions=(VersionRef(entity_type="application", id="synthetic", version=1),),
        )
    return OutboundIntent.model_validate(base.model_dump() | values)


class Reads:
    def __init__(self, store: StoreBase, patch: pytest.MonkeyPatch):
        self.calls: Counter[str] = Counter()
        self.handlers = 0
        original = store._read

        def read(key: keys.Key) -> Any:
            self.calls["get_item"] += 1
            return original(key)

        if isinstance(store, DynamoStore):
            self.client = CountClient(store._client)
            patch.setattr(store, "_client", self.client)
            self.calls = self.client.calls
        else:
            patch.setattr(store, "_read", read)


def saved_intent(store: StoreBase, intent: OutboundIntent) -> OutboundIntent:
    row = store.get(intent.scope, "outbound_intent", intent.id)
    assert row is not None
    return from_record(row, OutboundIntent)


def tick(world: ScribeWorld, budget: SweepBudget | None = None, **kwargs: Any) -> dict[str, Any]:
    return sweep_due(
        world.runtime,
        world.store,
        budget=budget or SweepBudget(max_seconds=60),
        elapsed_clock=lambda: 0.0,
        **kwargs,
    )


def upload(world: ScribeWorld) -> UploadStage:
    scope = PatientScope(doctor_id=world.doctor.id, patient_id="synthetic-upload-patient")
    id = "a" * 32
    receipt = inbound(
        scope,
        transport="browser",
        transport_key="synthetic-upload",
        id=keys.inbound("browser", keys.digest("synthetic-upload")).pk,
        channel="browser",
        kind="photo",
        provider_media_handle="upload:" + id,
        safety_screen_state="screened",
        safety_result={},
        principal=Principal(
            subject="synthetic-subject",
            actor_kind="patient",
            verified_roles=frozenset({"patient"}),
            **scope.model_dump(),
        ),
    )
    return UploadStage(
        id=id,
        scope=scope,
        subject=receipt.source_subject,
        session_scope=world.runtime.accounts.scope,
        session_id="synthetic-session",
        binding_id="synthetic-binding",
        binding_epoch=1,
        consent_version=1,
        auth_epoch=1,
        content_digest="b" * 64,
        content_type="image/png",
        size=10,
        object_ref="synthetic-private",
        receipt=receipt,
        recovery_deadline=world.clock(),
        created_at=world.clock(),
        updated_at=world.clock(),
        work_clock=OperationalClock(next_action_at=world.clock(), work_lane="operational"),
    )


@pytest.mark.parametrize("p", [214, 1000])
def test_f26_bootstrap_head_resume_wrap_and_position_bound(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    p: int,
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    quiet(world)
    foreign = [intent(world, i, foreign=f"foreign-{i // 6}") for i in range(1, p)]
    for row in foreign:
        put(store, row)
    target = intent(world, p)
    put(store, target)
    stage = upload(world)
    put(store, stage)
    recovered = []

    def recover(row: Any) -> None:
        recovered.append(row.id)
        put(store, revise(stage, clock(), state="attached", work_clock=None), stage.version)

    seen = []
    dispatch = world.runtime.dispatcher.dispatch_one

    def capture(*args: Any, **kwargs: Any) -> Any:
        seen.append(args[0].sk)
        return dispatch(*args, **kwargs)

    monkeypatch.setattr(world.runtime.dispatcher, "dispatch_one", capture)
    bound = ceil(max(p - 25, 0) / 175) + 1
    reports = []
    for _ in range(bound):
        reports.append(tick(world, upload_handler=recover))
        assert recovered == [stage.id]
        clock.now += timedelta(seconds=1)
        if target.id in " ".join(seen):
            break
    assert len(reports) <= bound
    assert len(world.transport.calls) == 1
    assert world.transport.calls[0].recipient_ref == APPLICANT
    assert saved_intent(store, target).status == "provider_accepted"
    assert reports[0]["discovered"]["delivery"] == 200
    assert "delivery" in reports[0]["lane_capped"]
    assert reports[0]["skipped"]["delivery"]["foreign_account"] == 200
    assert "operational" in reports[0]["discovered"]
    position = store.read_sweep_position(world.runtime.accounts.scope, "delivery", "0")
    assert position and position.due_sort is None
    for row in foreign:
        saved = store.get(row.scope, "outbound_intent", row.id)
        assert saved and saved.version == row.version and saved.body["status"] == "queued"
        clock_value = from_record(saved, OutboundIntent).work_clock
        assert clock_value and clock_value.attempt_count == 0
    # Head is revisited after reset, and new earlier work is never behind the old key.
    early = intent(world, 0)
    put(store, early)
    tick(world)
    assert len(world.transport.calls) == 2


def test_f26_measured_shape_counts_smoke_terminal_statuses(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    quiet(world)
    foreign = [intent(world, i, foreign=f"foreign-{(i - 1) // 6}") for i in range(1, 199)]
    foreign += [intent(world, i, foreign="foreign-33") for i in range(203, 209)]
    for row in foreign:
        put(store, row)
    own = [intent(world, i) for i in range(199, 203)]
    for row in own:
        put(store, row)
    # Reproduce both approved fake applicants: exactly three account intents each.
    before_smoke, _ = store.list_records(world.runtime.accounts.scope, "outbound_intent")
    before_ids = {row.id for row in before_smoke}
    for subject in ("999999999999999901", "999999999999999902"):
        world.approve(subject)
    smoke_rows, _ = store.list_records(world.runtime.accounts.scope, "outbound_intent")
    smoke = [from_record(row, OutboundIntent) for row in smoke_rows if row.id not in before_ids]
    assert len(smoke) == 6
    for i, row in enumerate(smoke, 209):
        changes: dict[str, Any] = {
            "work_clock": OperationalClock(
                next_action_at=clock() - timedelta(seconds=2000 - i), work_lane="delivery"
            )
        }
        if row.audience == "admin":
            changes.update(
                recipient_ref="999999999999999900", recipient_subject="999999999999999900"
            )
        changed = revise(row, clock(), **changes)
        put(store, changed, row.version)
    # Handlers' reads are separately attributed; scope discovery must remain linear.
    reads = Reads(store, monkeypatch)
    original = world.runtime.dispatcher.dispatch_one
    handler_reads = 0

    def dispatch(*args: Any, **kwargs: Any) -> Any:
        nonlocal handler_reads
        before = reads.calls["get_item"]
        if args[0].scope == world.runtime.accounts.scope:
            world.transport.script.append(
                SendOutcome(status="failed", code="blocked", retryable=False)
            )
        result = original(*args, **kwargs)
        # Suppressed intents consume no transport script.
        world.transport.script.clear()
        handler_reads += reads.calls["get_item"] - before
        return result

    monkeypatch.setattr(world.runtime.dispatcher, "dispatch_one", dispatch)
    counts = []
    for _ in range(3):
        reads.calls.clear()
        handler_reads = 0
        report = tick(world)
        gets = reads.calls["get_item"]
        # Only the four D# hints require an ownership Doctor read. Reads of the
        # two fake applicants' doctors are already attributed to their handlers.
        assert (
            gets
            == len(LANES)
            + (1 if report["examined"] else 0)
            + 2 * report["examined"]
            + handler_reads
        )
        assert reads.calls["scan"] == 0
        counts.append((gets, handler_reads, report["examined"], dict(report["discovered"])))
        if len(counts) == 2:
            assert all(saved_intent(store, row).status == "provider_accepted" for row in own)
    assert all(saved_intent(store, row).status == "provider_accepted" for row in own)
    statuses = [
        (
            row.template_id,
            saved_intent(store, row).status,
            saved_intent(store, row).suppression_reason,
        )
        for row in smoke
    ]
    assert sorted((t, s) for t, s, _ in statuses) == sorted(
        [
            ("admin_new_application", "suppressed"),
            ("application_received", "suppressed"),
            ("doctor_approved", "failed"),
        ]
        * 2
    )
    assert {reason for t, _, reason in statuses if t == "admin_new_application"} == {
        "admin_changed"
    }
    assert {reason for t, _, reason in statuses if t == "application_received"} == {
        "application_state"
    }
    assert not any(call.recipient_ref == ADMIN for call in world.transport.calls)
    assert len(world.transport.calls) == 6
    print("F26 measured", type(store).__name__, counts, statuses)


@pytest.mark.parametrize(
    "failure",
    ["lost", "conflict", "unreadable", "overlap", "authority", "authority_after_discovery"],
)
def test_f26_position_failures_overlap_and_authority(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    quiet(world)
    target = intent(world, 1)
    put(store, target)
    original = store.save_sweep_position
    if failure in {"lost", "conflict"}:

        def fail(*args: Any, **kwargs: Any) -> bool:
            if failure == "lost":
                raise RuntimeError("lost")
            return False

        monkeypatch.setattr(store, "save_sweep_position", fail)
    if failure == "unreadable":

        def unreadable(*args: Any) -> None:
            raise RuntimeError("unreadable")

        monkeypatch.setattr(store, "read_sweep_position", unreadable)
    if failure == "authority":
        suspended = revise(
            world.doctor, clock(), status="suspended", auth_epoch=world.doctor.auth_epoch + 1
        )
        put(store, suspended, world.doctor.version)
    if failure == "authority_after_discovery":
        dispatch = world.runtime.dispatcher.dispatch_one
        invalidated = False

        def revoke_before_send(*args: Any, **kwargs: Any) -> Any:
            nonlocal invalidated
            if not invalidated:
                invalidated = True
                suspended = revise(
                    world.doctor,
                    clock(),
                    status="suspended",
                    auth_epoch=world.doctor.auth_epoch + 1,
                )
                put(store, suspended, world.doctor.version)
            return dispatch(*args, **kwargs)

        monkeypatch.setattr(world.runtime.dispatcher, "dispatch_one", revoke_before_send)
    if failure == "overlap":
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: tick(world), range(2)))
    else:
        tick(world)
    monkeypatch.setattr(store, "save_sweep_position", original)
    tick(world)
    withdrawn = failure.startswith("authority")
    assert len(world.transport.calls) == (0 if withdrawn else 1)
    row = store.get(target.scope, "outbound_intent", target.id)
    assert row and row.body["status"] == ("suppressed" if withdrawn else "provider_accepted")


def test_f26_held_heads_do_not_block_later_and_timeout_saves(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    quiet(world)
    blocker = intent(world, 0).model_copy(
        update={
            "template_id": "scribe_card",
            "conversation_sequence": 0,
            "source_event_ids": ("held-card",),
            "work_clock": OperationalClock(
                next_action_at=clock() + timedelta(days=1), work_lane="delivery"
            ),
        }
    )
    put(store, blocker)
    held = [
        intent(world, i).model_copy(
            update={
                "template_id": "scribe_card",
                "conversation_sequence": 1,
                "source_event_ids": ("held-card",),
            }
        )
        for i in range(1, 201)
    ]
    for row in held:
        put(store, row)
    target = intent(world, 214)
    put(store, target)
    budget = SweepBudget(max_items=250, max_seconds=60)
    first = tick(world, budget)
    assert first["skipped"]["delivery"]["not_progressed"] == 200
    tick(world, budget)
    assert len(world.transport.calls) == 1
    assert all(saved_intent(store, row).version == 1 for row in held)
    elapsed = 0.0
    query = store._query

    def timed(*args: Any, **kwargs: Any) -> Any:
        nonlocal elapsed
        result = query(*args, **kwargs)
        if kwargs.get("index") == "GSI_DUE" and args[0] == "delivery#0":
            elapsed += 0.4
        return result

    monkeypatch.setattr(store, "_query", timed)
    report = sweep_due(
        world.runtime,
        store,
        budget=SweepBudget(max_items=500, max_seconds=1),
        elapsed_clock=lambda: elapsed,
    )
    assert report["budget_exhausted"]
    position = store.read_sweep_position(world.runtime.accounts.scope, "delivery", "0")
    assert position and position.due_sort and position.due_sk
    assert position.due_sk.endswith("0050")


@pytest.mark.parametrize("failure", ["clock_changed", "expired", "wrong_lane", "wrong_scope"])
def test_f26_hint_freshness_and_capability_before_handler(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from sanad.ops.sweep import _worker
    from sanad.store.records import DueItem

    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    target = intent(world, 1)
    put(store, target)
    worker = _worker(world.runtime, target.scope, "delivery", clock(), lambda: 0, None, None)
    assert target.work_clock
    hit = DueItem(
        record_key=to_record(target, target.scope).scoped_key(target.scope),
        entity_type="outbound_intent",
        id=target.id,
        next_action_at=target.work_clock.next_action_at,
    )
    if failure == "clock_changed":
        changed = revise(
            target,
            clock(),
            work_clock=OperationalClock(
                next_action_at=clock() + timedelta(days=1), work_lane="delivery"
            ),
        )
        put(store, changed, target.version)
    elif failure == "expired":
        worker.capability = worker.capability.model_copy(update={"auth_expiry": clock()})
    elif failure == "wrong_lane":
        worker.capability = worker.capability.model_copy(
            update={"permitted_lanes": frozenset({"media"})}
        )
    else:
        worker.capability = worker.capability.model_copy(
            update={"resolved_scope": AccountScope(bot_id="foreign")}
        )
    reads = Reads(store, monkeypatch)
    report = worker.sweep_hints("delivery", clock(), (hit,), SweepBudget())
    assert report.examined == 0 and report.skipped == 1
    assert reads.calls["get_item"] == (1 if failure == "clock_changed" else 0)
    assert not world.transport.calls


def test_f26_missing_and_other_bot_doctors_are_counted_without_mutation(
    store: StoreBase,
    clock: Clock,
) -> None:
    from sanad.domain import TenantScope
    from sanad.store.keys import IntakeScope

    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    quiet(world)
    other = world.doctor.model_copy(
        update={
            "id": "synthetic-other-bot",
            "scope": TenantScope(doctor_id="synthetic-other-bot"),
            "telegram_bot_id": "foreign-bot",
        }
    )
    put(store, other)
    rows = [
        intent(world, i).model_copy(
            update={"scope": IntakeScope(doctor_id=doctor, intake_id="scribe")}
        )
        for i, doctor in enumerate(("synthetic-missing", other.id), 1)
    ]
    for row in rows:
        put(store, row)
    report = tick(world)
    assert report["skipped"]["delivery"] == {
        "foreign_account": 0,
        "doctor_missing": 1,
        "doctor_other_bot": 1,
        "not_progressed": 0,
    }
    assert report["examined"] == 0 and not world.transport.calls
    assert all(saved_intent(store, row) == row for row in rows)
