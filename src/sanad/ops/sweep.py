"""Trusted tick composition: discover bounded index hints, then use scoped sweepers.

Index discovery stays inside the worker. It never grants a browser or an agent
cross-tenant access; each Sweeper obtains a short-lived, resolved capability and
strongly rereads the accepted store's base records before handling work.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from time import monotonic
from typing import Any

from sanad.channels.telegram.router import TelegramRuntime, route_receipt
from sanad.domain import TenantScope
from sanad.steward.sweep import SweepBudget, Sweeper
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.keys import AccountScope, Scope
from sanad.store.records import (
    MODELS,
    Doctor,
    DueItem,
    StoredRecord,
    SweepPosition,
    WorkerCapability,
    from_record,
    item_record,
    model_scope,
)

LANES = (
    "ingress",
    "review",
    "bundle",
    "mission",
    "followup",
    "question_digest",
    "claim",
    "scribe",
    "media",
    "delivery",
    "operational",
)
DEFAULT_BUDGET = SweepBudget()


def sweep_due(
    runtime: TelegramRuntime,
    store: StoreBase,
    claim_handler: Callable[[StoredRecord], None] | None = None,
    *,
    upload_handler: Callable[[StoredRecord], None] | None = None,
    budget: SweepBudget = DEFAULT_BUDGET,
    elapsed_clock: Callable[[], float] = monotonic,
) -> dict[str, Any]:
    start, now = elapsed_clock(), runtime.clock()
    totals: dict[str, Any] = {
        "examined": 0,
        "handled": 0,
        "deferred": 0,
        "errors": [],
        "budget_exhausted": False,
        "lane_capped": [],
        "discovered": {},
        "skipped": {},
        "oldest_due": {},
    }
    doctors: dict[str, str | None] = {}
    for lane in LANES:
        counts = dict.fromkeys(
            ("foreign_account", "doctor_missing", "doctor_other_bot", "not_progressed"), 0
        )
        totals["skipped"][lane] = counts
        totals["discovered"][lane] = 0
        totals["oldest_due"][lane] = None
        workers: dict[str, Sweeper] = {}
        position = None
        readable = True
        try:
            position = store.read_sweep_position(runtime.accounts.scope, lane, "0")
        except Exception:
            readable = False
            totals["errors"].append("position_read_failed")
        cursor = None
        last_key: tuple[str, str, str] | None = None
        head = True
        ended = False
        while totals["discovered"][lane] < budget.max_discovery_rows:
            if (
                totals["examined"] >= budget.max_items
                or elapsed_clock() - start >= budget.max_seconds
            ):
                totals["budget_exhausted"] = True
                break
            items, following = store._query(
                f"{lane}#0",
                index="GSI_DUE",
                through=keys.instant(now) + "#\uffff",
                cursor=cursor,
                limit=min(budget.page_size, budget.max_discovery_rows - totals["discovered"][lane]),
            )
            totals["discovered"][lane] += len(items)
            consumed = True
            for item in items:
                if (
                    totals["examined"] >= budget.max_items
                    or elapsed_clock() - start >= budget.max_seconds
                ):
                    totals["budget_exhausted"] = True
                    consumed = False
                    break
                instant = item["due_sort"].split("#", 1)[0]
                oldest = totals["oldest_due"][lane]
                totals["oldest_due"][lane] = min(oldest, instant) if oldest else instant
                previous_key = last_key
                last_key = (item["due_sort"], item["PK"], item["SK"])
                record = item_record(item)
                scope = model_scope(from_record(record, MODELS[record.entity_type]))
                if isinstance(scope, AccountScope):
                    if scope != runtime.accounts.scope:
                        counts["foreign_account"] += 1
                        continue
                else:
                    if scope.doctor_id not in doctors:
                        row = store.get(
                            TenantScope(doctor_id=scope.doctor_id), "doctor", scope.doctor_id
                        )
                        doctors[scope.doctor_id] = (
                            from_record(row, Doctor).telegram_bot_id if row else None
                        )
                    bot = doctors[scope.doctor_id]
                    if bot != runtime.settings.bot_id:
                        counts["doctor_missing" if bot is None else "doctor_other_bot"] += 1
                        continue
                identity = scope.model_dump_json()
                if identity not in workers:
                    workers[identity] = _worker(
                        runtime, scope, lane, now, elapsed_clock, claim_handler, upload_handler
                    )
                report = workers[identity].sweep_hints(
                    lane,
                    now,
                    (
                        DueItem(
                            record_key=record.scoped_key(scope),
                            entity_type=record.entity_type,
                            id=record.id,
                            next_action_at=instant,
                        ),
                    ),
                    SweepBudget(
                        max_items=budget.max_items - totals["examined"],
                        max_seconds=max(0.0, budget.max_seconds - (elapsed_clock() - start)),
                        page_size=budget.page_size,
                    ),
                )
                for key in ("examined", "handled", "deferred"):
                    totals[key] += getattr(report, key)
                counts["not_progressed"] += report.deferred
                totals["errors"].extend(report.errors)
                if report.budget_exhausted:
                    # Ownership reads may exhaust time before handing over the hint.
                    # Save the preceding key so this unhandled row is retried.
                    last_key = previous_key
                    totals["budget_exhausted"] = True
                    consumed = False
                    break
            if not consumed:
                break
            # Always read the head before consulting the saved continuation.
            if head:
                head = False
                if not readable:
                    break
                saved = (
                    (position.due_sort, position.due_pk, position.due_sk)
                    if position and position.due_sort and position.due_pk and position.due_sk
                    else None
                )
                if saved and last_key and saved > last_key:
                    assert position is not None
                    cursor = store.due_resume_cursor(lane, "0", now, position)
                    last_key = saved
                    continue
            cursor = following
            if following is None:
                ended = True
                break
        if totals["discovered"][lane] >= budget.max_discovery_rows and not ended:
            totals["lane_capped"].append(lane)
        if readable and (last_key is not None or ended):
            # An interrupted head read never moves an already saved continuation backwards.
            saved_key = (
                (position.due_sort, position.due_pk, position.due_sk)
                if position and position.due_sort and position.due_pk and position.due_sk
                else None
            )
            if not ended and saved_key and (last_key is None or saved_key > last_key):
                last_key = saved_key
            next_position = SweepPosition(
                id=keys.component(lane) + "#0",
                scope=runtime.accounts.scope,
                lane=lane,
                shard="0",
                version=position.version + 1 if position else 1,
                created_at=position.created_at if position else now,
                updated_at=now,
                due_sort=last_key[0] if last_key and not ended else None,
                due_pk=last_key[1] if last_key and not ended else None,
                due_sk=last_key[2] if last_key and not ended else None,
            )
            try:
                if not store.save_sweep_position(
                    next_position, position.version if position else None
                ):
                    totals["errors"].append("position_conflict")
            except Exception:
                totals["errors"].append("position_write_failed")
        if totals["budget_exhausted"]:
            break
    return totals


def _worker(
    runtime: TelegramRuntime,
    scope: Scope,
    lane: str,
    now: datetime,
    elapsed_clock: Callable[[], float],
    claim_handler: Callable[[StoredRecord], None] | None,
    upload_handler: Callable[[StoredRecord], None] | None,
) -> Sweeper:
    capability = WorkerCapability(
        service_subject="tick",
        permitted_lanes=frozenset({lane}),
        resolved_scope=scope,
        auth_expiry=now + timedelta(seconds=120),
        invocation_id="tick:" + keys.instant(now),
    )
    sweeper = Sweeper(
        runtime.steward,
        runtime.inbound,
        runtime.dispatcher,
        capability,
        elapsed_clock=elapsed_clock,
    )
    handlers: dict[str, Callable[[StoredRecord], None]] = {}

    def ingress(row: StoredRecord) -> None:
        resolved = model_scope(from_record(row, MODELS[row.entity_type]))
        route_receipt(runtime, row.scoped_key(resolved), owner="tick")

    def delivery(row: StoredRecord) -> None:
        resolved = model_scope(from_record(row, MODELS[row.entity_type]))
        runtime.dispatcher.dispatch_one(row.scoped_key(resolved), "tick", runtime.clock())

    def contact(row: StoredRecord, worker: Sweeper = sweeper) -> None:
        from sanad.contact.scheduler import schedule

        worker.accountability(row, runtime.clock())
        schedule(runtime.steward, row)

    def question_digest(row: StoredRecord) -> None:
        from sanad.contact.question_digest import wake

        wake(runtime.steward, row)

    def bundle(row: StoredRecord) -> None:
        from sanad.contact.bundle import wake

        wake(runtime.steward, row)

    def review(row: StoredRecord, worker: Sweeper = sweeper) -> None:
        from sanad.contact.bundle import review_wake

        if row.patient_id is None:
            review_wake(runtime.steward, row)
        else:
            worker.accountability(row, runtime.clock())

    handlers.update(
        ingress=ingress,
        delivery=delivery,
        mission=contact,
        followup=contact,
        bundle=bundle,
        question_digest=question_digest,
        review=review,
    )

    def operational(row: StoredRecord) -> None:
        from sanad.steward.removal import wake

        if row.entity_type == "patient_removal":
            wake(runtime.steward, row)
        elif row.entity_type == "upload_stage" and upload_handler:
            upload_handler(row)

    handlers["operational"] = operational
    if claim_handler:
        handlers["claim"] = claim_handler
    if runtime.scribe_route is not None:
        from sanad.scribe.turn import ScribeTurn

        if isinstance(runtime.scribe_route, ScribeTurn):
            handlers["scribe"] = runtime.scribe_route.sweep
            handlers["media"] = runtime.scribe_route.sweep
    if runtime.concierge_route is not None:
        from sanad.concierge.turn import ConciergeTurn
        from sanad.domain import PatientScope

        if isinstance(runtime.concierge_route, ConciergeTurn) and isinstance(scope, PatientScope):
            handlers["media"] = runtime.concierge_route.sweep
    sweeper.lane_handlers = handlers
    return sweeper
