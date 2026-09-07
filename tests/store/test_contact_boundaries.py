"""Isolation, re-read races, template validation and real concurrent CAS scenarios."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from harness import FakeClock

from sanad.auth.service import revise
from sanad.contact import bundle
from sanad.contact.templates import patient_text, render
from sanad.domain import FollowUpTask, Mission, ReviewKind, ReviewObligation
from sanad.domain.entities import MonitorDetails
from sanad.store._base import StoreBase
from sanad.store.records import BundleSchedule, OperationalClock, from_record
from store.account_fixtures import AccountWorld
from store.concierge_fixtures import PatientWorld
from store.contact_fixtures import add_mission, dispatch, required, tick, world
from store.test_contact_outage_bundle import bundle_intent, deadline


def resolve(w: PatientWorld, review: ReviewObligation) -> None:
    w.seed(
        revise(
            review,
            w.clock(),
            state="resolved",
            resolved_by=w.doctor.id,
            resolved_at=w.clock(),
            resolved_reason="done",
            resolved_action_event_id="resolve:" + review.id,
            work_clock=None,
        )
    )


def test_bundle_two_doctors_reread_and_twenty_line_cap(store: StoreBase, clock: FakeClock) -> None:
    clock.now = clock().replace(hour=7)
    w = world(store, clock)
    first = deadline(w, clock)
    # Synthetic second tenant has separate review, recipient and schedule.
    other = AccountWorld.approve(w, "20003")
    foreign = first.model_copy(
        update={
            "id": "foreign-review",
            "owner_doctor_id": other.id,
            "patient_id": None,
            "source_type": "outbound_intent",
            "review_kind": ReviewKind.delivery_failure,
            "source_id": "foreign",
        }
    )
    from sanad.domain.entities import review_source_key

    foreign = foreign.model_copy(
        update={
            "unique_source_key": review_source_key(
                other.id, "outbound_intent", "foreign", foreign.source_version, foreign.review_kind
            )
        }
    )
    w.seed(foreign)
    at = clock() + timedelta(days=7)
    w.seed(
        BundleSchedule(
            id=other.id,
            scope=other.scope,
            doctor_id=other.id,
            next_action_at=at,
            work_clock=OperationalClock(work_lane="bundle", next_action_at=at),
            created_at=clock(),
            updated_at=clock(),
        )
    )
    # 22 eligible local reviews, one resolved during the gap from wake to send.
    for index in range(21):
        copy = first.model_copy(
            update={
                "id": f"review-{index:02}",
                "source_id": f"source-{index:02}",
                "unique_source_key": review_source_key(
                    first.owner_doctor_id,
                    first.source_type,
                    f"source-{index:02}",
                    first.source_version,
                    first.review_kind,
                ),
            }
        )
        w.seed(copy)
    clock.now = at
    schedule = required(store.get(w.doctor.scope, "bundle_schedule", w.doctor.id))
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: bundle.wake(w.runtime.steward, schedule), range(2)))
    intent = bundle_intent(w)
    resolve(w, first)
    sent = dispatch(w, intent)
    assert sent.status == "provider_accepted"
    text = str(w.transport.calls[-1].payload["text"])
    assert text.count("مطلوب لم يكتمل") == 20 and "و 1 بنود تانية" in text
    assert "مشكلة توصيل رسالة" not in text
    tick(w)
    messages = [
        c
        for c in w.transport.calls
        if "بنود لسه" in str(c.payload) or "Items still needing follow-up" in str(c.payload)
    ]
    assert len(messages) == 2
    foreign_message = next(c for c in messages if c.recipient_ref == other.private_chat_id)
    assert "Message delivery problem" in str(foreign_message.payload)
    assert "days since first notice" in str(foreign_message.payload)
    assert "Synthetic Patient" not in str(foreign_message.payload)


def test_empty_suppressed_bundle_rearms_for_material_new_source(
    store: StoreBase, clock: FakeClock
) -> None:
    clock.now = clock().replace(hour=7)
    w = world(store, clock)
    old = deadline(w, clock)
    clock.now += timedelta(days=7)
    bundle.wake(
        w.runtime.steward, required(store.get(w.doctor.scope, "bundle_schedule", w.doctor.id))
    )
    intent = bundle_intent(w)
    resolve(w, required(store.get_review(w.patient_scope, old.id)))
    assert dispatch(w, intent).suppression_reason == "bundle_empty"
    clock.now += timedelta(minutes=1)
    tick(w)
    schedule = from_record(
        required(store.get(w.doctor.scope, "bundle_schedule", w.doctor.id)), BundleSchedule
    )
    assert schedule.work_clock is None and schedule.last_provider_accepted_at is None
    # A material change has a new obligation source/version and its own first notice.
    newer = deadline(w, clock, "new-source")
    assert newer.first_notice_at == clock() and newer.id != old.id
    clock.now += timedelta(days=6, hours=23, minutes=59)
    tick(w)
    assert not [c for c in w.transport.calls if "بنود لسه" in str(c.payload)]
    clock.now += timedelta(minutes=1)
    tick(w)
    assert len([c for c in w.transport.calls if "بنود لسه" in str(c.payload)]) == 1


@pytest.mark.parametrize("language", ["ar", "en"])
def test_all_patient_templates_validate_with_only_record_numbers(
    store: StoreBase, clock: FakeClock, language: str
) -> None:
    clock.now = clock().replace(hour=7)
    w = world(store, clock, medication=True)
    patient = required(w.claims.patient(w.patient_scope.doctor_id, w.patient_scope.patient_id))
    patient = patient.model_copy(update={"language": language})
    medication = from_record(w.rows("mission")[0], Mission)
    text = patient_text(store, medication, patient, "patient_chase_medication_start")
    assert "40" not in text
    w.send("بدأت الدوا")
    task = from_record(w.rows("followup")[0], FollowUpTask)
    assert "3" in patient_text(store, task, patient, "patient_day3_prompt")
    task_mission = add_mission(w)
    for key in ("test", "visit", "task", "send_records"):
        assert patient_text(store, task_mission, patient, "patient_chase_" + key)
    monitor = add_mission(
        w,
        "monitor",
        kind="MONITOR",
        details=MonitorDetails(metric="الضغط", unit="mmHg", slots=(clock(),), required_coverage=1),
    )
    assert patient_text(store, monitor, patient, "patient_monitor_prompt")
    assert render("doctor_weekly_bundle", language, lines="Synthetic")
    with pytest.raises(ValueError, match="contact_template_fields"):
        render("patient_chase_test", language, title="missing due")
    bad = task_mission.model_copy(update={"title": "متقلقش"})
    with pytest.raises(ValueError, match="contact_template_validation"):
        patient_text(
            store, bad, patient.model_copy(update={"language": "ar"}), "patient_chase_test"
        )
    with pytest.raises(ValueError, match="contact_template_validation"):
        patient_text(
            store,
            task_mission.model_copy(update={"title": "40 mg"}),
            patient.model_copy(update={"language": "en"}),
            "patient_chase_test",
        )


@pytest.mark.parametrize(
    "confirmed,due,chases",
    [
        (
            "2026-10-20T06:00+00:00",
            "2026-11-03T06:00+00:00",
            ("2026-10-20T07:00+00:00", "2026-10-27T07:00+00:00", "2026-11-02T08:00+00:00"),
        ),
        (
            "2026-04-20T06:00+00:00",
            "2026-05-04T06:00+00:00",
            ("2026-04-20T08:00+00:00", "2026-04-27T07:00+00:00", "2026-05-03T07:00+00:00"),
        ),
    ],
)
def test_both_cairo_dst_ladders_keep_clinical_instants(
    store: StoreBase, clock: FakeClock, confirmed: str, due: str, chases: tuple[str, ...]
) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from store.contact_fixtures import current

    clock.now = datetime.fromisoformat(confirmed)
    w = world(store, clock)
    m = add_mission(
        w, due=datetime.fromisoformat(due), due_source="doctor", original_time_expression=due
    )
    for count, instant in enumerate(chases, 1):
        clock.now = datetime.fromisoformat(instant)
        tick(w)
        assert current(w, m).contact_count == count
        assert clock().astimezone(ZoneInfo("Africa/Cairo")).hour == 10
        assert current(w, m).due_at == m.due_at


def test_reply_between_second_and_third_chase_preserves_ladder(
    store: StoreBase, clock: FakeClock
) -> None:
    from store.contact_fixtures import current

    clock.now = clock().replace(hour=7)
    w = world(store, clock)
    m = add_mission(w)
    tick(w)
    clock.now += timedelta(days=7)
    tick(w)
    assert current(w, m).unanswered_delivered_count == 2
    w.send("تمام")
    assert current(w, m).unanswered_delivered_count == 0
    clock.now += timedelta(days=6)
    tick(w)
    assert current(w, m).contact_count == 3
    assert current(w, m).unanswered_delivered_count == 1
    assert current(w, m).state == "waiting_patient"
