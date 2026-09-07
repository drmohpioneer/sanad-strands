from datetime import timedelta

import pytest
from domain_fixtures import NOW
from harness import FakeClock

from sanad.channels.transport import ProvablyUnsent, SendOutcome
from sanad.contact import feedback
from sanad.domain import FollowUpTask, MissionState
from sanad.steward.dispatch import Dispatcher, freshness
from sanad.steward.service import Steward
from sanad.steward.types import CommandResult
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.records import (
    CommandEnvelope,
    Consent,
    from_record,
)
from store.contact_fixtures import (
    add_mission,
    current,
    dispatch,
    plan,
    required,
    routine,
    tick,
    world,
)
from store.login_fixtures import browser_login


def test_snooze_queued_epoch_suppression_and_expiry_projection(
    store: StoreBase, clock: FakeClock
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    m = add_mission(w)
    tick(w)
    clock.now += timedelta(days=7)
    plan(w, current(w, m))
    queued = next(i for i in routine(w) if i.status == "queued")
    original = (m.due_at, m.escalation_at)
    w.send("أجّل 3 أيام")
    suppressed = dispatch(w, queued)
    assert suppressed.status == "suppressed"
    patient = required(w.claims.patient(w.patient_scope.doctor_id, w.patient_scope.patient_id))
    assert patient.contact_status == "paused" and patient.resume_at == clock() + timedelta(days=3)
    plan(w, current(w, m))
    assert current(w, m).next_contact_at == patient.resume_at
    assert patient.resume_at
    clock.now = patient.resume_at
    tick(w)
    patient = required(w.claims.patient(w.patient_scope.doctor_id, w.patient_scope.patient_id))
    assert patient.contact_status == "active" and patient.resume_at is None
    assert current(w, m).contact_count == 2
    assert (current(w, m).due_at, current(w, m).escalation_at) == original


def test_stop_queued_day3_keeps_disposition_clock(store: StoreBase, clock: FakeClock) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock, medication=True)
    w.send("بدأت الدوا")
    task = from_record(w.rows("followup")[0], FollowUpTask)
    assert task.prompt_at
    clock.now = task.prompt_at
    plan(w, task)
    queued = next(i for i in routine(w) if i.template_id == "patient_day3_prompt")
    w.send("وقف الرسايل")
    assert dispatch(w, queued).status == "suppressed"
    current_task = from_record(w.rows("followup")[0], FollowUpTask)
    assert current_task.state == "contact_suppressed" and current_task.work_clock
    assert (current_task.prompt_at, current_task.due_at) == (task.prompt_at, task.due_at)
    assert any(r.body["review_kind"] == "followup_disposition" for r in w.rows("review"))


def test_day3_quiet_absent_consent_still_reaches_deadline(
    store: StoreBase, clock: FakeClock
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock, medication=True)
    w.send("بدأت الدوا")
    task = from_record(w.rows("followup")[0], FollowUpTask)
    patient = required(w.claims.patient(w.patient_scope.doctor_id, w.patient_scope.patient_id))
    consent = from_record(
        required(store.get(w.patient_scope, "consent", required(patient.consent_id))), Consent
    )
    w.seed(consent.model_copy(update={"quiet_hours": ("09:00", "12:00")}))
    assert task.prompt_at
    clock.now = task.prompt_at
    tick(w)
    assert not any(i.template_id == "patient_day3_prompt" for i in routine(w))
    assert len([r for r in w.rows("review") if r.body["source_type"] == "slot"]) == 1
    assert task.due_at
    clock.now = task.due_at
    tick(w)
    assert from_record(w.rows("followup")[0], FollowUpTask).state == "overdue"
    notices = [r for r in w.rows("outbound_intent") if r.body["notification_purpose"] == "DEADLINE"]
    assert len(notices) == 1 and notices[0].body["status"] == "provider_accepted"


@pytest.mark.parametrize(
    "outcome",
    [
        SendOutcome(status="uncertain"),
        SendOutcome(status="failed", code="blocked", retryable=False),
        ProvablyUnsent(),
    ],
)
def test_unaccepted_contact_never_counts(
    store: StoreBase, clock: FakeClock, outcome: SendOutcome | ProvablyUnsent
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    m = add_mission(w)
    plan(w, m)
    intent = routine(w)[0]
    w.transport.script.append(outcome)
    dispatch(w, intent)
    assert current(w, m).contact_count == current(w, m).unanswered_delivered_count == 0
    assert intent.slot_id
    reservation = store._read(keys.contact(w.patient_scope, intent.slot_id))
    assert reservation
    assert reservation["state"] == (
        "released" if isinstance(outcome, ProvablyUnsent) else "reserved"
    )


def test_forged_slot_refused_and_stale_feedback_recovered(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    m = add_mission(w)
    plan(w, m)
    intent = routine(w)[0]
    lease = store.acquire_patient(w.patient_scope, "forge", clock(), timedelta(minutes=5))
    assert lease
    assert store.reserve_contact(w.patient_scope, "forged", intent.id, lease) == "already_taken"
    store.release_patient(lease)
    assert (
        freshness(store, intent.model_copy(update={"slot_id": "forged"}), clock()) == "slot_invalid"
    )
    actual = w.runtime.steward.handle
    seen: list[str] = []

    def stale(command: CommandEnvelope) -> CommandResult:
        if command.payload.get("type") == "_ContactFeedback":
            seen.append(command.command_id)
            return CommandResult(status="stale_version")
        return actual(command)

    with monkeypatch.context() as patch:
        patch.setattr(w.runtime.steward, "handle", stale)
        saved = dispatch(w, intent)
    assert len(seen) == 2 and saved.contact_feedback == "pending"
    assert saved.work_clock
    clock.now = saved.work_clock.next_action_at
    tick(w)
    assert current(w, m).contact_count == 1 and routine(w)[0].contact_feedback == "applied"


def test_patient_reply_after_acceptance_before_feedback_keeps_unanswered_zero(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    m = add_mission(w)
    plan(w, m)
    with monkeypatch.context() as patch:
        patch.setattr(feedback, "apply", lambda steward, intent: intent)
        accepted = dispatch(w, routine(w)[0])
    clock.now += timedelta(seconds=10)
    w.send("تمام")
    assert accepted.work_clock
    clock.now = accepted.work_clock.next_action_at
    tick(w)
    assert current(w, m).contact_count == 1
    assert current(w, m).unanswered_delivered_count == 0
    assert current(w, m).state == "open"


def test_awaiting_link_binding_starts_ladder_and_never_scanned_deadline(
    store: StoreBase, clock: FakeClock
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    patient = w.stub()
    w.patient_scope = patient.scope
    m = add_mission(w, "awaiting", state=MissionState.awaiting_link)
    plan(w, m)
    assert not routine(w)
    pending = w.consent(w.claim(w.invite(patient), "30004", id=301), id=302)
    result = w.claims.confirm(w.confirm_command(pending))
    assert result.status == "accepted", result
    assert current(w, m).state == "open"
    tick(w)
    assert current(w, m).contact_count == 1
    missing = w.stub()
    w.patient_scope = missing.scope
    late = add_mission(
        w, "never-scanned", state=MissionState.awaiting_link, due=clock() + timedelta(hours=4)
    )
    clock.now = late.escalation_at
    tick(w)
    assert current(w, late).state == "overdue"
    assert not routine(w)
    assert any(
        r.body["notification_purpose"] == "DEADLINE" and r.body["status"] == "provider_accepted"
        for r in w.rows("outbound_intent")
    )


def test_restart_90_day_explicit_horizon_and_doctor_extend(
    store: StoreBase, clock: FakeClock
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    due = clock() + timedelta(days=90)
    m = add_mission(
        w,
        due=due,
        due_source="doctor",
        original_time_expression="in 90 days",
        due_reason="explicit owner instruction",
    )
    original = (m.due_at, m.due_source, m.original_time_expression, m.timing_anchor)
    tick(w)
    clock.now += timedelta(days=45)
    w.runtime.steward = Steward(store, clock, w.runtime.steward.policy_provider)
    w.runtime.dispatcher = Dispatcher(
        w.runtime.steward, w.transport, settings=w.runtime.dispatcher.settings
    )
    tick(w)
    assert (
        current(w, m).due_at,
        current(w, m).due_source,
        current(w, m).original_time_expression,
        current(w, m).timing_anchor,
    ) == original
    from sanad.domain import ExplicitTiming
    from sanad.store.records import CommandEnvelope

    command = CommandEnvelope(
        command_id="extend-90",
        principal=w.owner,
        scope=w.patient_scope,
        requested_at=clock(),
        payload={
            "type": "ExtendMission",
            "mission_id": m.id,
            "timing": ExplicitTiming(
                instant=due + timedelta(days=7),
                original_expression="extend by seven days",
                timezone="Africa/Cairo",
            ).model_dump(mode="json"),
            "reason": "doctor extension",
        },
    )
    assert w.runtime.steward.handle(command).status == "accepted"
    assert current(w, m).due_at == due + timedelta(days=7)
    events = w.rows("audit_event")
    assert any(r.body["event_type"] == "DOCTOR_EXTEND" for r in events)
    prior = current(w, m).timing_history[0]
    assert (
        prior.due_at,
        prior.due_source,
        prior.original_time_expression,
        prior.timing_anchor,
    ) == original


def test_record_api_exposes_contact_and_followup_fields(store: StoreBase, clock: FakeClock) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock, medication=True)
    client = w.client()
    assert browser_login(client, w.login_path()).status_code == 303
    record = client.get("/api/patients/" + w.patient_scope.patient_id).json()
    assert record["contact_status"] == "active"
    assert {"next_contact_at", "contact_count", "unanswered_delivered_count"} <= record["missions"][
        0
    ].keys()
    assert {"prompt_at", "state"} <= record["followups"][0].keys()


def test_late_intended_person_binding_preserves_deadline(
    store: StoreBase, clock: FakeClock
) -> None:
    clock.now = NOW.replace(hour=7)
    w = world(store, clock)
    patient = w.stub()
    w.patient_scope = patient.scope
    m = add_mission(w, state=MissionState.awaiting_link, due=clock() + timedelta(hours=1))
    pending = w.consent(w.claim(w.invite(patient), "30004", id=601), id=602)
    clock.now += timedelta(hours=2)
    assert w.claims.confirm(w.confirm_command(pending)).status == "accepted"
    assert current(w, m).state == "overdue"
    assert (current(w, m).due_at, current(w, m).escalation_at) == (m.due_at, m.escalation_at)
    tick(w)
    assert not routine(w)
    assert (
        sum(
            r.body["notification_purpose"] == "DEADLINE" and r.body["status"] == "provider_accepted"
            for r in w.rows("outbound_intent")
        )
        == 1
    )
