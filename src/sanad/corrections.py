"""Doctor-authored correcting versions, separate from raw source provenance."""

from typing import Literal

from pydantic import JsonValue

from sanad.domain import PatientScope, Principal, VersionRef
from sanad.domain.boundaries import NonblankStr, UtcInstant
from sanad.domain.entities import MonitorReading
from sanad.domain.predicates import PredicateResult
from sanad.scribe.proposal import ScribeRecord


class Correction(ScribeRecord):
    entity_type: Literal["correction"] = "correction"
    scope: PatientScope
    origin: Literal["authorized_correction"] = "authorized_correction"
    predecessor: VersionRef
    successor: VersionRef
    operation: Literal["replace", "detach", "amend"]
    actor: Principal
    reason: NonblankStr
    before: dict[str, JsonValue]
    after: dict[str, JsonValue]
    predicates: dict[str, PredicateResult] = {}
    monitor_links: dict[str, tuple[MonitorReading, ...]] = {}
    exposures: dict[str, str] = {}
    prior_report_ids: tuple[str, ...] = ()
    affected_mission_ids: tuple[str, ...] = ()


class FactHead(ScribeRecord):
    entity_type: Literal["fact_head"] = "fact_head"
    scope: PatientScope
    current_ref: VersionRef
    status: Literal["accepted", "detached"] = "accepted"


class CorrectionOffer(ScribeRecord):
    entity_type: Literal["correction_offer"] = "correction_offer"
    scope: PatientScope
    actor: Principal
    mission_ref: VersionRef
    due_at: UtcInstant
    reason: NonblankStr
    binding_epoch: int
    consent_version: int | None
    delivery_epoch: int
    expires_at: UtcInstant
    preview: NonblankStr
    consumed_at: UtcInstant | None = None
