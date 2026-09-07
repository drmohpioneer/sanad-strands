"""Trusted tick composition: discover bounded index hints, then use scoped sweepers.

Index discovery stays inside the worker. It never grants a browser or an agent
cross-tenant access; each Sweeper obtains a short-lived, resolved capability and
strongly rereads the accepted store's base records before handling work.
"""

from collections.abc import Callable
from datetime import timedelta
from time import monotonic
from typing import Any

from sanad.channels.telegram.router import TelegramRuntime, route_receipt
from sanad.domain import TenantScope
from sanad.steward.sweep import SweepBudget, Sweeper
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.keys import AccountScope
from sanad.store.records import (
    MODELS,
    Doctor,
    StoredRecord,
    WorkerCapability,
    from_record,
    item_record,
    model_scope,
)

LANES = ("ingress", "delivery", "mission", "followup", "review", "claim", "scribe", "media")
DEFAULT_BUDGET = SweepBudget()


def sweep_due(
    runtime: TelegramRuntime,
    store: StoreBase,
    claim_handler: Callable[[StoredRecord], None] | None = None,
    *,
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
    }
    discovered = 0
    for lane in LANES:
        cursor = None
        scopes: set[str] = set()
        while True:
            remaining = budget.max_items - max(totals["examined"], discovered)
            seconds = budget.max_seconds - (elapsed_clock() - start)
            if remaining <= 0 or seconds <= 0:
                totals["budget_exhausted"] = True
                return totals
            items, cursor = store._query(
                f"{lane}#0",
                index="GSI_DUE",
                through=keys.instant(now) + "#\uffff",
                cursor=cursor,
                limit=min(budget.page_size, remaining),
            )
            discovered += len(items)
            for item in items:
                record = item_record(item)
                scope = model_scope(from_record(record, MODELS[record.entity_type]))
                if isinstance(scope, AccountScope):
                    if scope != runtime.accounts.scope:
                        continue
                else:
                    doctor_row = store.get(
                        TenantScope(doctor_id=scope.doctor_id), "doctor", scope.doctor_id
                    )
                    if (
                        doctor_row is None
                        or from_record(doctor_row, Doctor).telegram_bot_id
                        != runtime.settings.bot_id
                    ):
                        continue
                identity = scope.model_dump_json()
                if identity in scopes:
                    continue
                scopes.add(identity)
                capability = WorkerCapability(
                    service_subject="tick",
                    permitted_lanes=frozenset({lane}),
                    resolved_scope=scope,
                    auth_expiry=now + timedelta(seconds=120),
                    invocation_id="tick:" + keys.instant(now),
                )
                handlers: dict[str, Callable[[StoredRecord], None]] = {}

                def ingress(row: StoredRecord) -> None:
                    resolved = model_scope(from_record(row, MODELS[row.entity_type]))
                    route_receipt(runtime, row.scoped_key(resolved), owner="tick")

                def delivery(row: StoredRecord) -> None:
                    resolved = model_scope(from_record(row, MODELS[row.entity_type]))
                    runtime.dispatcher.dispatch_one(
                        row.scoped_key(resolved), "tick", runtime.clock()
                    )

                handlers.update(ingress=ingress, delivery=delivery)
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

                    if isinstance(runtime.concierge_route, ConciergeTurn) and isinstance(
                        scope, PatientScope
                    ):
                        handlers["media"] = runtime.concierge_route.sweep
                sweeper = Sweeper(
                    runtime.steward,
                    runtime.inbound,
                    runtime.dispatcher,
                    capability,
                    lane_handlers=handlers,
                    elapsed_clock=elapsed_clock,
                )
                report = sweeper.sweep(
                    lane,
                    "0",
                    now,
                    SweepBudget(
                        max_items=max(0, budget.max_items - totals["examined"]),
                        max_seconds=max(0.0, budget.max_seconds - (elapsed_clock() - start)),
                        page_size=budget.page_size,
                    ),
                )
                for key in ("examined", "handled", "deferred"):
                    totals[key] += getattr(report, key)
                totals["errors"].extend(report.errors)
                if report.budget_exhausted:
                    totals["budget_exhausted"] = True
                    return totals
            if cursor is None:
                break
    return totals
