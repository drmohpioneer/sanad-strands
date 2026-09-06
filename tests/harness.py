"""Synthetic clock and real store-boundary crash injection."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sanad.domain import utc_instant


@dataclass
class FakeClock:
    now: datetime

    def __post_init__(self) -> None:
        self.now = utc_instant(self.now)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta():
            raise ValueError("fake time cannot move backwards")
        self.now += delta
        return self.now


class SimulatedCrash(BaseException):
    """A process death bypasses ordinary exception handling."""


@contextmanager
def crash_after(store: object, method_name: str, n: int = 1) -> Iterator[None]:
    if n < 1:
        raise ValueError("call count must be positive")
    original = getattr(store, method_name)
    calls = 0

    def crashing(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == n:
            raise SimulatedCrash(method_name)
        return result

    setattr(store, method_name, crashing)
    try:
        yield
    finally:
        setattr(store, method_name, original)
