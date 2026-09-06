"""Composition hooks for the existing dispatcher and bounded claim sweep lane."""

from datetime import datetime

from sanad.auth.claim import ClaimService
from sanad.auth.login import LoginService
from sanad.store.records import LoginExchange, OutboundIntent, StoredRecord


def credential_freshness(login: LoginService, intent: OutboundIntent, now: datetime) -> str | None:
    if intent.template_id != "patient_login_link":
        return None
    hash = intent.payload_ref.removeprefix("patient_login:")
    exchange = login.load(login.scope, "patient_login", hash, LoginExchange)
    if (
        exchange is None
        or exchange.state != "issued"
        or exchange.expires_at <= now
        or exchange.subject != intent.recipient_subject
        or exchange.binding_epoch != intent.binding_epoch_seen
        or exchange.consent_version != intent.consent_version_seen
    ):
        return "credential_revoked"
    return None


def claim_lane(claims: ClaimService, row: StoredRecord) -> None:
    if row.entity_type == "invitation":
        claims.expire(row.id)
    elif row.entity_type == "patient_claim":
        claims.expire(str(row.body["invitation_id"]))
