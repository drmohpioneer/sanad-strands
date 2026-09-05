"""Contract 00 values from docs/domain-model.md.

Validation establishes data shape only. Authentication, authorization, record
existence, ownership and clinical acceptance require later authoritative services.
Use validated constructors/model_validate for new revisions, never the unchecked
Pydantic model_construct or model_copy(update=...) APIs for incoming data.
"""

from datetime import UTC, datetime
from functools import cache
from typing import Annotated, Literal, Self
from zoneinfo import available_timezones

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    model_validator,
)


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty or whitespace")
    return value


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


@cache
def _available_timezones() -> frozenset[str]:
    return frozenset(available_timezones())


def _iana_zone(value: str) -> str:
    if value not in _available_timezones():
        raise ValueError("must be an IANA timezone name")
    return value


def _is_absent(value: object) -> bool:
    return value is None


type NonblankStr = Annotated[StrictStr, AfterValidator(_nonblank)]
type PositiveVersion = Annotated[int, Field(strict=True, gt=0)]
type NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
type UnitInterval = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
type UtcInstant = Annotated[AwareDatetime, AfterValidator(_utc)]
type IanaZone = Annotated[NonblankStr, AfterValidator(_iana_zone)]
type ActorKind = Literal["unknown", "admin", "doctor", "patient", "system"]
type VerifiedRole = Literal["admin", "doctor", "patient"]


class _BoundaryValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


class Principal(_BoundaryValue):
    """Server-established identity data; constructing it does not authenticate."""

    subject: NonblankStr
    actor_kind: ActorKind
    verified_roles: frozenset[VerifiedRole] = frozenset()
    permissions: frozenset[NonblankStr] = frozenset()
    bot_id: NonblankStr | None = Field(default=None, exclude_if=_is_absent)
    user_id: NonblankStr | None = Field(default=None, exclude_if=_is_absent)
    session_id: NonblankStr | None = Field(default=None, exclude_if=_is_absent)
    doctor_id: NonblankStr | None = Field(default=None, exclude_if=_is_absent)
    patient_id: NonblankStr | None = Field(default=None, exclude_if=_is_absent)
    auth_epoch: NonnegativeInt | None = Field(default=None, exclude_if=_is_absent)

    @model_validator(mode="after")
    def validate_identity_shape(self) -> Self:
        if self.actor_kind == "unknown" and (
            self.verified_roles
            or self.permissions
            or self.doctor_id is not None
            or self.patient_id is not None
        ):
            raise ValueError("unknown actors cannot carry verified roles, permissions or scope")
        if self.actor_kind == "doctor" and self.doctor_id is None:
            raise ValueError("doctor actors require doctor_id")
        if self.actor_kind == "patient" and (self.doctor_id is None or self.patient_id is None):
            raise ValueError("patient actors require doctor_id and patient_id")
        if "patient" in self.verified_roles and self.verified_roles & {"admin", "doctor"}:
            raise ValueError("patient role cannot coexist with admin or doctor roles")
        if self.actor_kind == "system" and self.verified_roles:
            raise ValueError("system authority requires WorkerCapability, not verified_roles")
        return self


class TenantScope(_BoundaryValue):
    doctor_id: NonblankStr


class PatientScope(TenantScope):
    patient_id: NonblankStr


class VersionRef(_BoundaryValue):
    entity_type: NonblankStr
    id: NonblankStr
    version: PositiveVersion


class ObservationRef(_BoundaryValue):
    observation_id: NonblankStr


class CandidateRef(_BoundaryValue):
    candidate_id: NonblankStr
    version: PositiveVersion


class AcceptedFactRef(_BoundaryValue):
    fact_kind: Literal["clinical_fact", "evidence", "care_order"]
    fact_id: NonblankStr
    version: PositiveVersion


class TextSpan(_BoundaryValue):
    kind: Literal["text"] = "text"
    start: NonnegativeInt
    end: NonnegativeInt

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.start >= self.end:
            raise ValueError("text offsets require start < end")
        return self


class AudioSpan(_BoundaryValue):
    kind: Literal["audio"] = "audio"
    start_ms: NonnegativeInt
    end_ms: NonnegativeInt

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.start_ms >= self.end_ms:
            raise ValueError("audio offsets require start_ms < end_ms")
        return self


class ImageRegion(_BoundaryValue):
    asset_ref: NonblankStr
    coordinate_space: Literal["normalized_source"] = "normalized_source"
    x: UnitInterval
    y: UnitInterval
    width: Annotated[float, Field(strict=True, gt=0, le=1, allow_inf_nan=False)]
    height: Annotated[float, Field(strict=True, gt=0, le=1, allow_inf_nan=False)]

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("image region must fit within the normalized source")
        return self


class Provenance(_BoundaryValue):
    source_observation_id: NonblankStr
    actor_kind: ActorKind
    actor_id: NonblankStr
    source_kind: Literal[
        "doctor_statement", "patient_report", "document_observation", "clinician_confirmed"
    ]
    received_at: UtcInstant
    observed_at: UtcInstant | None = Field(default=None, exclude_if=_is_absent)
    model_id: NonblankStr | None = Field(default=None, exclude_if=_is_absent)
    prompt_version: NonblankStr | None = Field(default=None, exclude_if=_is_absent)
    extraction_version: NonblankStr | None = Field(default=None, exclude_if=_is_absent)
    source_span: Annotated[TextSpan | AudioSpan, Field(discriminator="kind")] | None = Field(
        default=None, exclude_if=_is_absent
    )
    source_region: ImageRegion | None = Field(default=None, exclude_if=_is_absent)
    confidence: UnitInterval | None = Field(default=None, exclude_if=_is_absent)
    confirmed_by: NonblankStr | None = Field(default=None, exclude_if=_is_absent)
    confirmed_at: UtcInstant | None = Field(default=None, exclude_if=_is_absent)
    validation_rule_ids: tuple[NonblankStr, ...] = ()

    @model_validator(mode="after")
    def validate_confirmation_pair(self) -> Self:
        if (self.confirmed_by is None) != (self.confirmed_at is None):
            raise ValueError("confirmed_by and confirmed_at must both be present or both absent")
        return self


class TimingProposal(_BoundaryValue):
    proposed_due_at: UtcInstant
    source: Literal["scribe", "default"]
    reason: NonblankStr
    timezone: IanaZone
    anchor_time: UtcInstant
    anchor_kind: Literal[
        "observation_received",
        "doctor_reference_time",
        "schedule_end",
        "reported_effective_start",
        "reported_effective_change",
    ]
    policy_version: NonblankStr
    source_observation_ref: ObservationRef
