"""Bounded due-index recovery; index entries are hints, never authority."""

from collections.abc import Callable
from datetime import datetime
from time import monotonic
from typing import Annotated

from pydantic import Field

from sanad.domain import FollowUpTask, Mission, PatientScope
from sanad.domain.boundaries import NonnegativeInt, PositiveVersion, _BoundaryValue
from sanad.steward.dispatch import Dispatcher
from sanad.steward.inbound import InboundProcessor
from sanad.steward.service import Steward, system_command
from sanad.store.keys import Scope
from sanad.store.records import (
    Cursor,
    ReconcileReport,
    StoredRecord,
    WorkerCapability,
    from_record,
    scope_owns,
)


class SweepBudget(_BoundaryValue):
    max_items: NonnegativeInt = 100
    max_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 10.0
    page_size: Annotated[PositiveVersion, Field(le=1000)] = 25


class SweepReport(_BoundaryValue):
    examined: NonnegativeInt = 0
    handled: NonnegativeInt = 0
    skipped: NonnegativeInt = 0
    deferred: NonnegativeInt = 0
    errors: tuple[str, ...] = ()
    budget_exhausted: bool = False
    cursor: Cursor | None = None


def next_work(record: StoredRecord) -> datetime | None:
    clock = record.body.get("work_clock")
    if not isinstance(clock, dict):
        return None
    at = clock.get("next_action_at")
    return datetime.fromisoformat(at) if isinstance(at, str) else None


class Sweeper:
    def __init__(
        self,
        steward: Steward,
        inbound: InboundProcessor,
        dispatcher: Dispatcher,
        capability: WorkerCapability,
        *,
        elapsed_clock: Callable[[], float] = monotonic,
    ):
        self.steward, self.store = steward, steward.store
        self.inbound, self.dispatcher, self.capability = inbound, dispatcher, capability
        self.elapsed_clock = elapsed_clock

    def sweep(self, lane: str, shard: str, now: datetime, budget: SweepBudget) -> SweepReport:
        start = self.elapsed_clock()
        examined = handled = skipped = deferred = 0
        errors: list[str] = []
        cursor = None
        while examined < budget.max_items and self.elapsed_clock() - start < budget.max_seconds:
            page, cursor = self.store.query_due(
                lane,
                shard,
                now,
                cursor,
                min(budget.page_size, budget.max_items - examined),
                capability=self.capability,
            )
            for hit in page:
                if self.elapsed_clock() - start >= budget.max_seconds:
                    return SweepReport(
                        examined=examined,
                        handled=handled,
                        skipped=skipped,
                        deferred=deferred,
                        errors=tuple(errors),
                        budget_exhausted=True,
                    )
                examined += 1
                scope = hit.record_key.scope
                fresh = self.store.get(scope, hit.entity_type, hit.id)
                if (
                    fresh is None
                    or next_work(fresh) != hit.next_action_at
                    or hit.next_action_at > now
                ):
                    skipped += 1
                    continue
                if not isinstance(scope, PatientScope):
                    deferred += 1
                    continue
                try:
                    if lane == "delivery" and fresh.entity_type == "outbound_intent":
                        self.dispatcher.dispatch_one(
                            hit.record_key, self.capability.service_subject, now
                        )
                    elif lane == "ingress" and fresh.entity_type == "inbound_receipt":
                        self.inbound.process_inbound(
                            hit.record_key, self.capability.service_subject, now
                        )
                    elif lane in {"mission", "followup", "review"} and fresh.entity_type == lane:
                        kind = "_Wake"
                        if lane == "mission":
                            mission = from_record(fresh, Mission)
                            if (
                                mission.state != "proposed"
                                and mission.escalation_at <= now
                                and mission.handled_deadline_generation
                                < mission.deadline_generation
                            ):
                                kind = "_Deadline"
                        elif lane == "followup":
                            task = from_record(fresh, FollowUpTask)
                            if (
                                task.state not in {"overdue", "contact_suppressed"}
                                and not task.deadline_handled
                                and (task.due_at or task.review_at) <= now
                            ):
                                kind = "_FollowupDeadline"
                        command = system_command(
                            scope,
                            f"sweep:{lane}:{fresh.id}:{fresh.version}",
                            {
                                "type": kind,
                                ("review_id" if lane == "review" else lane + "_id"): fresh.id,
                            },
                            now,
                            lane=lane,
                        )
                        self.steward.handle(command)
                    else:
                        deferred += 1
                        continue
                except Exception:
                    errors.append("worker_exception")
                latest = self.store.get(scope, hit.entity_type, hit.id)
                if latest is not None and (next_work(latest) is None or next_work(latest) > now):  # type: ignore[operator]
                    handled += 1
                else:
                    deferred += 1
            if cursor is None:
                return SweepReport(
                    examined=examined,
                    handled=handled,
                    skipped=skipped,
                    deferred=deferred,
                    errors=tuple(errors),
                )
        return SweepReport(
            examined=examined,
            handled=handled,
            skipped=skipped,
            deferred=deferred,
            errors=tuple(errors),
            budget_exhausted=True,
            cursor=cursor,
        )

    def reconcile(
        self, scope: Scope, cursor: Cursor | None = None, limit: int = 100
    ) -> ReconcileReport:
        if (
            not scope_owns(self.capability.resolved_scope, scope)
            or self.capability.auth_expiry <= self.steward.clock()
        ):
            return ReconcileReport()
        return self.store.reconcile_partition(scope, cursor, limit)
