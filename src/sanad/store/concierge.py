"""Narrow write authority for the receipt-bound deterministic patient executor."""

from datetime import datetime
from typing import TYPE_CHECKING

from sanad.concierge.plan import authorized
from sanad.concierge.records import PatientAction, ReportFactPayload
from sanad.scribe.records import ClinicalFact
from sanad.store.records import (
    CommitRequest,
    Consent,
    Patient,
    PatientBinding,
    PatientProfile,
    from_record,
)

if TYPE_CHECKING:
    from sanad.store._base import Check, StoreBase

KINDS = frozenset(
    {"ConciergeReply", "CreateSupportTicket", "RecordPatientReply", "SetContactPreference"}
)
ALLOWED = frozenset(
    {
        "patient_action",
        "clinical_fact",
        "mission",
        "followup",
        "review",
        "patient",
        "patient_profile",
        "consent",
        "patient_binding",
        "evidence_annotation",
        "media_work",
        "outbound_intent",
    }
)


def guards(store: "StoreBase", request: CommitRequest, now: datetime) -> list["Check"] | None:
    from sanad.store._base import Check

    command = request.command
    actor = command.principal
    if (
        command.payload.get("type") not in KINDS
        or not actor.bot_id
        or not command.work_claim
        or not command.fence
        or not request.receipt_completion
    ):
        return None
    auth = store.authorize(actor.bot_id, actor.subject)
    if not auth.binding:
        return None
    snap = authorized(store, actor, auth.binding, now)
    if (
        not snap
        or snap.scope != command.scope
        or command.payload.get("receipt_id") != command.work_claim.record_key.pk
    ):
        return None
    receipt = store.get(snap.scope, "inbound_receipt", command.work_claim.record_key.pk)
    if (
        not receipt
        or receipt.body.get("source_subject") != actor.subject
        or receipt.body.get("source_chat") != snap.binding.private_chat_id
    ):
        return None
    if any(r.entity_type not in ALLOWED for r in request.puts):
        return None
    subject_row = store.get(auth.binding.scope, "subject_binding", auth.binding.id)
    doctor_row = store.get(snap.scope, "doctor", snap.doctor.id)
    if not subject_row or not doctor_row:
        return None
    checks = [
        Check(subject_row.key, subject_row.version),
        Check(doctor_row.key, doctor_row.version),
    ]
    for model in (snap.patient, snap.consent, snap.binding):
        row = store.get(snap.scope, model.entity_type, model.id)
        assert row
        checks.append(Check(row.key, row.version))
    changed = {
        r.entity_type: r
        for r in request.puts
        if r.entity_type in {"patient", "consent", "patient_binding", "patient_profile"}
    }
    if changed:
        if command.payload.get("type") != "SetContactPreference" or set(changed) != {
            "patient",
            "consent",
            "patient_binding",
            "patient_profile",
        }:
            return None
        patient = from_record(changed["patient"], Patient)
        consent = from_record(changed["consent"], Consent)
        binding = from_record(changed["patient_binding"], PatientBinding)
        profile = from_record(changed["patient_profile"], PatientProfile)
        if not (
            consent.version
            == snap.consent.version + 1
            == patient.consent_version
            == binding.consent_version
            == profile.consent_version
            and profile.consent_active
            and profile.binding_active
            and patient.delivery_epoch == profile.delivery_epoch == snap.profile.delivery_epoch + 1
            and profile.routine_contact_enabled == consent.routine_contact_enabled
            and profile.routine_paused_until == patient.resume_at
        ):
            return None
        for old, new, fields in (
            (
                snap.patient,
                patient,
                {
                    "version",
                    "record_version",
                    "updated_at",
                    "consent_version",
                    "delivery_epoch",
                    "contact_status",
                    "resume_at",
                },
            ),
            (
                snap.consent,
                consent,
                {
                    "version",
                    "updated_at",
                    "routine_contact_enabled",
                    "quiet_hours",
                    "scheduled_slot_consents",
                },
            ),
            (snap.binding, binding, {"version", "updated_at", "consent_version"}),
            (
                snap.profile,
                profile,
                {
                    "version",
                    "updated_at",
                    "consent_version",
                    "delivery_epoch",
                    "routine_contact_enabled",
                    "routine_paused_until",
                },
            ),
        ):
            if old.model_dump(exclude=fields) != new.model_dump(exclude=fields):
                return None
        if not snap.consent.routine_contact_enabled and consent.routine_contact_enabled:
            if not any(
                r.entity_type == "patient_action"
                and r.body.get("action") == "resume"
                and r.body.get("consumed_at")
                for r in request.puts
            ):
                return None
    for row in request.puts:
        if row.entity_type == "outbound_intent":
            previous = store.get(snap.scope, "outbound_intent", row.id)
            fields = {"version", "updated_at", "status", "suppression_reason", "work_clock"}
            if (
                not previous
                or previous.body.get("status") != "queued"
                or row.body.get("status") != "suppressed"
                or command.payload.get("type") != "SetContactPreference"
                or {k: v for k, v in previous.body.items() if k not in fields}
                != {k: v for k, v in row.body.items() if k not in fields}
            ):
                return None
        if row.entity_type == "clinical_fact":
            fact = from_record(row, ClinicalFact)
            if (
                fact.version != 1
                or fact.category != "patient_report"
                or fact.visibility != "patient_released"
                or not isinstance(fact.payload, ReportFactPayload)
                or fact.provenance.actor_kind != "patient"
                or fact.provenance.actor_id != actor.subject
                or fact.provenance.source_kind != "patient_report"
                or fact.provenance.source_observation_id != receipt.id
            ):
                return None
        if row.entity_type == "patient_action":
            action = from_record(row, PatientAction)
            if action.actor_subject != actor.subject:
                return None
            if action.consumed_at:
                previous = store.get(snap.scope, "patient_action", row.id)
                payload = receipt.body.get("payload")
                if (
                    not previous
                    or not isinstance(payload, dict)
                    or payload.get("callback_token_hash") != row.id
                ):
                    return None
                prior_action = from_record(previous, PatientAction)
                if (
                    prior_action.consumed_at
                    or prior_action.expires_at <= now
                    or prior_action.delivery_epoch != snap.profile.delivery_epoch
                    or prior_action.binding_epoch != snap.profile.binding_epoch
                    or prior_action.consent_version != snap.consent.version
                    or action.model_dump(exclude={"version", "updated_at", "consumed_at"})
                    != prior_action.model_dump(exclude={"version", "updated_at", "consumed_at"})
                ):
                    return None
                checks.append(Check(previous.key, previous.version))
        if row.entity_type == "mission":
            if row.version > 1:
                from sanad.contact.scheduler import prime
                from sanad.domain import (
                    DRAFT_POLICY_2026_09,
                    Mission,
                    PatientReplied,
                    TransitionResult,
                    transition_mission,
                )

                original = next((m for m in snap.missions if m.id == row.id), None)
                if original:
                    reply = transition_mission(
                        original,
                        PatientReplied(event_id=command.command_id + ":reply:" + row.id),
                        now,
                        DRAFT_POLICY_2026_09,
                    )
                    if (
                        isinstance(reply, TransitionResult)
                        and isinstance(reply.aggregate, Mission)
                        and prime(reply.aggregate, now).model_dump(mode="json") == row.body
                    ):
                        continue
            if row.version == 1 and (
                row.body.get("kind") != "QUESTION"
                or command.payload.get("type") != "CreateSupportTicket"
            ):
                return None
            if row.version > 1 and (
                row.body.get("kind") != "MEDICATION"
                or row.body.get("state") != "fulfilled"
                or command.payload.get("type") != "RecordPatientReply"
            ):
                return None
    for row in request.intents:
        if (
            row.body.get("audience") == "doctor"
            and row.body.get("notification_purpose") != "DONE:FULFILLMENT"
        ):
            return None
        if (
            row.body.get("audience") == "patient"
            and row.body.get("notification_purpose") != "solicited_reply"
        ):
            return None
    return checks
