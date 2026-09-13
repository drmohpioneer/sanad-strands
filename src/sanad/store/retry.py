"""Bounded retries for contention, without changing command or authority semantics."""

from collections.abc import Callable
from functools import wraps
from time import sleep as sleep

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from sanad.store.records import AuthorizationUnavailable

BACKOFF = (0.05, 0.15, 0.45)


class StoreConflict(RuntimeError):
    """Transient worker contention, distinct from a stale human decision."""


def transient_conflict(error: Exception) -> bool:
    if isinstance(error, StoreConflict):
        return True
    if not isinstance(error, ClientError):
        return False
    code = error.response["Error"]["Code"]
    reasons = {r["Code"] for r in error.response.get("CancellationReasons", [])}
    return code in {"TransactionConflictException", "TransactionInProgressException"} or (
        code == "TransactionCanceledException"
        and bool(reasons & {"TransactionConflict", "ConditionalCheckFailed"})
        and not reasons - {"None", "TransactionConflict", "ConditionalCheckFailed"}
    )


def authorization_read[**P, T](fn: Callable[P, T]) -> Callable[P, T]:
    @wraps(fn)
    def read(*args: P.args, **kwargs: P.kwargs) -> T:
        for attempt in range(4):
            try:
                return fn(*args, **kwargs)
            except Exception as error:
                if not isinstance(error, AuthorizationUnavailable) and not transient_conflict(
                    error
                ):
                    raise
                if attempt == 3:
                    raise AuthorizationUnavailable("authority_snapshot_busy") from None
                sleep(BACKOFF[attempt])
        raise AssertionError("unreachable")

    return read
