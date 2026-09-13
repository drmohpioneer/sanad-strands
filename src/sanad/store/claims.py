"""Invocation-local tracking of issued claims for fenced worker cleanup."""

from contextvars import ContextVar

from sanad.store.records import Claim

issued: ContextVar[list[Claim] | None] = ContextVar("issued_claims", default=None)
