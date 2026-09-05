from datetime import timedelta
from uuid import uuid4

from domain_fixtures import NOW

from sanad.domain import PatientScope, Principal, TenantScope
from sanad.store import keys
from sanad.store.records import (
    AuditEvent,
    CommandEnvelope,
    CommitRequest,
    InboundReceipt,
    OperationalClock,
    OutboundIntent,
    PatientProfile,
    StoredRecord,
    WorkerCapability,
)

SCOPE = PatientScope(doctor_id="synthetic-doctor", patient_id="synthetic-patient")
OTHER = PatientScope(doctor_id="synthetic-other", patient_id="synthetic-patient")
TENANT = TenantScope(doctor_id=SCOPE.doctor_id)
ACTOR = Principal(
    subject="synthetic-doctor",
    actor_kind="doctor",
    doctor_id=SCOPE.doctor_id,
    verified_roles=frozenset({"doctor"}),
)


def profile(scope: PatientScope = SCOPE, **changes: object) -> PatientProfile:
    return PatientProfile.model_validate(
        {
            "id": scope.patient_id,
            "patient_id": scope.patient_id,
            "doctor_id": scope.doctor_id,
            "created_at": NOW,
            "updated_at": NOW,
        }
        | changes
    )


def inbound(scope: PatientScope = SCOPE, **changes: object) -> InboundReceipt:
    transport_key = "synthetic-bot:123456789012345678901234567890"
    return InboundReceipt.model_validate(
        {
            "id": keys.inbound("telegram", keys.digest(transport_key)).pk,
            "scope": scope,
            "transport": "telegram",
            "transport_key": transport_key,
            "created_at": NOW,
            "updated_at": NOW,
            "received_at": NOW,
            "source_subject": "synthetic-subject",
            "source_chat": "synthetic-private-chat",
            "channel": "telegram",
            "kind": "text",
            "payload_ref": "synthetic-protected-reference",
            "work_clock": OperationalClock(next_action_at=NOW, work_lane="ingress"),
        }
        | changes
    )


def intent(scope: PatientScope = SCOPE, **changes: object) -> OutboundIntent:
    return OutboundIntent.model_validate(
        {
            "id": "synthetic-intent",
            "scope": scope,
            "scope_kind": "patient",
            "audience": "patient",
            "logical_key": "synthetic-event:1:patient:routine:slot",
            "source_event_ids": ("synthetic-event",),
            "source_versions": (),
            "recipient_ref": "synthetic-verified-recipient",
            "notification_purpose": "routine_prompt",
            "eligibility_class": "scheduled",
            "payload_ref": "synthetic-message",
            "payload_digest": keys.digest("synthetic-message"),
            "conversation_sequence": 1,
            "slot_id": "synthetic-slot",
            "created_at": NOW,
            "updated_at": NOW,
            "expires_at": NOW + timedelta(hours=1),
            "status": "queued",
            "delivery_lease_seconds": 30,
            "work_clock": OperationalClock(next_action_at=NOW, work_lane="delivery"),
        }
        | changes
    )


def event(command_id: str, id: str = "synthetic-event", **changes: object) -> AuditEvent:
    return AuditEvent.model_validate(
        {
            "id": id,
            "event_id": id,
            "command_id": command_id,
            "scope": SCOPE,
            "event_type": "SYNTHETIC_ACCEPTED",
            "actor": ACTOR,
            "accepted_at": NOW,
            "created_at": NOW,
            "updated_at": NOW,
        }
        | changes
    )


def request(
    *records: StoredRecord, command_id: str | None = None, **changes: object
) -> CommitRequest:
    return CommitRequest.model_validate(
        {
            "command": CommandEnvelope(
                command_id=command_id or uuid4().hex,
                principal=ACTOR,
                scope=SCOPE,
                payload={"synthetic": True},
                requested_at=NOW,
            ),
            "puts": records,
            "expected": tuple(r.ref for r in records),
        }
        | changes
    )


def capability(scope: PatientScope | TenantScope = SCOPE) -> WorkerCapability:
    return WorkerCapability(
        service_subject="synthetic-worker",
        permitted_lanes=frozenset({"mission", "followup", "review", "ingress", "delivery"}),
        resolved_scope=scope,
        auth_expiry=NOW + timedelta(days=100),
        invocation_id="synthetic-invocation",
    )
