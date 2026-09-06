"""Bounded asynchronous Lambda hand-off; only opaque receipt references leave ingress."""

import json
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any

from sanad.ops.tick_signing import signed_headers
from sanad.store.keys import ScopedKey

logger = logging.getLogger(__name__)


def worker_body(event: dict[str, Any]) -> bytes:
    return json.dumps(
        {"type": event["type"], "receipt": event["receipt"]}, sort_keys=True, separators=(",", ":")
    ).encode()


class AsyncReceiptInvoker:
    def __init__(
        self,
        client: Any,
        function_name: str,
        secret: str,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client, self.function_name, self._secret = client, function_name, secret
        self.clock, self.sleep = clock, sleep

    def __call__(self, key: ScopedKey) -> None:
        event: dict[str, Any] = {"type": "process_receipt", "receipt": key.model_dump(mode="json")}
        event["headers"] = signed_headers(
            self._secret, str(int(self.clock())), secrets.token_hex(32), worker_body(event)
        )
        # Initial attempt plus at most three retries. SDK retries are disabled by composition.
        for attempt in range(4):
            try:
                result = self.client.invoke(
                    FunctionName=self.function_name,
                    InvocationType="Event",
                    Payload=json.dumps(event).encode(),
                )
                if result["StatusCode"] == 202:
                    return
            except Exception:
                # Never stringify provider errors: invocation bodies/credentials may be present.
                pass
            if attempt < 3:
                self.sleep(0.1 * 2**attempt)
        logger.warning("async hand-off deferred; durable receipt retained for sweep")
