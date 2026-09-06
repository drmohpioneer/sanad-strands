import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest
from domain_fixtures import NOW
from harness import FakeClock
from pydantic import BaseModel, ValidationError
from store.fixtures import SCOPE
from store.processing_fixtures import World

from sanad.domain import ObservationRef, VersionRef
from sanad.safety import (
    SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY,
)
from sanad.safety import (
    IncidentFacts,
    LabCandidate,
    OrderSummary,
    OutputContext,
    Quantity,
    SafetyPolicy,
    find_bp,
    grade_bp,
    grade_lab,
    normalize,
    render_urgent,
    screen_text,
    to_incident_facts,
    validate_patient_output,
    wants_treatment_change,
)
from sanad.store.memory import MemoryStore
from sanad.store.records import Incident

SOURCE = ObservationRef(observation_id="synthetic-observation")


def test_frozen_json_roundtrips() -> None:
    output = OutputContext(
        mode="plan_explanation",
        active_orders=(
            OrderSummary(
                order_ref=VersionRef(entity_type="care_order", id="synthetic", version=1),
                drug_names=("atorvastatin",),
            ),
        ),
    )
    screen = screen_text("chest pain", policy=POLICY)
    candidate = LabCandidate(analyte_raw="K", value=Quantity(raw_value="6.5", raw_unit="mmol/L"))
    facts, _ = to_incident_facts(screen, source=SOURCE, policy=POLICY)
    values: tuple[BaseModel, ...] = (
        POLICY,
        POLICY.bp_thresholds,
        *POLICY.lab_rules,
        output,
        *output.active_orders,
        candidate,
        candidate.value,
        screen,
        grade_bp(190, 125, policy=POLICY),
        grade_lab(candidate, policy=POLICY),
        validate_patient_output("don't worry", context=output, policy=POLICY),
        facts,
    )
    for value in values:
        assert type(value).model_validate_json(value.model_dump_json()) == value
        field = next(iter(type(value).model_fields))
        with pytest.raises(ValidationError):
            setattr(value, field, getattr(value, field))
        with pytest.raises(ValidationError):
            type(value).model_validate(value.model_dump() | {"unapproved_extra": "no"})


@pytest.mark.parametrize("kind", ["screen", "vital", "lab", "unknown"])
def test_facts_are_accepted_by_actual_slice03_incident_transaction(kind: str) -> None:
    clock = FakeClock(NOW)
    world = World.create(MemoryStore(clock=clock), clock)
    candidate = LabCandidate(
        analyte_raw="K" if kind != "unknown" else "unlisted-assay",
        value=Quantity(raw_value="6.5", raw_unit="mmol/L"),
    )
    verdict = (
        screen_text("chest pain", policy=POLICY)
        if kind == "screen"
        else grade_bp(190, 125, policy=POLICY)
        if kind == "vital"
        else grade_lab(candidate, policy=POLICY)
    )
    facts, severity = to_incident_facts(verdict, source=SOURCE, policy=POLICY)
    assert IncidentFacts.model_validate(facts.as_payload()) == facts
    assert facts.unique_source_key == f"{SOURCE.observation_id}#{facts.rule_family}#{facts.rule_id}"
    incident = world.urgent.raise_incident(
        SCOPE, facts.unique_source_key, facts.as_payload(), severity, NOW
    )
    assert Incident.model_validate_json(incident.model_dump_json()) == incident
    assert incident.facts == facts.as_payload() and incident.severity == severity
    assert severity == ("concern" if kind == "unknown" else "danger")
    duplicate = world.urgent.raise_incident(
        SCOPE, facts.unique_source_key, facts.as_payload(), severity, NOW
    )
    assert duplicate == incident
    assert len(world.queued("DANGER")) == 1


def test_normal_verdicts_cannot_be_promoted_to_incidents_and_policy_cannot_be_relabelled() -> None:
    for verdict in (screen_text("hello", policy=POLICY), grade_bp(120, 80, policy=POLICY)):
        with pytest.raises(ValueError, match="nonincident"):
            to_incident_facts(verdict, source=SOURCE, policy=POLICY)
    other = SafetyPolicy.model_validate(
        POLICY.model_dump() | {"policy_version": "synthetic-different-policy"}
    )
    with pytest.raises(ValueError, match="versions differ"):
        to_incident_facts(screen_text("chest pain", policy=POLICY), source=SOURCE, policy=other)
    facts, severity = to_incident_facts(
        grade_bp(300, 200, policy=POLICY), source=SOURCE, policy=POLICY
    )
    assert severity == "concern" and facts.verdict.level == "implausible"
    assert (
        to_incident_facts(grade_bp(85, 55, policy=POLICY), source=SOURCE, policy=POLICY)[1]
        == "danger"
    )


def test_public_policy_is_required_and_there_is_no_downgrade_operation() -> None:
    for function in (
        screen_text,
        grade_bp,
        find_bp,
        grade_lab,
        normalize,
        validate_patient_output,
        wants_treatment_change,
        render_urgent,
        to_incident_facts,
    ):
        assert inspect.signature(function).parameters["policy"].default is inspect.Parameter.empty
    import sanad.safety as safety

    assert not any("downgrade" in name.lower() for name in safety.__all__)
    danger = screen_text("chest pain", policy=POLICY)
    with pytest.raises(ValidationError):
        danger.level = "none"  # type: ignore[misc]  # Exercise the runtime frozen guard.
    with pytest.raises(TypeError):
        screen_text("nothing dangerous", policy=POLICY, previous_verdict=danger)  # type: ignore[call-arg]


def test_pending_approval_and_unsupported_protocol_are_explicit() -> None:
    assert POLICY.approved_by == "Clinical approver of record (original Sanad build)"
    assert str(POLICY.approved_on) == "2026-08-29"
    assert (
        POLICY.cohort
        == "adult cardiology outpatients, original Sanad build; re-approval for Sanad v2 pending"
    )
    unsupported = SafetyPolicy.model_validate(
        POLICY.model_dump() | {"phrase_table_version": "missing-protocol"}
    )
    with pytest.raises(ValueError, match="missing protocol"):
        screen_text("hello", policy=unsupported)


def test_imports_are_a_leaf_and_provider_imports_are_impossible() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "sanad" / "safety"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert name.split(".")[0] in sys.stdlib_module_names | {
                    "pydantic"
                } or name.startswith(("sanad.safety", "sanad.domain")), (path, name)
    script = """
import importlib.abc
import sys
class DenyProviders(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'google', 'boto3', 'botocore', 'strands'}:
            raise AssertionError('provider import: ' + fullname)
sys.meta_path.insert(0, DenyProviders())
from sanad.safety import screen_text, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT
assert screen_text('chest pain', policy=SAFETY_POLICY_V1_CARDIOLOGY_DRAFT).level == 'danger'
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
