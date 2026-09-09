"""Immutable displayed references and single-action offers, never capabilities."""

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from sanad.domain import (
    NonblankStr,
    PatientScope,
    PositiveVersion,
    TenantScope,
    UtcInstant,
    VersionRef,
)
from sanad.domain.boundaries import _BoundaryValue
from sanad.store.keys import IntakeScope


class ReviewSnapshot(_BoundaryValue):
    scope: PatientScope | IntakeScope | TenantScope
    review_ref: VersionRef
    source_type: NonblankStr
    source_id: NonblankStr
    source_version: PositiveVersion
    material_version: PositiveVersion
    source_ref: VersionRef | None = None


class Notice(_BoundaryValue):
    entity_type: Literal["liaison_notice"] = "liaison_notice"
    id: NonblankStr
    scope: TenantScope
    version: PositiveVersion = 1
    created_at: UtcInstant
    updated_at: UtcInstant
    intent_scope: PatientScope | IntakeScope | TenantScope
    intent_id: NonblankStr
    attempt_id: NonblankStr
    doctor_subject: NonblankStr
    bot_id: NonblankStr
    auth_epoch: int
    snapshots: tuple[ReviewSnapshot, ...]
    payload: dict[str, JsonValue] = Field(repr=False)
    refusal: str | None = None


class ReviewOffer(_BoundaryValue):
    entity_type: Literal["review_offer"] = "review_offer"
    id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    scope: TenantScope
    version: PositiveVersion = 1
    created_at: UtcInstant
    updated_at: UtcInstant
    notice_id: NonblankStr
    doctor_subject: NonblankStr
    bot_id: NonblankStr
    auth_epoch: int
    snapshot: ReviewSnapshot
    action: NonblankStr
    expires_at: UtcInstant
    consumed_by: str | None = None
    consumed_at: UtcInstant | None = None
