from datetime import datetime, timedelta

import pytest
from domain_fixtures import (
    CORRECTION,
    EVIDENCE,
    NOW,
    ORDER,
    POLICY,
    followup,
    followup_payload,
    mission,
    predicate_result,
)

import sanad.domain.events as ev
from sanad.domain import (
    AnchorFollowUp,
    DoctorTimingPolicy,
    EvidencePredicate,
    ExplicitTiming,
    FollowUpKind,
    FollowUpState,
    FollowUpTask,
    FulfillmentValidity,
    MedicationDetails,
    Mission,
    MissionKind,
    MissionState,
    ObservationRef,
    PatientReportPredicate,
    ResolvedTiming,
    ReviewKind,
    ReviewObligation,
    Timeliness,
    create_followup,
    create_proposed_mission,
    create_review,
    create_support_ticket,
    resolve_timing,
    transition_followup,
    transition_mission,
)


def result(value: ev.TransitionResult | ev.TransitionRejected) -> ev.TransitionResult:
    assert isinstance(value, ev.TransitionResult), value
    return value


def mission_result(value: ev.TransitionResult | ev.TransitionRejected) -> Mission:
    aggregate = result(value).aggregate
    assert isinstance(aggregate, Mission)
    return aggregate


def followup_result(value: ev.TransitionResult | ev.TransitionRejected) -> FollowUpTask:
    aggregate = result(value).aggregate
    assert isinstance(aggregate, FollowUpTask)
    return aggregate


def effects_are(value: ev.TransitionResult, *types: type[ev.Effect]) -> None:
    assert tuple(type(effect) for effect in value.effects) == (*types, ev.RecordAudit)


def fulfillment(at: datetime, **changes: object) -> ev.ObjectiveFulfilled:
    return ev.ObjectiveFulfilled.model_validate(
        {
            "event_id": "synthetic-fulfill",
            "predicate_result": predicate_result(at=at),
            "objective_received_at": at,
            "evidence_refs": (EVIDENCE,),
            "fulfillment_event_id": "synthetic-fulfillment",
            "danger_flag": False,
            "actor_kind": "patient",
            **changes,
        }
    )


def test_test_mission_full_lifecycle_preserves_effect_stream_and_timing_history() -> None:
    timing = resolve_timing(MissionKind.TEST, NOW, POLICY, state_hint="proposed")
    assert isinstance(timing, ResolvedTiming)
    original = mission()
    created = result(
        create_proposed_mission(
            ev.ProposalCreated(
                event_id="synthetic-proposal",
                mission_id=original.id,
                doctor_id=original.doctor_id,
                patient_id=original.patient_id,
                created_at=NOW,
                kind=original.kind,
                title=original.title,
                details=original.details,
                objective_predicate=original.objective_predicate,
                order_refs=original.order_refs,
                timing=timing,
            )
        )
    )
    effects_are(created)
    proposed = mission_result(created)
    assert proposed.state == MissionState.proposed and proposed.confirmed_at is None
    first_due = NOW + timedelta(days=2)
    confirmed = result(
        transition_mission(
            proposed,
            ev.ConfirmMission(
                event_id="synthetic-confirm",
                consent_active=True,
                actor_id="synthetic-doctor",
                explicit=ExplicitTiming(
                    instant=first_due,
                    original_expression="Synthetic in two days",
                    timezone="Africa/Cairo",
                ),
            ),
            NOW,
            POLICY,
        )
    )
    effects_are(confirmed)
    active = mission_result(confirmed)
    contacted = result(
        transition_mission(
            active,
            ev.ContactAccepted(event_id="synthetic-contact"),
            NOW + timedelta(hours=1),
            POLICY,
        )
    )
    effects_are(contacted)
    waiting = mission_result(contacted)
    assert waiting.state == MissionState.waiting_patient
    assert waiting.contact_count == waiting.unanswered_delivered_count == 1
    overdue_at = first_due + timedelta(hours=1)
    overdue_result = result(
        transition_mission(
            waiting, ev.DeadlineReached(event_id="synthetic-deadline"), overdue_at, POLICY
        )
    )
    effects_are(overdue_result, ev.CreateReview, ev.EmitIntent)
    overdue = mission_result(overdue_result)
    assert overdue.state == MissionState.overdue and overdue.handled_deadline_generation == 1
    review_effect = overdue_result.effects[0]
    assert isinstance(review_effect, ev.CreateReview)
    assert (
        review_effect.review_kind,
        review_effect.source_type,
        review_effect.source_id,
        review_effect.source_version,
    ) == (ReviewKind.unmet_objective, "mission", overdue.id, overdue.version)
    assert overdue_result.effects[1] == ev.EmitIntent(
        purpose="DEADLINE",
        source_event_id="synthetic-deadline",
        source_version=overdue.version,
        facts_ref="synthetic-deadline",
    )
    replay = result(
        transition_mission(
            overdue,
            ev.DeadlineReached(event_id="synthetic-deadline-replay"),
            overdue_at + timedelta(minutes=1),
            POLICY,
        )
    )
    assert replay.noop and replay.aggregate is overdue and not replay.effects
    new_due = NOW + timedelta(days=3)
    extended_result = result(
        transition_mission(
            overdue,
            ev.DoctorExtend(
                event_id="synthetic-extend",
                actor_id="synthetic-doctor",
                timing=ExplicitTiming(
                    instant=new_due,
                    original_expression="Synthetic new deadline",
                    timezone="Africa/Cairo",
                ),
                reason="Synthetic extension",
                consent_active=True,
            ),
            overdue_at + timedelta(minutes=1),
            POLICY,
        )
    )
    effects_are(extended_result, ev.ResolveReviewEffect)
    assert extended_result.effects[0] == ev.ResolveReviewEffect(
        review_kind=ReviewKind.unmet_objective,
        source_type="mission",
        source_id=overdue.id,
        reason="Synthetic extension",
    )
    extended = mission_result(extended_result)
    assert extended.state == MissionState.open and extended.deadline_generation == 2
    assert extended.confirmed_at == active.confirmed_at
    assert extended.due_at == new_due and extended.escalation_at == new_due
    verified_at = NOW + timedelta(days=4)
    receipt = new_due + timedelta(minutes=1)
    fulfilled_result = result(
        transition_mission(
            extended, fulfillment(verified_at, objective_received_at=receipt), verified_at, POLICY
        )
    )
    effects_are(fulfilled_result, ev.CreateReview, ev.EmitIntent)
    fulfilled = mission_result(fulfilled_result)
    assert fulfilled.state == MissionState.fulfilled and fulfilled.work_clock is None
    assert fulfilled.fulfilled_at == verified_at and fulfilled.objective_received_at == receipt
    assert fulfilled.timeliness == Timeliness.late
    assert isinstance(fulfilled_result.effects[0], ev.CreateReview)
    assert fulfilled_result.effects[0].review_at == verified_at + POLICY.result_review_interval
    correction = ev.CorrectAcceptedEvidence(
        event_id="synthetic-correction",
        superseded_evidence_ref=EVIDENCE,
        correcting_evidence_ref=CORRECTION,
        predicate_still_holds=False,
    )
    corrected_result = result(
        transition_mission(fulfilled, correction, verified_at + timedelta(hours=1), POLICY)
    )
    effects_are(corrected_result, ev.SupersedeEvidence, ev.SuppressRoutineIntents, ev.CreateReview)
    corrected = mission_result(corrected_result)
    assert corrected.state == MissionState.fulfilled
    assert corrected.fulfillment_validity == FulfillmentValidity.invalidated_pending_review
    assert corrected.evidence_refs == (CORRECTION,) and fulfilled.evidence_refs == (EVIDENCE,)
    assert corrected.fulfillment_event_id == fulfilled.fulfillment_event_id
    assert corrected.objective_received_at == receipt and corrected.fulfilled_at == verified_at
    validated_at = verified_at + timedelta(hours=2)
    validated = result(
        transition_mission(
            corrected,
            ev.ValidateCorrection(
                event_id="synthetic-validate", predicate_result=predicate_result(at=validated_at)
            ),
            validated_at,
            POLICY,
        )
    )
    effects_are(validated)
    assert mission_result(validated).fulfillment_validity == FulfillmentValidity.valid


def test_start_report_and_independent_day_three_response_emit_two_done_intents() -> None:
    draft = mission(
        MissionState.proposed,
        kind=MissionKind.MEDICATION,
        details=MedicationDetails(action="START", order_ref=ORDER),
        objective_predicate=PatientReportPredicate(report_kind="started"),
    )
    confirmed = result(
        transition_mission(
            draft,
            ev.ConfirmMission(
                event_id="synthetic-start-confirm", consent_active=True, actor_id="synthetic-doctor"
            ),
            NOW,
            POLICY,
        )
    )
    effects_are(confirmed, ev.CreateFollowUp)
    payload = confirmed.effects[0]
    assert isinstance(payload, ev.CreateFollowUp) and payload.anchor_time is None
    task = followup_result(create_followup(payload, NOW, POLICY))
    assert (
        task.state == FollowUpState.awaiting_anchor
        and task.prompt_at is None
        and task.due_at is None
    )
    assert task.review_at == NOW + POLICY.draft_review_interval
    day_one = NOW + timedelta(days=1)
    started = result(
        transition_mission(mission_result(confirmed), fulfillment(day_one), day_one, POLICY)
    )
    effects_are(started, ev.EmitIntent, ev.AnchorFollowUp)
    assert task.state == FollowUpState.awaiting_anchor and task.version == 1
    anchor = started.effects[1]
    assert isinstance(anchor, AnchorFollowUp) and anchor.anchor_time == day_one
    anchored = followup_result(
        transition_followup(
            task,
            ev.AnchorConfirmed(
                event_id="synthetic-anchor",
                anchor_kind=anchor.anchor_kind,
                anchor_time=anchor.anchor_time,
                anchor_source_ref=ObservationRef(observation_id="synthetic-start-observation"),
            ),
            day_one,
            POLICY,
        )
    )
    assert anchored.prompt_at == day_one + timedelta(days=3)
    assert anchored.due_at == anchored.prompt_at + timedelta(days=2)
    assert anchored.review_at == anchored.due_at
    prompt_at = anchored.prompt_at
    assert prompt_at is not None
    prompted = followup_result(
        transition_followup(
            anchored, ev.PromptAccepted(event_id="synthetic-prompt"), prompt_at, POLICY
        )
    )
    assert prompted.state == FollowUpState.waiting_response
    response_at = prompt_at + timedelta(hours=1)
    responded = result(
        transition_followup(
            prompted,
            ev.ResponseReceived(
                event_id="synthetic-checkin-response",
                predicate_result=predicate_result(at=response_at),
                objective_received_at=response_at,
                source_report_ids=("synthetic-response-report",),
                danger_flag=False,
            ),
            response_at,
            POLICY,
        )
    )
    effects_are(responded, ev.EmitIntent)
    done = [
        effect
        for outcome in (started, responded)
        for effect in outcome.effects
        if isinstance(effect, ev.EmitIntent)
    ]
    assert [intent.purpose for intent in done] == ["DONE:FULFILLMENT", "DONE:FULFILLMENT"]
    assert done[0].source_event_id != done[1].source_event_id
    assert followup_result(responded).work_clock is None
    assert followup_result(responded).source_report_ids == ("synthetic-response-report",)
    correction = result(
        transition_mission(
            mission_result(started),
            ev.CorrectAcceptedEvidence(
                event_id="synthetic-correction",
                superseded_evidence_ref=EVIDENCE,
                correcting_evidence_ref=CORRECTION,
                predicate_still_holds=False,
            ),
            response_at,
            POLICY,
        )
    )
    assert not any(isinstance(effect, ev.EmitIntent) for effect in correction.effects)
    assert followup_result(responded).state == FollowUpState.fulfilled


def test_pause_and_barrier_cross_deadline_without_moving_clinical_times() -> None:
    due = NOW + timedelta(hours=4)
    active = mission(
        due_at=due, escalation_at=due, review_at=due, next_contact_at=NOW + timedelta(hours=1)
    )
    paused = mission_result(
        transition_mission(
            active,
            ev.PauseContact(
                event_id="synthetic-pause",
                reason="Synthetic pause",
                resume_at=NOW + timedelta(days=1),
            ),
            NOW,
            POLICY,
        )
    )
    assert (paused.due_at, paused.escalation_at, paused.review_at) == (due, due, due)
    assert paused.state == MissionState.blocked and paused.next_contact_at is None
    barrier = mission_result(
        transition_mission(
            paused,
            ev.BarrierRecorded(
                event_id="synthetic-barrier",
                barrier_type="access",
                reason="Synthetic access barrier",
                resume_at=NOW + timedelta(days=2),
            ),
            NOW + timedelta(hours=1),
            POLICY,
        )
    )
    assert (barrier.due_at, barrier.escalation_at, barrier.review_at) == (due, due, due)
    overdue = mission_result(
        transition_mission(
            barrier,
            ev.DeadlineReached(event_id="synthetic-deadline"),
            due + timedelta(days=5),
            POLICY,
        )
    )
    assert overdue.state == MissionState.overdue and overdue.resume_at is None
    assert overdue.due_at == overdue.escalation_at == due
    assert overdue.review_at == due + timedelta(days=5) + POLICY.overdue_review_interval
    assert overdue.fulfillment_validity == FulfillmentValidity.not_fulfilled


def test_receipt_before_due_is_on_time_even_when_verified_after_deadline_and_grace() -> None:
    value = mission(due_at=NOW, escalation_at=NOW + timedelta(hours=2), grace_seconds=7200)
    verified_at = NOW + timedelta(days=1)
    fulfilled = mission_result(
        transition_mission(
            value, fulfillment(verified_at, objective_received_at=NOW), verified_at, POLICY
        )
    )
    assert fulfilled.timeliness == Timeliness.on_time and fulfilled.fulfilled_at == verified_at
    late = mission_result(
        transition_mission(
            value,
            fulfillment(verified_at, objective_received_at=NOW + timedelta(seconds=1)),
            verified_at,
            POLICY,
        )
    )
    assert late.timeliness == Timeliness.late


@pytest.mark.parametrize("action", ["START", "STOP", "CHANGE"])
def test_only_medication_start_confirmation_creates_automatic_followup(action: str) -> None:
    draft = mission(
        MissionState.proposed,
        kind=MissionKind.MEDICATION,
        details={"kind": "MEDICATION", "action": action, "order_ref": ORDER},
        objective_predicate=PatientReportPredicate(report_kind="synthetic-report"),
    )
    confirmed = result(
        transition_mission(
            draft,
            ev.ConfirmMission(
                event_id="synthetic-confirm",
                consent_active=False,
                actor_id="synthetic-doctor",
                anchor_time=NOW,
            ),
            NOW,
            POLICY,
        )
    )
    assert len(
        [effect for effect in confirmed.effects if isinstance(effect, ev.CreateFollowUp)]
    ) == (1 if action == "START" else 0)
    assert mission_result(confirmed).state == MissionState.awaiting_link


def test_question_factory_has_its_own_source_clock_and_doctor_answer_authority() -> None:
    policy = DoctorTimingPolicy.model_validate(POLICY.model_dump() | {"default_grace_seconds": 99})
    created = result(
        create_support_ticket(
            ev.CreateSupportTicket(
                event_id="synthetic-ticket",
                mission_id="synthetic-question",
                doctor_id="synthetic-doctor",
                patient_id="synthetic-patient",
                title="Synthetic question",
                question_text="Synthetic plan question?",
                source_observation_ref=ObservationRef(observation_id="synthetic-observation"),
            ),
            NOW,
            policy,
        )
    )
    ticket = mission_result(created)
    assert (
        ticket.kind == MissionKind.QUESTION and ticket.objective_predicate.kind == "doctor_answer"
    )
    assert ticket.due_at == ticket.escalation_at == ticket.review_at == NOW + timedelta(hours=48)
    assert ticket.grace_seconds == 0 and ticket.order_refs == ()
    assert (
        ticket.confirmed_at == ticket.created_at == NOW
        and ticket.confirmed_by == "system:support_ticket"
    )
    assert ticket.timing_anchor.kind == "ticket_created"
    effects_are(created, ev.CreateReview)
    assert isinstance(created.effects[0], ev.CreateReview)
    obligation = create_review(created.effects[0], NOW, POLICY).aggregate
    assert (
        isinstance(obligation, ReviewObligation)
        and obligation.review_kind == ReviewKind.question_answer
    )
    patient_answer = transition_mission(ticket, fulfillment(NOW), NOW, POLICY)
    assert (
        isinstance(patient_answer, ev.TransitionRejected)
        and patient_answer.reason_code == "doctor_answer_required"
    )
    answered = mission_result(
        transition_mission(ticket, fulfillment(NOW, actor_kind="doctor"), NOW, POLICY)
    )
    assert answered.state == MissionState.fulfilled
    assert obligation.state == "open" and obligation.work_clock is not None


@pytest.mark.parametrize("kind", ["mission", "followup"])
def test_danger_completion_emits_one_urgent_intent_with_completion_provenance(kind: str) -> None:
    if kind == "mission":
        completed = result(
            transition_mission(mission(), fulfillment(NOW, danger_flag=True), NOW, POLICY)
        )
        effects_are(completed, ev.CreateReview, ev.EmitIntent)
    else:
        completed = result(
            transition_followup(
                followup(),
                ev.ResponseReceived(
                    event_id="synthetic-danger-response",
                    predicate_result=predicate_result(),
                    objective_received_at=NOW,
                    danger_flag=True,
                ),
                NOW,
                POLICY,
            )
        )
        effects_are(completed, ev.EmitIntent)
    intents = [effect for effect in completed.effects if isinstance(effect, ev.EmitIntent)]
    assert len(intents) == 1 and intents[0].purpose == "DANGER"
    assert intents[0].source_event_id == intents[0].facts_ref
    assert intents[0].source_version == completed.aggregate.version


def test_explicit_clinical_checkin_does_not_use_medication_offset() -> None:
    prompt = NOW + timedelta(hours=7)
    task = followup_result(
        create_followup(
            followup_payload(
                kind=FollowUpKind.CLINICAL_CHECKIN,
                anchor_kind="doctor_specified_date",
                anchor_time=None,
                prompt_at=prompt,
            ),
            NOW,
            POLICY,
        )
    )
    assert task.state == FollowUpState.scheduled
    assert task.prompt_at == prompt and task.due_at == prompt + POLICY.followup_response_window


def test_reopen_retains_fulfillment_history_and_continues_work_generation() -> None:
    original = mission(MissionState.fulfilled, last_work_generation=8)
    reopened = mission_result(
        transition_mission(
            original,
            ev.DoctorReopen(
                event_id="synthetic-reopen",
                actor_id="synthetic-doctor",
                timing=ExplicitTiming(
                    instant=NOW + timedelta(days=2),
                    original_expression="Synthetic revised deadline",
                    timezone="Africa/Cairo",
                ),
                new_objective_predicate=EvidencePredicate(evaluator="test"),
                new_order_refs=(ORDER,),
                current_active_order_refs=(ORDER,),
                reason="Synthetic revision",
            ),
            NOW,
            POLICY,
        )
    )
    assert reopened.state == MissionState.awaiting_link
    assert reopened.prior_fulfillment_event_ids == (original.fulfillment_event_id,)
    assert reopened.fulfillment_event_id is None
    assert reopened.fulfilled_at is None
    assert reopened.objective_received_at is None
    assert reopened.fulfillment_validity == FulfillmentValidity.not_fulfilled
    assert (
        reopened.timeliness == Timeliness.undetermined
        and reopened.evidence_refs == original.evidence_refs
    )
    assert reopened.last_work_generation == 9 and reopened.deadline_generation == 2
    assert reopened.work_clock is not None and reopened.work_clock.work_generation == 9
    cancelled = mission_result(
        transition_mission(
            reopened,
            ev.DoctorCancel(
                event_id="synthetic-cancel",
                actor_id="synthetic-doctor",
                reason="Synthetic disposition",
            ),
            NOW,
            POLICY,
        )
    )
    assert cancelled.fulfillment_validity == FulfillmentValidity.not_fulfilled
    assert cancelled.prior_fulfillment_event_ids == reopened.prior_fulfillment_event_ids
