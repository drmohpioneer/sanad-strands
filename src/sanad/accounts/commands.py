"""Frozen command payloads; submitted content can never supply verified roles."""

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import Field

from sanad.domain import NonblankStr, PositiveVersion, Principal, VersionRef
from sanad.domain.boundaries import _BoundaryValue
from sanad.domain.operations import OperationsPolicy, PositiveDuration
from sanad.store.records import Accepted, Claim, Duplicate, Forbidden, StaleVersion, TooLarge


class AccountPolicy(_BoundaryValue):
    policy_version: NonblankStr = "draft-2026-09"
    application_review_interval: PositiveDuration = timedelta(hours=24)
    application_ack_interval: PositiveDuration = timedelta(hours=24)
    callback_ttl: PositiveDuration = timedelta(days=7)
    operations: OperationsPolicy = OperationsPolicy()


class AccountCommand(_BoundaryValue):
    command_id: NonblankStr
    actor: Principal
    expected_versions: tuple[VersionRef, ...] = ()
    session_role: Literal["doctor", "patient", "admin"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    admin_epoch: int | None = Field(default=None, exclude_if=lambda value: value is None)
    inbound_claim: Claim | None = Field(default=None, exclude=True)


class ApplyAsDoctor(AccountCommand):
    type: Literal["ApplyAsDoctor"] = "ApplyAsDoctor"
    private_chat_id: NonblankStr
    claimed_name: Annotated[str, Field(strict=True, max_length=160)] = ""
    claimed_specialty: Annotated[str, Field(strict=True, max_length=160)] = ""
    claimed_city: Annotated[str, Field(strict=True, max_length=160)] = ""
    restart_rejected: bool = False


class SetDoctorName(AccountCommand):
    type: Literal["SetDoctorName"] = "SetDoctorName"
    name: Annotated[str, Field(strict=True, min_length=1, max_length=160)]


class ApproveDoctor(AccountCommand):
    type: Literal["ApproveDoctor"] = "ApproveDoctor"
    application_id: NonblankStr
    expected_application_version: PositiveVersion


class RejectDoctor(AccountCommand):
    type: Literal["RejectDoctor"] = "RejectDoctor"
    application_id: NonblankStr
    expected_application_version: PositiveVersion
    reason_code: Annotated[str, Field(strict=True, pattern=r"^[a-z0-9_:-]{1,80}$")]


class SuspendDoctor(AccountCommand):
    type: Literal["SuspendDoctor"] = "SuspendDoctor"
    doctor_id: NonblankStr
    expected_doctor_version: PositiveVersion
    reason_code: Annotated[str, Field(strict=True, pattern=r"^[a-z0-9_:-]{1,80}$")]


class ReinstateDoctor(AccountCommand):
    type: Literal["ReinstateDoctor"] = "ReinstateDoctor"
    doctor_id: NonblankStr
    expected_doctor_version: PositiveVersion


class AlreadyInState(_BoundaryValue):
    status: Literal["already_in_state"] = "already_in_state"
    entity_id: NonblankStr
    state: NonblankStr


class CallbackRefused(_BoundaryValue):
    status: Literal["callback_refused"] = "callback_refused"
    reason: Literal["unknown", "expired", "consumed", "actor", "stale_version"]


type AccountResult = Annotated[
    Accepted | Duplicate | StaleVersion | Forbidden | TooLarge | AlreadyInState,
    Field(discriminator="status"),
]
