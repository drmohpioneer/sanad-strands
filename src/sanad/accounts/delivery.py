"""Send-time account authority checks, independent of Telegram IO."""

from datetime import datetime

from sanad.accounts.records import Application, Doctor
from sanad.store.keys import AccountScope
from sanad.store.protocol import Store
from sanad.store.records import IdentityConfig, InboundReceipt, OutboundIntent, from_record


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
        and intent.template_id != "patient_safety_ack"
    ):
        return "application_state"
    for ref in intent.source_versions:
        row = store.get_account_source(scope, ref)
        if row is None:
            return "source_version"
        if row.entity_type == "application":
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
            if intent.template_id not in {"patient_emergency", "patient_safety_ack"}:
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
