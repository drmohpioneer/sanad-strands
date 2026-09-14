"""Typed commands carry verified principals, never requested role sets or credentials."""

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator

from sanad.accounts.commands import AccountCommand
from sanad.domain import NonblankStr, UtcInstant
from sanad.domain.boundaries import IanaZone, _BoundaryValue
from sanad.domain.language import default_language
from sanad.domain.operations import PositiveDuration
from sanad.store.records import Accepted


class AuthPolicy(_BoundaryValue):
    version: NonblankStr = "login-claim-draft-2026-09"
    login_ttl: PositiveDuration = timedelta(minutes=10)
    invitation_ttl: PositiveDuration = timedelta(hours=24)
    idle_ttl: PositiveDuration = timedelta(minutes=30)
    absolute_ttl: PositiveDuration = timedelta(hours=12)


class ConsentPolicy(_BoundaryValue):
    """Per-doctor policy supplied explicitly until DoctorPolicy is released."""

    quiet_hours: tuple[str, str]
    clinic_contact: Annotated[str, Field(min_length=1, max_length=160)]
    retention: Annotated[str, Field(min_length=1, max_length=160)]
    urgent_response_policy_id: NonblankStr

    @field_validator("quiet_hours")
    @classmethod
    def hours(cls, value: tuple[str, str]) -> tuple[str, str]:
        import re

        if any(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", v) is None for v in value):
            raise ValueError("quiet hours require two local HH:MM values")
        return value


class IssueDoctorLogin(AccountCommand):
    type: Literal["IssueDoctorLogin"] = "IssueDoctorLogin"


class IssueAdminLogin(AccountCommand):
    type: Literal["IssueAdminLogin"] = "IssueAdminLogin"


class IssuePatientLogin(AccountCommand):
    type: Literal["IssuePatientLogin"] = "IssuePatientLogin"


class CreatePatientStub(AccountCommand):
    type: Literal["CreatePatientStub"] = "CreatePatientStub"
    display_name: Annotated[str, Field(strict=True, min_length=1, max_length=160)]
    language: Literal["ar", "en"] = default_language
    timezone: IanaZone = "Africa/Cairo"


class IssueInvitation(AccountCommand):
    type: Literal["IssueInvitation"] = "IssueInvitation"
    patient_id: NonblankStr
    include_qr: bool = False


class ClaimInvitation(AccountCommand):
    type: Literal["ClaimInvitation"] = "ClaimInvitation"
    invitation_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    private_chat_id: NonblankStr


class RecordConsent(AccountCommand):
    type: Literal["RecordConsent"] = "RecordConsent"
    claim_id: NonblankStr
    accept: bool


class ReadConsentTerms(AccountCommand):
    type: Literal["ReadConsentTerms"] = "ReadConsentTerms"
    claim_id: NonblankStr
    offer_generation: int
    callback_hash: NonblankStr


class RefreshConsentOffer(AccountCommand):
    type: Literal["RefreshConsentOffer"] = "RefreshConsentOffer"
    claim_id: NonblankStr


class ConsentContactUnavailable(AccountCommand):
    type: Literal["ConsentContactUnavailable"] = "ConsentContactUnavailable"
    claim_id: NonblankStr


class ConfirmPatientClaim(AccountCommand):
    type: Literal["ConfirmPatientClaim"] = "ConfirmPatientClaim"
    claim_id: NonblankStr


class RejectClaim(AccountCommand):
    type: Literal["RejectClaim"] = "RejectClaim"
    claim_id: NonblankStr


class RevokeBinding(AccountCommand):
    type: Literal["RevokeBinding"] = "RevokeBinding"
    patient_id: NonblankStr
    reason_code: Annotated[str, Field(strict=True, pattern=r"^[a-z0-9_:-]{1,80}$")]


class IssuedInvitation(_BoundaryValue):
    status: Literal["issued"] = "issued"
    token: SecretStr
    qr_payload: SecretStr
    expires_at: UtcInstant
    result: Accepted


class ExchangeRefused(_BoundaryValue):
    status: Literal[
        "malformed",
        "no_pre_session",
        "bad_csrf",
        "unknown_link",
        "already_used",
        "expired",
        "wrong_account",
        "commit_failed",
        "origin",
    ]
