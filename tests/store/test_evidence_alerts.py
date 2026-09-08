"""Decision 019 at all three production entry points, including context and refusal."""

from datetime import timedelta

import pytest
from harness import FakeClock

from sanad.store._base import StoreBase
from sanad.store.records import Incident, OutboundIntent, from_record
from store import evidence_fixtures as f
from store import photo_fixtures
from store.account_fixtures import PATIENT, update
from store.concierge_fixtures import PatientWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> PatientWorld:
    return f.world(store, clock)


@pytest.mark.parametrize("route", ["doctor_photo", "patient_reading", "patient_evidence"])
@pytest.mark.parametrize("value", ["5.6", "6.3"])
def test_alert_floor_every_route_one_incident(world: PatientWorld, route: str, value: str) -> None:
    f.alert(world)
    if route == "doctor_photo":
        photo_fixtures.providers(world, f.lab(value), f.lab(value))
        assert world.post(photo_fixtures.photo("Synthetic Patient", id=1100)).status_code == 200
    elif route == "patient_reading":
        assert world.post(update(PATIENT, f"Potassium {value} mmol/L", 1100)).status_code == 200
        assert any(r.body["category"] == "patient_report" for r in world.rows("clinical_fact"))
    else:
        f.mission(world)
        f.providers(world, f.lab(value), f.lab(value))
        assert f.upload(world) == "accepted"
        assert world.rows("mission")[0].body["state"] == "fulfilled"
        assert not any(
            r.body.get("notification_purpose") == "DONE:FULFILLMENT"
            for r in world.rows("outbound_intent")
        )
    incidents = [from_record(r, Incident) for r in world.rows("incident")]
    assert len(incidents) == 1
    assert incidents[0].severity == ("patient_alert" if value == "5.6" else "danger")
    facts = incidents[0].facts
    verdict = facts["verdict"]
    assert isinstance(verdict, dict) and verdict["level"] == (
        "normal" if value == "5.6" else "critical"
    )
    assert facts["patient_alerts"]
    danger = [
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body.get("notification_purpose") == "DANGER"
    ]
    assert len(danger) == 1
    rendered = world.runtime.patient_payload(danger[0])["text"]
    assert "5.5" in str(rendered)
    if route == "patient_evidence":
        assert "وصل المستند المطلوب" in str(rendered)


def test_critical_mismatch_rejection_duplicate_never_suppresses_danger(world: PatientWorld) -> None:
    f.mission(world)
    f.alert(world)
    value = f.lab("6.3", printed_name="Foreign Person")
    vision, _, _ = f.providers(world, value, value)
    assert f.upload(world) == "accepted"
    assert len(world.rows("incident")) == 1
    incident = from_record(world.rows("incident")[0], Incident)
    assert incident.verified_status == "unverified"
    card = next(
        i for i in world.patient_intents() if i.template_id == "patient_evidence_name_check"
    )
    world.press(card, index=1)
    assert f.current(world).association_state == "rejected"
    assert f.upload(world, 1101) == "accepted"
    assert len(world.rows("incident")) == 1 and len(vision.calls) == 2


@pytest.mark.parametrize(
    "threshold,unit,status,expected",
    [
        ("6.5", "mmol/L", "active", "ignored"),
        ("5.5", "bananas", "active", "mismatch"),
        ("5.5", None, "active", "mismatch"),
        ("5.5", "mmol/L", "stopped", "ignored"),
    ],
)
def test_invalid_alerts_cannot_lower_kernel(
    world: PatientWorld, threshold: str, unit: str | None, status: str, expected: str
) -> None:
    f.mission(world)
    f.alert(world, threshold, unit=unit, status=status)
    f.providers(world, f.lab("5.6"), f.lab("5.6"))
    assert f.upload(world) == "accepted"
    assert not world.rows("incident")
    assert ("alert_unit_mismatch" in f.current(world).flags) == (expected == "mismatch")


def test_looser_alert_refused_on_real_dictation_card(world: PatientWorld) -> None:
    text = "Synthetic Patient بلغني لو البوتاسيوم فوق 6.5 mmol/L"
    proposal = world.dictate(
        text,
        {
            "intent": "update_record",
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "alerts": ["البوتاسيوم فوق 6.5 mmol/L"],
            "numbers_used": ["6.5"],
        },
        id=440,
    )
    assert proposal.blocked("alert:0")
    assert any(i.question and "أضعف" in i.question for i in proposal.issues)
    world.tap(id=441)
    assert not world.rows("care_order_head")


def test_context_active_orders_conditions_previous_reading(world: PatientWorld) -> None:
    from sanad.domain import Provenance
    from sanad.scribe.extract import OrderCandidate
    from sanad.scribe.records import CareOrderHead, CareOrderVersion, ClinicalFact, FactPayload

    world.send("Potassium 4.9 mmol/L")
    world.clock.advance(timedelta(days=1))
    now = world.clock()
    source = Provenance(
        source_observation_id="synthetic-condition",
        actor_kind="doctor",
        actor_id=world.owner.subject,
        source_kind="doctor_statement",
        received_at=now,
    )
    world.seed(
        ClinicalFact(
            id="condition",
            scope=world.patient_scope,
            category="condition",
            payload=FactPayload(text="Known synthetic condition"),
            provenance=source,
            created_at=now,
            updated_at=now,
        )
    )
    world.seed(
        CareOrderVersion(
            id="medication:1",
            order_id="medication",
            order_version=1,
            type="medication",
            structured_instruction=OrderCandidate(
                action="start", drug="Synthetic medicine", dose="5 mg", frequency="daily"
            ),
            provenance=source,
            confirmed_by=world.owner.subject,
            confirmed_at=now,
            scope=world.patient_scope,
            created_at=now,
            updated_at=now,
        )
    )
    world.seed(
        CareOrderHead(
            id="medication",
            order_id="medication",
            current_order_version=1,
            current_version_id="medication:1",
            type="medication",
            name="Synthetic medicine",
            status="active",
            changed_by=world.owner.subject,
            changed_at=now,
            scope=world.patient_scope,
            created_at=now,
            updated_at=now,
        )
    )
    f.alert(world)
    f.providers(world, f.lab("5.6"), f.lab("5.6"))
    assert f.upload(world) == "accepted"
    incident = from_record(world.rows("incident")[0], Incident)
    context = incident.facts["context"]
    assert isinstance(context, dict) and context["conditions"] == ["Known synthetic condition"]
    assert context["medications"] == [
        {"drug": "Synthetic medicine", "dose": "5 mg", "frequency": "daily"}
    ]
    previous = context["previous"]
    assert (
        isinstance(previous, dict)
        and previous["value"] == "4.9"
        and previous["source"] == "patient_report"
    )
