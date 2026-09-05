import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError

from sanad.domain.boundaries import (
    AcceptedFactRef,
    AudioSpan,
    CandidateRef,
    ImageRegion,
    ObservationRef,
    PatientScope,
    Principal,
    Provenance,
    TenantScope,
    TextSpan,
    TimingProposal,
    VersionRef,
)

RECEIVED = datetime(2026, 9, 6, 10, 0, tzinfo=timezone(timedelta(hours=3)))
PROVENANCE: dict[str, object] = {
    "source_observation_id": "synthetic-receipt-1",
    "actor_kind": "patient",
    "actor_id": "synthetic-patient-1",
    "source_kind": "patient_report",
    "received_at": RECEIVED,
}
TIMING: dict[str, object] = {
    "proposed_due_at": RECEIVED + timedelta(days=1),
    "source": "scribe",
    "reason": "Synthetic proposal for boundary validation",
    "timezone": "Africa/Cairo",
    "anchor_time": RECEIVED,
    "anchor_kind": "observation_received",
    "policy_version": "synthetic-policy-1",
    "source_observation_ref": {"observation_id": "synthetic-receipt-1"},
}
VALUES: list[tuple[type[BaseModel], dict[str, object]]] = [
    (Principal, {"subject": "synthetic-subject", "actor_kind": "unknown"}),
    (TenantScope, {"doctor_id": "synthetic-doctor"}),
    (PatientScope, {"doctor_id": "synthetic-doctor", "patient_id": "synthetic-patient"}),
    (VersionRef, {"entity_type": "synthetic", "id": "synthetic-1", "version": 1}),
    (ObservationRef, {"observation_id": "synthetic-receipt-1"}),
    (CandidateRef, {"candidate_id": "synthetic-candidate-1", "version": 1}),
    (AcceptedFactRef, {"fact_kind": "clinical_fact", "fact_id": "synthetic-fact-1", "version": 1}),
    (TextSpan, {"start": 0, "end": 1}),
    (AudioSpan, {"start_ms": 0, "end_ms": 100}),
    (ImageRegion, {"asset_ref": "synthetic-asset", "x": 0, "y": 0, "width": 1, "height": 1}),
    (Provenance, PROVENANCE),
    (TimingProposal, TIMING),
]


@pytest.mark.parametrize(("model", "payload"), VALUES, ids=[m.__name__ for m, _ in VALUES])
def test_values_are_frozen_and_round_trip(
    model: type[BaseModel], payload: dict[str, object]
) -> None:
    value = model.model_validate(payload)
    assert model.model_validate_json(value.model_dump_json()) == value
    field = next(iter(model.model_fields))
    with pytest.raises(ValidationError, match="frozen"):
        setattr(value, field, getattr(value, field))
    with pytest.raises(ValidationError, match="frozen"):
        delattr(value, field)


@pytest.mark.parametrize(("model", "payload"), VALUES, ids=[m.__name__ for m, _ in VALUES])
def test_unknown_fields_fail(model: type[BaseModel], payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as exc:
        model.model_validate({**payload, "unexpected": "synthetic"})
    assert any(
        e["loc"] == ("unexpected",) and e["type"] == "extra_forbidden" for e in exc.value.errors()
    )


@pytest.mark.parametrize("payload", [{}, {"doctor_id": "synthetic"}, {"patient_id": "synthetic"}])
def test_patient_scope_requires_both_ids(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="Field required"):
        PatientScope.model_validate(payload)


@pytest.mark.parametrize("value", ["", " \t\n", 9007199254740993123])
def test_identifiers_are_nonblank_strings(value: object) -> None:
    for model, payload in VALUES:
        for field in (
            "subject",
            "doctor_id",
            "patient_id",
            "id",
            "entity_type",
            "observation_id",
            "candidate_id",
            "fact_id",
            "asset_ref",
            "source_observation_id",
            "actor_id",
            "policy_version",
            "reason",
        ):
            if field in payload:
                with pytest.raises(ValidationError):
                    model.model_validate({**payload, field: value})


@pytest.mark.parametrize("version", [0, -1, 1.5, True, "1"])
def test_versions_are_positive_integers(version: object) -> None:
    for model, payload in VALUES:
        if "version" in payload:
            with pytest.raises(ValidationError):
                model.model_validate({**payload, "version": version})


@pytest.mark.parametrize("epoch", [-1, True, 0.5, "0"])
def test_invalid_epoch(epoch: object) -> None:
    with pytest.raises(ValidationError):
        Principal.model_validate(
            {"subject": "synthetic", "actor_kind": "admin", "auth_epoch": epoch}
        )


def test_large_telegram_id_and_immutable_identity_collections() -> None:
    roles = ["admin", "doctor"]
    permissions = ["synthetic:read"]
    value = Principal.model_validate(
        {
            "subject": "synthetic-subject",
            "actor_kind": "doctor",
            "doctor_id": "synthetic-doctor",
            "user_id": "90071992547409931234567890",
            "auth_epoch": 0,
            "verified_roles": roles,
            "permissions": permissions,
        }
    )
    roles.clear()
    permissions.clear()
    assert value.verified_roles == frozenset({"admin", "doctor"})
    assert value.permissions == frozenset({"synthetic:read"})
    assert json.loads(value.model_dump_json())["user_id"] == "90071992547409931234567890"
    assert value.auth_epoch == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"verified_roles": ["doctor"]},
        {"permissions": ["synthetic:read"]},
        {"doctor_id": "synthetic"},
        {"patient_id": "synthetic"},
        {"actor_kind": "doctor"},
        {"actor_kind": "patient", "doctor_id": "synthetic"},
        {"actor_kind": "patient", "patient_id": "synthetic"},
        {"actor_kind": "admin", "verified_roles": ["admin", "patient"]},
        {"actor_kind": "admin", "verified_roles": ["doctor", "patient"]},
        {"actor_kind": "system", "verified_roles": ["admin"]},
        {"actor_kind": "invented"},
        {"verified_roles": ["system"]},
        {"actor_kind": "admin", "permissions": ["  "]},
        {"role": "admin"},
    ],
)
def test_invalid_principal_shape(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Principal.model_validate({"subject": "synthetic", "actor_kind": "unknown", **changes})


@pytest.mark.parametrize("field", ["bot_id", "user_id", "session_id", "doctor_id", "patient_id"])
def test_optional_identity_cannot_be_blank(field: str) -> None:
    with pytest.raises(ValidationError):
        Principal.model_validate({"subject": "synthetic", "actor_kind": "admin", field: " \t"})


def test_principal_construction_only_validates_shape() -> None:
    # An invented identity can be constructed: no lookup/authentication has occurred.
    value = Principal(subject="synthetic-unverified", actor_kind="doctor", doctor_id="synthetic")
    assert value.verified_roles == frozenset()
    assert value.permissions == frozenset()
    assert value.auth_epoch is None
    patient = Principal(subject="synthetic", actor_kind="patient", doctor_id="d", patient_id="p")
    assert patient.permissions == frozenset()
    assert Principal(subject="synthetic-worker", actor_kind="system").verified_roles == frozenset()


@pytest.mark.parametrize(
    ("model", "payload", "field"),
    [
        (Provenance, PROVENANCE, "received_at"),
        (Provenance, PROVENANCE, "observed_at"),
        (Provenance, {**PROVENANCE, "confirmed_by": "synthetic-doctor"}, "confirmed_at"),
        (TimingProposal, TIMING, "proposed_due_at"),
        (TimingProposal, TIMING, "anchor_time"),
    ],
)
def test_every_supplied_instant_requires_timezone_and_normalizes_to_utc(
    model: type[BaseModel],
    payload: dict[str, object],
    field: str,
) -> None:
    for naive in (datetime(2026, 9, 6, 10), "2026-09-06T10:00:00"):
        with pytest.raises(ValidationError) as exc:
            model.model_validate({**payload, field: naive})
        assert any(e["type"] == "timezone_aware" for e in exc.value.errors())
    for aware in (RECEIVED, RECEIVED.isoformat()):
        value = model.model_validate({**payload, field: aware})
        instant = getattr(value, field)
        assert instant == datetime(2026, 9, 6, 7, tzinfo=UTC)
        assert instant.tzinfo is UTC
        assert json.loads(value.model_dump_json())[field] == "2026-09-06T07:00:00Z"


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan"), float("inf"), True, "0.5"])
def test_invalid_confidence(confidence: object) -> None:
    with pytest.raises(ValidationError):
        Provenance.model_validate({**PROVENANCE, "confidence": confidence})


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_confidence_endpoints(confidence: float) -> None:
    assert (
        Provenance.model_validate({**PROVENANCE, "confidence": confidence}).confidence == confidence
    )


def test_absent_provenance_is_not_invented() -> None:
    value = Provenance.model_validate(PROVENANCE)
    assert value.confidence is None
    assert value.validation_rule_ids == ()
    assert set(value.model_dump(exclude_none=True)) == set(PROVENANCE) | {"validation_rule_ids"}
    assert Provenance.model_validate_json(value.model_dump_json(exclude_none=True)) == value
    assert set(json.loads(value.model_dump_json())) == set(PROVENANCE) | {"validation_rule_ids"}
    assert set(Principal(subject="synthetic", actor_kind="unknown").model_dump()) == {
        "subject",
        "actor_kind",
        "verified_roles",
        "permissions",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmed_by": "synthetic-doctor"},
        {"confirmed_at": RECEIVED},
        {"model_id": " "},
        {"prompt_version": " "},
        {"extraction_version": " "},
        {"validation_rule_ids": [" "]},
        {"confirmation": True},
        {"source_kind": "invented"},
    ],
)
def test_invalid_provenance_metadata(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Provenance.model_validate({**PROVENANCE, **changes})


@pytest.mark.parametrize("source_kind", ["doctor_statement", "patient_report"])
def test_direct_report_retains_source_without_candidate(source_kind: str) -> None:
    value = Provenance.model_validate(
        {
            **PROVENANCE,
            "source_kind": source_kind,
            "confirmed_by": "synthetic-doctor",
            "confirmed_at": RECEIVED,
        }
    )
    assert value.source_kind == source_kind
    assert value.model_id is None
    assert "candidate_id" not in value.model_dump()


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (TextSpan, {"start": -1, "end": 2}),
        (TextSpan, {"start": 2, "end": 2}),
        (TextSpan, {"start": 3, "end": 2}),
        (TextSpan, {"start": True, "end": 2}),
        (AudioSpan, {"start_ms": -1, "end_ms": 2}),
        (AudioSpan, {"start_ms": 2, "end_ms": 2}),
        (AudioSpan, {"start_ms": 3, "end_ms": 2}),
        (AudioSpan, {"start_ms": 0.5, "end_ms": 2}),
    ],
)
def test_invalid_span_ranges(model: type[BaseModel], payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    ("span", "expected_type"),
    [
        ({"kind": "text", "start": 0, "end": 2}, TextSpan),
        ({"kind": "audio", "start_ms": 0, "end_ms": 1200}, AudioSpan),
    ],
)
def test_span_discriminator_round_trip(
    span: dict[str, object], expected_type: type[BaseModel]
) -> None:
    value = Provenance.model_validate({**PROVENANCE, "source_span": span})
    restored = Provenance.model_validate_json(value.model_dump_json())
    assert type(restored.source_span) is expected_type
    assert json.loads(restored.model_dump_json())["source_span"] == span


@pytest.mark.parametrize(
    "span",
    [
        {"kind": "text", "start_ms": 0, "end_ms": 100},
        {"kind": "audio", "start": 0, "end": 2},
        {"kind": "invented", "start": 0, "end": 2},
    ],
)
def test_wrong_span_variant(span: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Provenance.model_validate({**PROVENANCE, "source_span": span})


@pytest.mark.parametrize(
    "changes",
    [
        {"x": -0.1},
        {"y": -0.1},
        {"width": 0},
        {"height": 0},
        {"x": 0.8},
        {"y": 0.8},
        {"width": float("inf")},
        {"height": float("nan")},
        {"x": True},
        {"coordinate_space": "pixels"},
        {"asset_ref": " "},
    ],
)
def test_image_region_bounds(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ImageRegion.model_validate(
            {
                "asset_ref": "synthetic",
                "x": 0.1,
                "y": 0.1,
                "width": 0.5,
                "height": 0.5,
                **changes,
            }
        )


def test_extraction_metadata_and_unicode_offsets_survive_round_trip() -> None:
    text = "سند🩺"
    span = TextSpan(start=0, end=3)
    assert text[span.start : span.end] == "سند"
    value = Provenance.model_validate(
        {
            **PROVENANCE,
            "source_kind": "document_observation",
            "model_id": "synthetic-model",
            "prompt_version": "synthetic-prompt",
            "extraction_version": "synthetic-extractor",
            "source_span": span,
            "validation_rule_ids": ["synthetic-rule"],
            "source_region": {
                "asset_ref": "synthetic-image",
                "x": 0.25,
                "y": 0.25,
                "width": 0.75,
                "height": 0.75,
            },
        }
    )
    restored = Provenance.model_validate_json(value.model_dump_json())
    assert restored == value
    assert restored.validation_rule_ids == ("synthetic-rule",)
    assert isinstance(restored.source_region, ImageRegion)


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "Not/AZone"},
        {"timezone": "posixrules"},
        {"timezone": "localtime"},
        {"timezone": "../UTC"},
        {"timezone": "+03:00"},
        {"timezone": " "},
        {"reason": " "},
        {"policy_version": " "},
        {"source": "doctor"},
        {"anchor_kind": "invented"},
        {"due_at": RECEIVED},
        {"source_observation_ref": {"candidate_id": "synthetic", "version": 1}},
    ],
)
def test_invalid_timing_proposal(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TimingProposal.model_validate({**TIMING, **changes})


@pytest.mark.parametrize("source", ["scribe", "default"])
@pytest.mark.parametrize(
    "anchor",
    [
        "observation_received",
        "doctor_reference_time",
        "schedule_end",
        "reported_effective_start",
        "reported_effective_change",
    ],
)
def test_timing_keeps_source_timezone_and_anchor(source: str, anchor: str) -> None:
    value = TimingProposal.model_validate({**TIMING, "source": source, "anchor_kind": anchor})
    assert value.timezone == "Africa/Cairo"
    assert value.anchor_kind == anchor
    assert value.source == source
    assert value.anchor_time == RECEIVED
    assert value.anchor_time.tzinfo is UTC
    assert value.proposed_due_at.tzinfo is UTC
    assert type(value.source_observation_ref) is ObservationRef


class _ReferenceScenario(BaseModel):
    """Test-only consumer proves annotation and wire-stage separation."""

    observation: ObservationRef
    candidate: CandidateRef
    accepted: AcceptedFactRef
    provenance: Provenance


def test_synthetic_observation_candidate_and_accepted_fact_are_distinct() -> None:
    observation = ObservationRef(observation_id="synthetic-receipt-1")
    candidate = CandidateRef(candidate_id="synthetic-candidate-1", version=1)
    accepted = AcceptedFactRef(fact_kind="clinical_fact", fact_id="synthetic-fact-1", version=2)
    scenario = _ReferenceScenario(
        observation=observation,
        candidate=candidate,
        accepted=accepted,
        provenance=Provenance.model_validate(PROVENANCE),
    )
    restored = _ReferenceScenario.model_validate_json(scenario.model_dump_json())
    assert type(restored.observation) is ObservationRef
    assert type(restored.candidate) is CandidateRef
    assert type(restored.accepted) is AcceptedFactRef
    assert restored.provenance.source_observation_id == restored.observation.observation_id
    assert restored.provenance.actor_id == "synthetic-patient-1"
    assert restored.provenance.source_kind == "patient_report"
    assert "candidate_id" not in restored.accepted.model_dump()


@pytest.mark.parametrize(("model", "payload"), VALUES[4:7])
def test_wrong_stage_reference_rejected(model: type[BaseModel], payload: dict[str, object]) -> None:
    value = model.model_validate(payload)
    for other in (ObservationRef, CandidateRef, AcceptedFactRef):
        if other is model:
            continue
        with pytest.raises(ValidationError):
            other.model_validate(value)
        with pytest.raises(ValidationError):
            other.model_validate_json(value.model_dump_json())


@pytest.mark.parametrize("kind", ["clinical_fact", "evidence", "care_order"])
def test_accepted_fact_kinds(kind: Literal["clinical_fact", "evidence", "care_order"]) -> None:
    assert AcceptedFactRef(fact_kind=kind, fact_id="synthetic", version=1).fact_kind == kind


def test_candidate_kind_cannot_assert_acceptance() -> None:
    with pytest.raises(ValidationError):
        AcceptedFactRef.model_validate(
            {"fact_kind": "candidate", "fact_id": "synthetic", "version": 1}
        )
