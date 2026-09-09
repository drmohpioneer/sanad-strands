"""Synthetic records for routed browser inspection; never imported by production."""

from datetime import timedelta
from typing import cast

from domain_fixtures import mission, review_payload
from harness import FakeClock
from store.concierge_fixtures import PatientWorld

from sanad.auth.service import revise
from sanad.domain import (
    DRAFT_POLICY_2026_09,
    EvidencePredicate,
    MissionState,
    Provenance,
    ReviewObligation,
    VersionRef,
    create_review,
)
from sanad.domain.entities import MonitorDetails, MonitorReading
from sanad.domain.language import Language
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.records import CareOrderHead, CareOrderVersion, ClinicalFact, FactPayload
from sanad.store._base import StoreBase
from sanad.store.records import OrderAuthority, Patient, from_record

LONG_NAME = "Ahmed Abdelrahman Mostafa عبد الرحمن مصطفى — synthetic long name"
LONG_DRUG = "Synthetic atorvastatin calcium extended label for wrapping verification"


def fixture(
    store: StoreBase, clock: FakeClock, *, count: int = 60, language: Language = "en"
) -> PatientWorld:
    world = cast(PatientWorld, PatientWorld.create(store, clock))
    bound = world.enroll(medication=False)
    world.seed(revise(world.doctor, clock(), language=language))
    world.seed(
        revise(
            bound,
            clock(),
            record_version=bound.record_version + 1,
            display_name=LONG_NAME,
            language=language,
            age="63",
        )
    )
    patients = [from_record(store.get(bound.scope, "patient", bound.id), Patient)]  # type: ignore[arg-type]
    for i in range(1, count):
        p = world.named_stub(f"{'Mariam مريم' if i % 2 else 'Youssef يوسف'} Synthetic {i:03d}")
        patients.append(p)
    now = clock()
    for i, p in enumerate(patients):
        m = mission(
            MissionState.blocked if i % 7 == 1 else MissionState.open,
            doctor_id=p.scope.doctor_id,
            patient_id=p.id,
            id=f"browser-request-{i}",
            title="Blood pressure متابعة الضغط",
            kind="MONITOR",
            order_refs=(),
            details=MonitorDetails(
                metric="blood pressure",
                unit="mmHg",
                slots=(now - timedelta(hours=12), now - timedelta(hours=1)),
                required_coverage=2,
                readings=(
                    MonitorReading(
                        source_ref=VersionRef(
                            entity_type="clinical_fact", id="browser-reading", version=1
                        ),
                        reading_index=0,
                        observed_at=now - timedelta(hours=12),
                        received_at=now - timedelta(hours=11),
                        slot=0,
                        value="128/82",
                    ),
                )
                if i == 0
                else (),
            ),
            objective_predicate=EvidencePredicate(evaluator="monitor"),
        )
        world.seed(m)
        created = create_review(
            review_payload(
                event_id=f"browser-review-{i}",
                owner_doctor_id=p.scope.doctor_id,
                patient_id=p.id,
                source_id=m.id,
                source_mission_id=m.id,
                review_kind="incident_response" if i == 0 else "result_review",
                review_at=now + timedelta(hours=i - 3),
            ),
            now - timedelta(days=2),
            DRAFT_POLICY_2026_09,
        ).aggregate
        assert isinstance(created, ReviewObligation)
        changes: dict[str, object] = {
            "first_notice_at": now - timedelta(days=1),
            "last_material_change_version": 3 if i % 4 == 0 else 2,
        }
        if i % 3 == 1:
            changes.update(
                state="acknowledged",
                acknowledged_by=world.owner.subject,
                acknowledged_at=now - timedelta(hours=1),
            )
        if i % 3 == 2:
            changes.update(
                state="resolved",
                work_clock=None,
                resolved_by=world.owner.subject,
                resolved_at=now,
                resolved_reason="Synthetic review completed by doctor",
                resolved_action_event_id="browser-resolution",
            )
        world.seed(ReviewObligation.model_validate(created.model_dump() | changes))
    source = Provenance(
        source_observation_id="browser-doctor-statement",
        actor_kind="doctor",
        actor_id=world.owner.subject,
        source_kind="doctor_statement",
        received_at=now,
    )
    world.seed(
        ClinicalFact(
            id="browser-history",
            scope=bound.scope,
            category="history",
            payload=FactPayload(text="Synthetic history: متابعة after visit"),
            provenance=source,
            created_at=now,
            updated_at=now,
        )
    )
    order = CareOrderVersion(
        id="browser-order:1",
        order_id="browser-order",
        order_version=1,
        scope=bound.scope,
        type="medication",
        structured_instruction=OrderCandidate(
            action="start", drug=LONG_DRUG, dose="40 mg", frequency="once daily", timing="at night"
        ),
        provenance=source,
        confirmed_by=world.owner.subject,
        confirmed_at=now,
        created_at=now,
        updated_at=now,
    )
    world.seed(order)
    world.seed(
        CareOrderHead(
            id="browser-order",
            order_id="browser-order",
            scope=bound.scope,
            current_order_version=1,
            current_version_id=order.id,
            type="medication",
            name=LONG_DRUG,
            status="active",
            changed_by=world.owner.subject,
            changed_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    world.seed(
        OrderAuthority(
            id="browser-order", scope=bound.scope, status="active", created_at=now, updated_at=now
        )
    )
    return world
