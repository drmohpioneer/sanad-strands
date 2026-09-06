"""Atomic, persistent replay guard over the accepted store CAS primitives.

DynamoDB TTL deletes old rows asynchronously. Until deletion, an expired row
can be replaced under its version guard; a valid signed request cannot outlive
the 600-second retention period. No patient record or clinical mutation lives here.
"""

from collections.abc import Callable, Mapping
from datetime import datetime

from sanad.ops.tick_signing import authentic
from sanad.store import keys
from sanad.store._base import StoreBase, Write


class NonceStore:
    def __init__(self, store: StoreBase, service: str):
        self.store, self.service = store, service

    def accept(self, nonce: str, now: int) -> bool:
        key = keys.operational(self.service, "NONCE", keys.digest(nonce))
        old = self.store._read(key)
        if old is not None and old["ttl"] > now:
            return False
        before = old["version"] if old else None
        return self.store._atomic(
            [
                Write(
                    {"PK": key.pk, "SK": key.sk, "version": (before or 0) + 1, "ttl": now + 600},
                    before,
                )
            ],
            [],
        )

    def cleanup(self, now: int, *, limit: int = 25) -> int:
        """Bounded opportunistic cleanup; DynamoDB TTL remains the full collector."""
        pk = keys.operational(self.service, "NONCE", "lookup").pk
        rows, _ = self.store._query(pk, prefix="NONCE#", limit=limit)
        return sum(
            row["ttl"] <= now
            and self.store._delete_nonce(keys.Key(row["PK"], row["SK"]), row["version"])
            for row in rows
        )


class TickVerifier:
    def __init__(self, secret: str, nonces: NonceStore, clock: Callable[[], datetime]):
        if not secret:
            raise ValueError("tick secret is required")
        self._secret, self.nonces, self.clock = secret, nonces, clock

    def verify(self, headers: Mapping[str, str], body: bytes) -> int:
        now = self.clock().timestamp()
        if not authentic(self._secret, headers, body, now):
            return 401
        return 200 if self.nonces.accept(headers["x-sanad-nonce"], int(now)) else 409
