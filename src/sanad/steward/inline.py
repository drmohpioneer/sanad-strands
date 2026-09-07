"""Best-effort scoped outbox pass; committed work always retains tick recovery."""

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from sanad.steward.dispatch import Dispatcher
from sanad.store.keys import Scope
from sanad.store.records import OutboundIntent, from_record

INLINE_MAX_INTENTS = 10
INLINE_MAX_SECONDS = 15.0


@dataclass(frozen=True)
class DeliveryScope:
    scope: Scope
    recipient_subject: str | None = None


def dispatch_inline(
    dispatcher: Dispatcher,
    scopes: tuple[DeliveryScope, ...],
    *,
    elapsed_clock: Callable[[], float] = monotonic,
) -> int:
    started, handled = elapsed_clock(), 0
    for target in dict.fromkeys(scopes):
        cursor = None
        while elapsed_clock() - started < INLINE_MAX_SECONDS and handled < INLINE_MAX_INTENTS:
            rows, cursor = dispatcher.store.list_records(
                target.scope, "outbound_intent", cursor=cursor, limit=10
            )
            for row in sorted(rows, key=lambda r: int(str(r.body.get("conversation_sequence", 0)))):
                if elapsed_clock() - started >= INLINE_MAX_SECONDS or handled >= INLINE_MAX_INTENTS:
                    return handled
                intent = from_record(row, OutboundIntent)
                now = dispatcher.steward.clock()
                if (
                    intent.scope != target.scope
                    or intent.status != "queued"
                    or intent.work_clock is None
                    or intent.work_clock.next_action_at > now
                    or (
                        target.recipient_subject is not None
                        and intent.recipient_subject != target.recipient_subject
                    )
                ):
                    continue
                dispatcher.dispatch_one(row.scoped_key(target.scope), "inline", now)
                handled += 1
            if cursor is None:
                break
    return handled
