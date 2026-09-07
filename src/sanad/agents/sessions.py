"""Bounded plain conversation memory under the accepted patient fence."""

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

from sanad.domain import PatientScope, VersionRef
from sanad.domain.boundaries import _BoundaryValue
from sanad.store.protocol import Store
from sanad.store.records import Lease, SessionSnapshot, canonical_json


class Turn(_BoundaryValue):
    role: Literal["user", "assistant"]
    text: str


class FencedSessionManager:
    def __init__(
        self,
        store: Store,
        scope: PatientScope,
        key: str,
        role: Literal["concierge", "coordinator"],
        fence: Lease,
        clock: Callable[[], datetime],
        *,
        safety_epoch: int,
        window: int = 8,
        source_order_versions: tuple[VersionRef, ...] = (),
    ):
        if fence.scope != scope:
            raise ValueError("session fence scope mismatch")
        self.store, self.scope, self.key, self.role = store, scope, key, role
        self.fence, self.clock = fence, clock
        self.safety_epoch, self.orders = safety_epoch, source_order_versions
        if not 2 <= window <= 8 or window % 2:
            raise ValueError("session window must retain complete pairs")
        self.window = window
        self.snapshot = store.load_session(scope, key)
        if self.snapshot is not None and self.snapshot.role != role:
            raise ValueError("session role mismatch")
        self.turns: tuple[Turn, ...] = ()
        if (
            self.snapshot
            and self.snapshot.safety_epoch == safety_epoch
            and (self.snapshot.source_order_versions == source_order_versions)
        ):
            try:
                self.turns = TypeAdapter(tuple[Turn, ...]).validate_python(
                    self.snapshot.blob.get("turns", [])
                )[-8:]
            except ValidationError:
                self.turns = ()
        self.turns = self._bounded(self.turns[-self.window :])

    @staticmethod
    def _bounded(turns: tuple[Turn, ...]) -> tuple[Turn, ...]:
        turns = tuple(t for t in turns[-8:] if len(t.text) <= 4000)
        while turns and len(canonical_json([t.model_dump() for t in turns])) > 24000:
            turns = turns[1:]
        # Keep complete conversational pairs, never an orphaned assistant statement.
        while turns and turns[0].role != "user":
            turns = turns[1:]
        return turns

    def prepare(self, prompt: str, reply: str) -> SessionSnapshot:
        now = self.clock()
        turns = self._bounded(
            (*self.turns, Turn(role="user", text=prompt), Turn(role="assistant", text=reply))[
                -self.window :
            ]
        )
        version = self.snapshot.version if self.snapshot else 0
        blob: dict[str, JsonValue] = {"turns": [t.model_dump(mode="json") for t in turns]}
        snapshot = SessionSnapshot(
            id=self.key,
            scope=self.scope,
            role=self.role,
            version=version + 1,
            session_version=version + 1,
            created_at=self.snapshot.created_at if self.snapshot else now,
            updated_at=now,
            blob=blob,
            source_order_versions=self.orders,
            safety_epoch=self.safety_epoch,
            fence_generation=self.fence.generation,
        )
        return snapshot

    def commit(self, prompt: str, reply: str) -> bool:
        snapshot = self.prepare(prompt, reply)
        version = self.snapshot.version if self.snapshot else 0
        return self.store.commit_session(
            self.scope, self.key, version, self.fence, snapshot=snapshot
        )
