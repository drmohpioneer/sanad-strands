"""Send-time account authority checks, independent of Telegram IO."""

from datetime import datetime

from sanad.accounts.records import Application, Doctor
from sanad.store.keys import AccountScope
from sanad.store.protocol import Store
from sanad.store.records import (
    IdentityConfig,
    InboundReceipt,
    Invitation,
    LoginExchange,
    OutboundIntent,
    PatientClaim,
    SubjectBinding,
    from_record,
)


def account_freshness(
    store: Store,
    intent: OutboundIntent,
    now: datetime,
    settings: IdentityConfig | None,
) -> str | None:
    scope = intent.scope
    if not isinstance(scope, AccountScope) or intent.recipient_subject is None:
        return "unsupported_variant"
    if settings is None or settings.bot_id != scope.bot_id:
        return "admin_changed" if intent.audience == "admin" else "recipient_authority"
    if intent.audience == "admin" and (
        intent.recipient_subject != settings.admin_user_id
        or intent.recipient_ref != settings.admin_user_id
    ):
        return "admin_changed"
    if intent.expires_at <= now:
        return "expired"
    auth = store.authorize(scope.bot_id, intent.recipient_subject)
    if auth.binding and auth.binding.private_chat_id != intent.recipient_ref:
        return "recipient_authority"
    if (
        intent.audience == "applicant"
        and auth.binding is not None
        and intent.notification_purpose != "patient_safety_response"
        and intent.template_id not in {"patient_safety_ack", "claim_refused"}
    ):
        return "application_state"
    for ref in intent.source_versions:
        row = store.get_account_source(scope, ref)
        if row is None:
            return "source_version"
        if row.entity_type == "doctor_login":
            exchange = from_record(row, LoginExchange)
            if (
                intent.template_id != "doctor_login_link"
                or exchange.state != "issued"
                or exchange.expires_at <= now
                or exchange.subject != intent.recipient_subject
                or exchange.auth_epoch != auth.auth_epoch
                or exchange.doctor_id != auth.principal.doctor_id
            ):
                return "credential_revoked"
        elif row.entity_type == "patient_claim":
            claim = from_record(row, PatientClaim)
            if intent.audience == "doctor":
                allowed = (
                    intent.template_id == "claim_awaiting_doctor"
                    and claim.state == "pending"
                    and claim.consent_id is not None
                    and claim.review_at > now
                ) or (intent.template_id == "claim_declined_doctor" and claim.state == "rejected")
                if not allowed or auth.principal.doctor_id != claim.doctor_id:
                    return "claim_state"
            elif (
                claim.candidate_subject != intent.recipient_subject
                or claim.private_chat_id != intent.recipient_ref
                or not (
                    (
                        intent.template_id in {"consent_request", "consent_recorded_wait_doctor"}
                        and claim.state == "pending"
                        and claim.review_at > now
                    )
                    or (
                        intent.template_id in {"consent_declined_ack", "claim_rejected"}
                        and claim.state == "rejected"
                    )
                )
            ):
                return "claim_state"
        elif row.entity_type == "invitation":
            invitation = from_record(row, Invitation)
            if (
                intent.template_id != "invitation_expired_doctor"
                or invitation.state != "expired"
                or auth.principal.doctor_id != invitation.doctor_id
                or intent.audience != "doctor"
            ):
                return "claim_state"
        elif row.entity_type == "subject_binding":
            bound = from_record(row, SubjectBinding)
            if (
                intent.template_id != "claim_refused"
                or bound.telegram_user_id != intent.recipient_subject
                or bound.private_chat_id != intent.recipient_ref
            ):
                return "recipient_authority"
        elif row.entity_type == "application":
            app = from_record(row, Application)
            allowed = (
                app.status == "pending"
                if intent.template_id in {"application_received", "admin_new_application"}
                else app.status == "approved"
                if intent.template_id == "doctor_approved"
                else app.status == "rejected"
                if intent.template_id == "application_rejected"
                else False
            )
            if not allowed:
                return "application_state"
            if intent.audience != "admin" and (
                app.telegram_user_id != intent.recipient_subject
                or app.private_chat_id != intent.recipient_ref
            ):
                return "recipient_authority"
        elif row.entity_type == "doctor":
            doctor = from_record(row, Doctor)
            if (
                doctor.telegram_user_id != intent.recipient_subject
                or doctor.private_chat_id != intent.recipient_ref
            ):
                return "recipient_authority"
            if intent.template_id == "doctor_suspended_notice":
                if (
                    doctor.status != "suspended"
                    or doctor.auth_epoch != intent.recipient_auth_epoch_seen
                    or auth.binding is None
                    or auth.binding.status != "frozen"
                    or "doctor" not in auth.binding.role_set
                    or auth.binding.doctor_id != doctor.id
                ):
                    return "recipient_authority"
        elif row.entity_type == "inbound_receipt":
            receipt = from_record(row, InboundReceipt)
            if (
                receipt.source_subject != intent.recipient_subject
                or receipt.source_chat != intent.recipient_ref
                or receipt.state != "completed"
                or receipt.safety_screen_state != "screened"
            ):
                return "recipient_authority"
            if intent.template_id not in {
                "patient_emergency",
                "patient_safety_ack",
                "claim_refused",
            }:
                return "recipient_authority"
        else:
            return "source_version"
        if row.version != ref.version:
            return "source_version"
    if intent.audience == "doctor" and intent.template_id != "doctor_suspended_notice":
        if (
            "doctor" not in auth.principal.verified_roles
            or auth.auth_epoch != intent.recipient_auth_epoch_seen
        ):
            return "recipient_authority"
    if "doctor" in auth.principal.verified_roles:
        if auth.auth_epoch != intent.recipient_auth_epoch_seen:
            return "recipient_authority"
    return None
