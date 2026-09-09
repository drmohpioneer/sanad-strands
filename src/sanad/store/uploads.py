"""Conditional staging ledger shared by memory and DynamoDB; no clinical mutations."""

from datetime import timedelta
from typing import TYPE_CHECKING

from sanad.domain import PatientScope
from sanad.domain.operations import transition_operational_clock
from sanad.store.identity import live_snapshot
from sanad.store.records import (
    InboundAccept,
    InboundReceipt,
    StoredRecord,
    UploadStage,
    WebSession,
    from_record,
    record_item,
    to_record,
)

if TYPE_CHECKING:
    from sanad.store._base import Check, StoreBase


def authority(store: "StoreBase", stage: UploadStage) -> list["Check"] | None:
    from sanad.store._base import Check

    now = store._clock()
    row = store.get(stage.session_scope, "web_session", stage.session_id)
    if row is None:
        return None
    session = from_record(row, WebSession)
    checks = [Check(row.key, row.version)]
    if (
        session.role != "patient"
        or session.revoked_at is not None
        or min(session.idle_expires_at, session.absolute_expires_at) <= now
        or session.subject != stage.subject
        or session.doctor_id != stage.scope.doctor_id
        or session.patient_id != stage.scope.patient_id
        or session.binding_id != stage.binding_id
        or session.binding_epoch != stage.binding_epoch
        or session.consent_version != stage.consent_version
        or session.auth_epoch != stage.auth_epoch
        or not live_snapshot(store, stage.session_scope, session, checks)
    ):
        return None
    auth = store.authorize(stage.session_scope.bot_id, stage.subject)
    if (
        auth.principal != stage.receipt.principal
        or auth.private_chat_id != stage.receipt.source_chat
    ):
        return None
    return checks


def reserve(store: "StoreBase", stage: UploadStage) -> bool:
    from sanad.store._base import Write

    # Revalidate values, including callers using unchecked model_copy/construct.
    stage = UploadStage.model_validate(stage.model_dump())
    if stage.version != 1 or stage.state != "reserved" or stage.recovery_deadline <= store._clock():
        return False
    checks = authority(store, stage)
    if checks is None:
        return False
    row = to_record(stage, stage.scope)
    return store._atomic([Write(record_item(row), None)], checks)


def attach(
    store: "StoreBase", transport_key: str, receipt_row: StoredRecord, upload_id: str
) -> InboundAccept:
    from sanad.store._base import Write

    try:
        receipt = from_record(receipt_row, InboundReceipt)
    except ValueError:
        return InboundAccept(status="forbidden")
    if not isinstance(receipt.scope, PatientScope):
        return InboundAccept(status="forbidden")
    row = store.get(receipt.scope, "upload_stage", upload_id)
    if row is None:
        return InboundAccept(status="forbidden")
    stage = from_record(row, UploadStage)
    if receipt != stage.receipt or transport_key != receipt.transport_key:
        return InboundAccept(status="conflict")
    checks = authority(store, stage)
    if checks is None:
        return InboundAccept(status="forbidden")
    if stage.state == "attached":
        existing = store.get(stage.scope, "inbound_receipt", receipt.id)
        if existing is None:
            return InboundAccept(status="conflict")
        return InboundAccept(status="existing", record=existing, state=str(existing.body["state"]))
    if stage.state != "reserved":
        return InboundAccept(status="conflict")
    changed = store._revision(row, store._clock(), state="attached", work_clock=None)
    canonical = to_record(receipt, receipt.scope)
    if store._atomic(
        [Write(record_item(changed), row.version), Write(record_item(canonical), None)], checks
    ):
        return InboundAccept(status="created", record=canonical, state="pending")
    # CAS failure is recoverable; never ACK a reservation as a saved receipt.
    return InboundAccept(status="conflict")


def discard(store: "StoreBase", scope: PatientScope, id: str, version: int) -> UploadStage | None:
    from sanad.store._base import Write

    row = store.get(scope, "upload_stage", id)
    if row is None or row.version != version:
        return None
    stage = from_record(row, UploadStage)
    now = store._clock()
    if stage.state == "attached" or stage.recovery_deadline > now:
        return None
    # Keep a tombstone clock: a delayed S3 writer can finish after disposal.
    assert stage.work_clock is not None
    changed = store._revision(
        row,
        now,
        state="discarded",
        work_clock=transition_operational_clock(
            stage.work_clock, now + timedelta(hours=24), attempt_delta=1
        ),
    )
    if not store._atomic([Write(record_item(changed), row.version)], []):
        return None
    return from_record(changed, UploadStage)
