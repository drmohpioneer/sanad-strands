"""Contract 15 uses the existing lifecycle table for all three mission kinds."""

from datetime import datetime, timedelta

import pytest
from domain_fixtures import NOW, POLICY, mission, predicate_result

from sanad.concierge.tasks import is_task_done
from sanad.concierge.visits import booking_window, is_attended, is_booked, not_attended
from sanad.domain import (
    Mission,
    ObservationRef,
    TransitionRejected,
    TransitionResult,
    transition_mission,
)
from sanad.domain import events as ev
from sanad.domain.deadlines import ExplicitTiming
from sanad.domain.entities import QuestionDetails, TaskDetails, VisitDetails
from sanad.domain.predicates import DoctorAnswerPredicate, PatientReportPredicate


@pytest.mark.parametrize("kind", ["VISIT", "TASK", "QUESTION"])
def test_complete_lifecycle_through_shared_table(kind: str) -> None:
    details = {
        "VISIT": VisitDetails(objective="attendance_reported"),
        "TASK": TaskDetails(
            category="doctor_request", instruction="Bring diary", completion_rule="patient_report"
        ),
        "QUESTION": QuestionDetails(
            question_text="A question",
            source_observation_ref=ObservationRef(observation_id="received"),
        ),
    }[kind]
    m = mission(
        kind=kind,
        details=details,
        due_at=NOW + timedelta(days=1),
        escalation_at=NOW + timedelta(days=1),
        order_refs=(),
        objective_predicate=DoctorAnswerPredicate()
        if kind == "QUESTION"
        else PatientReportPredicate(report_kind=kind),
    )

    def apply(source: Mission, event: ev.MissionEvent, at: datetime = NOW) -> TransitionResult:
        result = transition_mission(source, event, at, POLICY)
        assert isinstance(result, TransitionResult), result
        return result

    completed = None
    for hours, timeliness in [(0, "on_time"), (25, "late")]:
        at = NOW + timedelta(hours=hours)
        completed = apply(
            m,
            ev.ObjectiveFulfilled(
                event_id="done",
                fulfillment_event_id="done",
                actor_kind="doctor" if kind == "QUESTION" else "patient",
                predicate_result=predicate_result(at=at),
                objective_received_at=at,
                danger_flag=False,
            ),
            at,
        ).aggregate
        assert (
            isinstance(completed, Mission)
            and completed.state == "fulfilled"
            and completed.timeliness == timeliness
        )
    overdue = apply(m, ev.DeadlineReached(event_id="deadline"), m.escalation_at)
    assert overdue.aggregate.state == "overdue"
    assert any(isinstance(e, ev.EmitIntent) and e.purpose == "DEADLINE" for e in overdue.effects)
    assert isinstance(overdue.aggregate, Mission)
    due = NOW + timedelta(days=4)
    timing = ExplicitTiming(instant=due, timezone="Africa/Cairo", original_expression="four days")
    extended = apply(
        overdue.aggregate,
        ev.DoctorExtend(
            event_id="extend",
            actor_id="doctor",
            timing=timing,
            reason="More time",
            consent_active=True,
        ),
        NOW + timedelta(days=2),
    )
    assert isinstance(extended.aggregate, Mission)
    assert extended.aggregate.due_at == due and extended.aggregate.escalation_at == due
    assert any(
        isinstance(e, ev.ResolveReviewEffect) and e.review_kind == "unmet_objective"
        for e in extended.effects
    )
    cancelled = apply(
        m,
        ev.DoctorCancel(
            event_id="cancel", actor_id="doctor", reason="Cancelled", question_review_open=False
        ),
    )
    assert cancelled.aggregate.state == "cancelled"
    closed = apply(
        m,
        ev.DoctorCloseUnfulfilled(
            event_id="close", actor_id="doctor", reason="Closed", open_incident=False
        ),
    )
    assert closed.aggregate.state == "closed_unfulfilled"
    assert isinstance(completed, Mission)
    reopened = apply(
        completed,
        ev.DoctorReopen(
            event_id="reopen",
            actor_id="doctor",
            timing=timing,
            reason="Repeat",
            consent_active=True,
            new_objective_predicate=m.objective_predicate,
            new_order_refs=(),
            current_active_order_refs=(),
        ),
        NOW + timedelta(days=2),
    )
    assert isinstance(reopened.aggregate, Mission)
    assert reopened.aggregate.state == "open" and reopened.aggregate.deadline_generation == 2
    if kind == "QUESTION":
        assert isinstance(
            transition_mission(
                m, ev.DoctorCancel(event_id="cancel", actor_id="doctor", reason="No"), NOW, POLICY
            ),
            TransitionRejected,
        )
        assert isinstance(
            transition_mission(
                m,
                ev.ObjectiveFulfilled(
                    event_id="patient",
                    fulfillment_event_id="patient",
                    actor_kind="patient",
                    predicate_result=predicate_result(),
                    objective_received_at=NOW,
                    danger_flag=False,
                ),
                NOW,
                POLICY,
            ),
            TransitionRejected,
        )


@pytest.mark.parametrize(
    "text,booked,attended,absent,done",
    [
        ("booked", True, False, False, False),
        ("appointment on Thursday", True, False, False, False),
        ("حجزت ورحت", True, True, False, False),
        ("went to cardiology", False, True, False, False),
        ("قابلت الدكتور", False, True, False, False),
        ("couldn't go", False, False, True, False),
        ("مرحتش", False, False, True, False),
        ("اتأجل", False, False, True, False),
        ("not booked", False, False, False, False),
        ("I didn't attend", False, False, False, False),
        ("done", False, False, False, True),
        ("finished", False, False, False, True),
        ("خلصت", False, False, False, True),
        ("نفذت", False, False, False, True),
        ("not done", False, False, False, False),
        ("لسه مخلصتش", False, False, False, False),
        ("Should I say done?", False, False, False, False),
    ],
)
def test_recognizers_preserve_negation(
    text: str, booked: bool, attended: bool, absent: bool, done: bool
) -> None:
    assert (is_booked(text), is_attended(text), not_attended(text), is_task_done(text)) == (
        booked,
        attended,
        absent,
        done,
    )


@pytest.mark.parametrize(
    "text,date",
    [
        ("حجزت يوم الخميس", "2026-09-09"),
        ("حجزت بعد بكرة", "2026-09-07"),
        ("booked 2026-09-08", "2026-09-07"),
    ],
)
def test_explicit_booking_day_uses_cairo_midnight(text: str, date: str) -> None:
    value = booking_window(text, NOW, "Africa/Cairo")
    assert value and value[0].isoformat() == date + "T21:00:00+00:00"


def test_conflicting_date_is_clarification() -> None:
    with pytest.raises(ValueError, match="ambiguous_booking_date"):
        booking_window("booked tomorrow 2026-10-01", NOW, "Africa/Cairo")
