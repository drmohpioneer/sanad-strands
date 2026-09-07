"""Synthetic evidence through actual receipt, media, reader and Steward boundaries."""

from typing import cast

from domain_fixtures import mission as base_mission
from harness import FakeClock
from providers.fixtures import (
    FakeS3,
    FakeTelegramFiles,
    ScriptedConverter,
    ScriptedVision,
    document,
    png,
)

from sanad.domain import EvidencePredicate, Mission, Principal, TestDetails
from sanad.media.retrieve import MediaRetriever
from sanad.media.vision import VisionAdapter
from sanad.store._base import StoreBase
from sanad.store.records import Evidence, EvidenceHead, InboundReceipt, MediaWork, from_record
from store.concierge_fixtures import PatientWorld
from store.test_concierge_media import media_message


def world(store: StoreBase, clock: FakeClock) -> PatientWorld:
    value = cast(PatientWorld, PatientWorld.create(store, clock))
    value.enroll(medication=False)
    return value


def mission(world: PatientWorld, id: str = "potassium-test", **changes: object) -> Mission:
    value = base_mission(
        doctor_id=world.patient_scope.doctor_id,
        patient_id=world.patient_scope.patient_id,
        id=id,
        title="تحليل البوتاسيوم",
        details=TestDetails(analytes=("Potassium",), completeness="all"),
        objective_predicate=EvidencePredicate(evaluator="test"),
    )
    value = Mission.model_validate(value.model_dump() | changes)
    world.seed(value)
    return value


def lab(value: str = "5.0", **changes: object) -> str:
    return document(
        **(
            {
                "printed_name": "Synthetic Patient",
                "items": [{"name": "Potassium", "value": value, "unit": "mmol/L"}],
            }
            | changes
        )
    )


def providers(
    world: PatientWorld, *replies: str, data: bytes | None = None
) -> tuple[ScriptedVision, FakeTelegramFiles, FakeS3]:
    vision, files, s3 = (
        ScriptedVision(*(replies or (lab(), lab()))),
        FakeTelegramFiles(data or png()),
        FakeS3(),
    )

    def media(receipt: InboundReceipt, actor: Principal) -> MediaRetriever:
        auth = world.store.authorize(world.runtime.settings.bot_id, actor.subject)
        assert auth.binding
        binding = auth.binding
        return MediaRetriever(
            world.runtime.steward,
            s3,
            files,
            ScriptedConverter(),
            world.patient_scope,
            actor,
            lambda: world.concierge.valid(actor, binding),
            world.runtime.steward.policy_provider(world.patient_scope),
        )

    world.concierge.media_factory = media
    world.concierge.vision_factory = lambda source: VisionAdapter(
        vision, source, world.runtime.safety_policy
    )
    return vision, files, s3


def upload(world: PatientWorld, id: int = 1100, caption: str = "") -> str:
    assert world.post(media_message("photo", id, caption)).status_code == 200
    receipt = world.receipt(id)
    assert receipt.state == "completed"
    auth = world.store.authorize(world.runtime.settings.bot_id, receipt.source_subject)
    return world.concierge.evidence.run(receipt, auth.principal)


def current(world: PatientWorld) -> Evidence:
    head = from_record(world.rows("evidence_head")[-1], EvidenceHead)
    row = world.store.get(world.patient_scope, "evidence", f"{head.id}:{head.current_version}")
    assert row
    return from_record(row, Evidence)


def work(world: PatientWorld) -> MediaWork:
    return from_record(world.rows("media_work")[-1], MediaWork)


def alert(
    world: PatientWorld,
    threshold: str = "5.5",
    *,
    unit: str | None = "mmol/L",
    status: str = "active",
) -> None:
    from sanad.domain import Provenance
    from sanad.scribe.records import CareOrderHead, CareOrderVersion, ValueAlert

    now = world.clock()
    source = Provenance(
        source_observation_id="synthetic-alert-order",
        actor_kind="doctor",
        actor_id=world.owner.subject,
        source_kind="doctor_statement",
        received_at=now,
    )
    world.seed(
        CareOrderVersion(
            id="synthetic-alert:1",
            order_id="synthetic-alert",
            order_version=1,
            scope=world.patient_scope,
            type="value_alert",
            structured_instruction=ValueAlert(
                text="Synthetic potassium alert",
                metric="Potassium",
                comparator="ge",
                threshold=threshold,
                unit=unit,
            ),
            provenance=source,
            confirmed_by=world.owner.subject,
            confirmed_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    world.seed(
        CareOrderHead.model_validate(
            {
                "id": "synthetic-alert",
                "order_id": "synthetic-alert",
                "scope": world.patient_scope,
                "current_order_version": 1,
                "current_version_id": "synthetic-alert:1",
                "type": "value_alert",
                "name": "Potassium",
                "status": status,
                "changed_by": world.owner.subject,
                "changed_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
    )


def stopped_medication(world: PatientWorld) -> None:
    from sanad.domain import Provenance
    from sanad.scribe.extract import OrderCandidate
    from sanad.scribe.records import CareOrderHead, CareOrderVersion

    now = world.clock()
    source = Provenance(
        source_observation_id="synthetic-stopped-order",
        actor_kind="doctor",
        actor_id=world.owner.subject,
        source_kind="doctor_statement",
        received_at=now,
    )
    world.seed(
        CareOrderVersion(
            id="old-med:1",
            order_id="old-med",
            order_version=1,
            type="medication",
            scope=world.patient_scope,
            structured_instruction=OrderCandidate(
                action="stop", drug="Bisoprolol", dose="5 mg", frequency="daily"
            ),
            provenance=source,
            confirmed_by=world.owner.subject,
            confirmed_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    world.seed(
        CareOrderHead(
            id="old-med",
            order_id="old-med",
            current_order_version=1,
            current_version_id="old-med:1",
            type="medication",
            scope=world.patient_scope,
            name="Bisoprolol",
            status="stopped",
            changed_by=world.owner.subject,
            changed_at=now,
            created_at=now,
            updated_at=now,
        )
    )
