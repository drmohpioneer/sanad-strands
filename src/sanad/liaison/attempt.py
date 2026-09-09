"""Durable one-call budget. A refused budget always leaves the template available."""

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from sanad.domain import Principal, TenantScope
from sanad.store.keys import AccountScope, Scope, digest
from sanad.store.records import (
    AuditEvent,
    CommandEnvelope,
    CommitRequest,
    OutboundIntent,
    WorkerCapability,
    from_record,
    to_record,
)

if TYPE_CHECKING:
    from sanad.store._base import Check, StoreBase
    from sanad.store.protocol import Store


def start(
    store: "Store",
    intent: OutboundIntent,
    actor: Principal,
    worker: WorkerCapability,
    now: datetime,
) -> bool:
    assert isinstance(worker.resolved_scope, TenantScope)
    tenant = worker.resolved_scope
    id = digest("liaison-call:" + intent.id)
    command = CommandEnvelope(
        command_id=id,
        principal=actor,
        worker=worker,
        scope=tenant,
        requested_at=now,
        payload={
            "type": "_StartLiaison",
            "intent_scope": intent.scope.model_dump(mode="json"),
            "intent_id": intent.id,
            "attempt_id": intent.active_attempt_id,
        },
    )
    event = to_record(
        AuditEvent(
            id=id,
            event_id=id,
            command_id=id,
            scope=tenant,
            event_type="LIAISON_MODEL_ATTEMPT",
            actor=actor,
            accepted_at=now,
            created_at=now,
            updated_at=now,
        ),
        tenant,
    )
    result = store.commit(CommitRequest(command=command, events=(event,), expected=(event.ref,)))
    return result.status == "accepted"  # A replay cannot spend another provider call.


def guards(store: "StoreBase", request: CommitRequest, now: datetime) -> list["Check"] | None:
    from pydantic import TypeAdapter

    from sanad.store._base import Check
    from sanad.store.reviews import doctor_checks

    command, worker = request.command, request.command.worker
    if (
        type(command.scope) is not TenantScope
        or not store._identity
        or not worker
        or command.principal.actor_kind != "system"
        or command.principal.subject != "steward:notice-decorator"
        or worker.service_subject != command.principal.subject
        or worker.resolved_scope != command.scope
        or worker.permitted_lanes != frozenset({"delivery"})
        or worker.auth_expiry <= now
    ):
        return None
    if (
        request.puts
        or request.intents
        or request.markers
        or request.identity_reads
        or request.receipt_completion
        or len(request.events) != 1
    ):
        return None
    try:
        scope: Scope = TypeAdapter(Scope).validate_python(command.payload.get("intent_scope"))
    except ValidationError:
        return None
    if isinstance(scope, AccountScope) or scope.doctor_id != command.scope.doctor_id:
        return None
    row = store.get(scope, "outbound_intent", str(command.payload.get("intent_id", "")))
    if not row:
        return None
    intent = from_record(row, OutboundIntent)
    if (
        intent.status != "sending"
        or intent.audience != "doctor"
        or intent.notification_purpose == "DANGER"
        or intent.template_id == "scribe_photo_column"
        or intent.active_attempt_id != command.payload.get("attempt_id")
        or not intent.delivery_claim
        or intent.delivery_claim.expires_at <= now
        or command.command_id != digest("liaison-call:" + intent.id)
    ):
        return None
    from sanad.store.records import Doctor

    d = store.get(command.scope, "doctor", command.scope.doctor_id)
    if not d:
        return None
    doctor = from_record(d, Doctor)
    checks = doctor_checks(
        store,
        store.authorize(store._identity.bot_id, doctor.telegram_user_id).principal,
        command.scope.doctor_id,
    )
    if (
        checks is None
        or intent.recipient_ref != doctor.private_chat_id
        or intent.recipient_auth_epoch_seen != doctor.auth_epoch
    ):
        return None
    event = from_record(request.events[0], AuditEvent)
    if (
        event.event_type != "LIAISON_MODEL_ATTEMPT"
        or event.id != command.command_id
        or event.scope != command.scope
        or event.actor != command.principal
        or event.aggregate_refs
        or event.source_refs
        or event.payload_ref
    ):
        return None
    return [*checks, Check(row.key, row.version)]
