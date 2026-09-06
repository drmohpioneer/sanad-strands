from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from sanad.domain import DoctorTimingPolicy, PatientScope, VersionRef
from sanad.domain.boundaries import NonblankStr, _BoundaryValue
from sanad.domain.operations import OperationsPolicy
from sanad.store.keys import Scope
from sanad.store.protocol import Store
from sanad.store.records import Accepted, CommitResult, Duplicate, StoredRecord


class Clock(Protocol):
    def __call__(self) -> datetime: ...


@dataclass(frozen=True)
class StewardPolicy:
    timing: DoctorTimingPolicy
    operations: OperationsPolicy = field(default_factory=OperationsPolicy)


type PolicyProvider = Callable[[PatientScope], StewardPolicy]


class CommandResult(_BoundaryValue):
    status: Literal[
        "accepted",
        "stale_version",
        "forbidden",
        "unsupported",
        "needs_confirmation",
        "invalid_input",
    ]
    event_ids: tuple[str, ...] = ()
    resulting_versions: tuple[VersionRef, ...] = ()
    reason_code: NonblankStr | None = None


def command_result(result: CommitResult) -> CommandResult:
    if isinstance(result, Duplicate):
        result = result.original
    if isinstance(result, Accepted):
        return CommandResult(
            status=result.command_status,
            event_ids=result.event_ids,
            resulting_versions=result.resulting_versions,
            reason_code=result.reason_code,
        )
    if result.status == "too_large":
        return CommandResult(status="invalid_input", reason_code="transaction_too_large")
    return CommandResult(status=result.status)


def records(store: Store, scope: Scope, entity_type: str) -> Iterator[StoredRecord]:
    cursor = None
    while True:
        page, cursor = store.list_records(scope, entity_type, cursor)
        yield from page
        if cursor is None:
            break
