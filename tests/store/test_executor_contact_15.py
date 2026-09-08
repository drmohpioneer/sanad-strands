"""Visit brief scheduling, unsupported task policy, and bounded question listings."""

from datetime import timedelta
from typing import Any

import pytest
from domain_fixtures import NOW, mission
from harness import FakeClock

from sanad.auth.service import revise
from sanad.contact.scheduler import schedule
from sanad.domain import Mission, PatientReportPredicate
from sanad.domain.entities import SendRecordsDetails, TaskDetails
from sanad.scribe.card import render_card
from sanad.store._base import StoreBase
from sanad.store.records import Consent, OutboundIntent, from_record, to_record
from store.concierge_fixtures import PatientWorld
from store.contact_fixtures import dispatch
from store.executors_15_fixtures import add, doctor, get, question, world
from store.test_executor_recovery_15 import command


def briefs(w: PatientWorld) -> list[OutboundIntent]:
    return [i for i in w.patient_intents() if i.template_id == "patient_visit_brief"]


def test_visit_brief_content_clock_and_one_per_generation(
    store: StoreBase, clock: FakeClock
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    m = add(w, due_at=clock() + timedelta(days=1))
    for id, kind, days in [
        ("CBC", "TEST", 0),
        ("old-record", "SEND_RECORDS", 1),
        ("later-test", "TEST", 2),
    ]:
        fields: dict[str, Any] = (
            {
                "kind": kind,
                "details": SendRecordsDetails(categories=("Old report",), required_count=1),
            }
            if kind == "SEND_RECORDS"
            else {}
        )
        w.seed(
            mission(
                id=id,
                title=id,
                doctor_id=m.doctor_id,
                patient_id=m.patient_id,
                due_at=clock() + timedelta(days=days),
                escalation_at=clock() + timedelta(days=days),
                order_refs=(),
                **fields,
            )
        )
    other = w.named_stub("Other synthetic patient")
    w.seed(
        mission(
            id="foreign-test",
            title="Other patient's test",
            doctor_id=m.doctor_id,
            patient_id=other.id,
            due_at=clock(),
            escalation_at=clock(),
            order_refs=(),
        )
    )
    result = schedule(w.runtime.steward, to_record(m, w.patient_scope))
    assert result.status == "accepted", result
    assert len(briefs(w)) == 1
    sent = dispatch(w, briefs(w)[0])
    assert sent.status == "provider_accepted"
    payload = w.transport.calls[-1].payload
    assert "Cardiology" in str(payload) and "CBC" in str(payload) and "old-record" in str(payload)
    assert "later-test" not in str(payload) and "Other patient's test" not in str(payload)
    assert get(w, m).due_at == m.due_at
    schedule(w.runtime.steward, to_record(get(w, m), w.patient_scope))
    assert len(briefs(w)) == 1


def test_new_deadline_generation_gets_a_new_brief(store: StoreBase, clock: FakeClock) -> None:
    from sanad.domain.deadlines import ExplicitTiming

    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    m = add(w, due_at=clock() + timedelta(days=1))
    assert schedule(w.runtime.steward, to_record(m, w.patient_scope)).status == "accepted"
    assert dispatch(w, briefs(w)[0]).status == "provider_accepted"
    timing = ExplicitTiming(
        instant=clock() + timedelta(days=4),
        timezone="Africa/Cairo",
        original_expression="Four days",
    )
    assert (
        command(
            w,
            get(w, m),
            "ExtendMission",
            timing=timing.model_dump(mode="json"),
            reason="Moved deadline",
        ).status
        == "accepted"
    )
    clock.advance(timedelta(days=3))
    assert schedule(w.runtime.steward, to_record(get(w, m), w.patient_scope)).status == "accepted"
    assert len(briefs(w)) == 2
    assert len({i.source_event_ids[0] for i in briefs(w)}) == 2


@pytest.mark.parametrize("mode", ["cancel", "window_passed", "stop"])
def test_queued_brief_is_suppressed_when_ineligible(
    store: StoreBase, clock: FakeClock, mode: str
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    m = add(w, due_at=clock() + timedelta(days=1))
    assert schedule(w.runtime.steward, to_record(m, w.patient_scope)).status == "accepted"
    intent = briefs(w)[0]
    current = get(w, m)
    if mode == "cancel":
        assert command(w, current, "CancelMission", reason="Doctor cancelled").status == "accepted"
    elif mode == "window_passed":
        w.seed(
            revise(
                current,
                clock(),
                details=current.details.model_copy(
                    update={
                        "window_start": clock() - timedelta(hours=1),
                        "window_end": clock() + timedelta(hours=1),
                    }
                ),
            )
        )
    else:
        w.seed(revise(w.profile, clock(), routine_contact_enabled=False))
    assert dispatch(w, intent).status == "suppressed"


def test_brief_moves_out_of_quiet_hours(store: StoreBase, clock: FakeClock) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    row = w.rows("consent")[0]
    consent = from_record(row, Consent)
    w.seed(revise(consent, clock(), quiet_hours=("22:00", "12:00")))
    m = add(w, due_at=clock() + timedelta(days=1))
    assert schedule(w.runtime.steward, to_record(m, w.patient_scope)).status == "accepted"
    assert not briefs(w)
    clock.advance(timedelta(hours=2))
    assert schedule(w.runtime.steward, to_record(get(w, m), w.patient_scope)).status == "accepted"
    assert len(briefs(w)) == 1
    assert dispatch(w, briefs(w)[0]).status == "provider_accepted"


@pytest.mark.parametrize(
    "language,text,expected",
    [
        ("en", "order a taxi", "not supported: recorded as a request only"),
        ("ar", "احجزله ميعاد", "مش مدعوم: هيتسجل كطلب بس"),
    ],
)
def test_unsupported_card_and_compiler_share_policy_without_patient_chase(
    store: StoreBase, clock: FakeClock, language: str, text: str, expected: str
) -> None:
    w = world(store, clock)
    if language == "ar":
        w.seed(revise(w.doctor, clock(), language="ar"))
    p = w.dictate(
        "Synthetic Patient " + text,
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "missions": [{"kind": "TASK", "text": text}],
        },
        id=400,
    )
    assert expected in "\n".join(render_card(p))
    w.tap("✅ Confirm" if language == "en" else "✅ تمام", id=401)
    m = from_record(next(r for r in w.rows("mission") if r.body["kind"] == "TASK"), Mission)
    assert isinstance(m.details, TaskDetails) and isinstance(
        m.objective_predicate, PatientReportPredicate
    )
    assert m.details.completion_rule == "unsupported_action"
    assert m.objective_predicate.report_kind == "doctor_task"
    assert schedule(w.runtime.steward, to_record(m, w.patient_scope)).status == "accepted"
    assert not [i for i in w.patient_intents() if i.notification_purpose == "routine_prompt"]
    clock.now = m.escalation_at
    from store.test_executor_recovery_15 import sweep

    sweep(w)
    assert get(w, m).state == "overdue"
    assert (
        len([r for r in w.rows("outbound_intent") if r.body["notification_purpose"] == "DEADLINE"])
        == 1
    )
    model, reply = w.send("done" if language == "en" else "خلصت")
    assert not model.script.calls and reply.template_id == "patient_task_recorded"
    assert get(w, m).state == "fulfilled"


def test_two_hundred_questions_page_and_stable_mapping(store: StoreBase, clock: FakeClock) -> None:
    w = world(store, clock)
    m = question(w)
    for index in range(199):
        w.seed(m.model_copy(update={"id": f"ticket-{index:03d}"}))
    listing = doctor(w, "/questions")
    assert listing.payload and len(str(listing.payload["text"])) < 3500
    assert "page 1/34" in str(listing.payload["text"])
    assert len(listing.question_listing_targets) == 6 and listing.question_listing_targets[0] == (
        m.patient_id,
        m.id,
    )
    second = doctor(w, "/questions 2", 3001)
    assert len(second.question_listing_targets) == 6
    assert not set(second.question_listing_targets) & set(listing.question_listing_targets)
    # A newly inserted older ticket must not renumber the already shown page.
    w.seed(
        m.model_copy(
            update={"id": "new-earliest", "created_at": m.created_at - timedelta(seconds=1)}
        )
    )
    target = second.question_listing_targets[0][1]
    # The fixture creates a matching review by reusing the accepted review shape.
    from sanad.domain import ReviewObligation

    original = from_record(w.rows("review")[0], ReviewObligation)
    from sanad.domain.entities import review_source_key

    review = original.model_copy(
        update={
            "id": "page-two-review",
            "source_id": target,
            "source_mission_id": target,
            "unique_source_key": review_source_key(
                m.doctor_id, "mission", target, 1, original.review_kind
            ),
        }
    )
    w.seed(review)
    result = doctor(w, "/answer 1 Bring your diary.", 3002)
    assert result.template_id == "doctor_question_recorded", result.payload
    changed = w.store.get(w.patient_scope, "mission", target)
    assert changed and changed.body["state"] == "fulfilled"
    assert get(w, m).state == "open"
