"""Persist and read back the same receipt before acknowledging a browser command."""

from sanad.api.failures import RequestFailure, store_busy
from sanad.store.protocol import Store
from sanad.store.records import InboundAccept, InboundReceipt, from_record, to_record
from sanad.store.retry import BACKOFF, sleep


def persist(store: Store, receipt: InboundReceipt) -> InboundAccept:
    busy = False
    for attempt in range(4):
        try:
            accepted = store.accept_inbound(
                receipt.transport_key, to_record(receipt, receipt.scope)
            )
            if accepted.record is not None and accepted.status in {"created", "existing"}:
                saved = from_record(accepted.record, InboundReceipt)
                row = store.get(saved.scope, "inbound_receipt", saved.id)
                if row is not None and row.body.get("transport_key") == receipt.transport_key:
                    return accepted
        except Exception as error:
            busy = store_busy(
                error
            )  # No receipt ACK or exception payload; retry the identical durable key.
        if attempt < 3:
            sleep(BACKOFF[attempt])
    raise RequestFailure("store_busy" if busy else "receipt_persist_failed")
