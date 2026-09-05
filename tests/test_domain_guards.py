from datetime import timedelta

import pytest
from domain_fixtures import (
    EVIDENCE,
    NOW,
    ORDER,
    POLICY,
    followup,
    followup_payload,
    mission,
    predicate_result,
    review,
    review_payload,
)
from test_domain_scenarios import followup_result, fulfillment, mission_result, result

import sanad.domain.events as ev
from sanad.domain import (
    CoverageStatus,
    DoctorTimingPolicy,
    ExplicitTiming,
    FollowUpKind,
    FollowUpState,
    FulfillmentValidity,
    MedicationDetails,
    MissionState,
    PatientReportPredicate,
    ReviewAction,
    ReviewKind,
    ReviewObligation,
    ReviewState,
    VersionRef,
    create_followup,
    create_review,
    review_source_key,
    transition_followup,
    transition_mission,
    transition_review,
)


def rejected(
    value: ev.TransitionResult | ev.TransitionRejected, code: str
) -> ev.TransitionRejected:
    assert isinstance(value, ev.TransitionRejected), value
    assert value.reason_code == code
    return value


@pytest.mark.parametrize("danger", [False, True])
def test_unsatisfied_predicate_cannot_fulfill_even_with_danger_flag(danger: bool) -> None:
    original = mission()
    failure = rejected(
        transition_mission(
            original,
            fulfillment(NOW, predicate_result=predicate_result(False), danger_flag=danger),
            NOW,
            POLICY,
        ),
        "predicate_not_satisfied",
    )
    assert (
        failure.effects == () and original.fulfillment_validity == FulfillmentValidity.not_fulfilled
    )
    rejected(
        transition_followup(
            followup(),
            ev.ResponseReceived(
                event_id="synthetic-response",
                predicate_result=predicate_result(False),
                objective_received_at=NOW,
                danger_flag=danger,
            ),
            NOW,
            POLICY,
        ),
        "predicate_not_satisfied",
    )


def test_early_deadline_does_not_mutate_or_notify_and_extension_rearms_a_new_generation() -> None:
    original = mission()
    event = ev.DeadlineReached(event_id="synthetic-deadline")
    failure = rejected(transition_mission(original, event, NOW, POLICY), "deadline_not_reached")
    assert not failure.effects
    late = original.escalation_at + timedelta(days=40)
    overdue = mission_result(transition_mission(original, event, late, POLICY))
    assert overdue.review_at == late + POLICY.overdue_review_interval
    new_due = late + timedelta(hours=4)
    extended = mission_result(
        transition_mission(
            overdue,
            ev.DoctorExtend(
                event_id="synthetic-extension",
                actor_id="synthetic-doctor",
                timing=ExplicitTiming(
                    instant=new_due,
                    original_expression="Synthetic in four hours",
                    timezone="Africa/Cairo",
                ),
                reason="Synthetic extension",
                consent_active=True,
            ),
            late,
            POLICY,
        )
    )
    assert extended.handled_deadline_generation == 1 and extended.deadline_generation == 2
    second = result(
        transition_mission(
            extended, ev.DeadlineReached(event_id="synthetic-second-deadline"), new_due, POLICY
        )
    )
    notices = [e for e in second.effects if isinstance(e, ev.EmitIntent)]
    assert len(notices) == 1 and notices[0].purpose == "DEADLINE"
    assert mission_result(second).handled_deadline_generation == 2


@pytest.mark.parametrize("offset", [timedelta(days=-1), timedelta(), timedelta(days=400)])
def test_extend_and_reopen_reject_nonfuture_or_out_of_horizon_timing(offset: timedelta) -> None:
    timing = ExplicitTiming(
        instant=NOW + offset, original_expression="Synthetic invalid time", timezone="Africa/Cairo"
    )
    rejected(
        transition_mission(
            mission(),
            ev.DoctorExtend(
                event_id="synthetic-extend",
                actor_id="synthetic-doctor",
                timing=timing,
                reason="Synthetic extension",
            ),
            NOW,
            POLICY,
        ),
        "timing_needs_clarification",
    )
    rejected(
        transition_mission(
            mission(MissionState.cancelled),
            ev.DoctorReopen(
                event_id="synthetic-reopen",
                actor_id="synthetic-doctor",
                timing=timing,
                new_objective_predicate=mission().objective_predicate,
                new_order_refs=(ORDER,),
                current_active_order_refs=(ORDER,),
                reason="Synthetic reopen",
            ),
            NOW,
            POLICY,
        ),
        "timing_needs_clarification",
    )


def test_reopen_rejects_superseded_orders_and_stale_medication_details() -> None:
    replacement = VersionRef(entity_type="care_order", id=ORDER.id, version=2)
    timing = ExplicitTiming(
        instant=NOW + timedelta(hours=4),
        original_expression="Synthetic future time",
        timezone="Africa/Cairo",
    )
    event = ev.DoctorReopen(
        event_id="synthetic-reopen",
        actor_id="synthetic-doctor",
        timing=timing,
        new_objective_predicate=mission().objective_predicate,
        new_order_refs=(ORDER,),
        current_active_order_refs=(replacement,),
        reason="Synthetic reopen",
    )
    rejected(
        transition_mission(mission(MissionState.cancelled), event, NOW, POLICY), "superseded_order"
    )
    medication = mission(
        MissionState.cancelled,
        kind="MEDICATION",
        details=MedicationDetails(action="START", order_ref=ORDER),
        objective_predicate=PatientReportPredicate(report_kind="started"),
    )
    new_event = ev.DoctorReopen.model_validate(
        event.model_dump()
        | {
            "new_objective_predicate": medication.objective_predicate,
            "new_order_refs": (replacement,),
        }
    )
    rejected(
        transition_mission(medication, new_event, NOW, POLICY), "order_details_require_revision"
    )


@pytest.mark.parametrize(("history", "open_incident"), [(True, False), (False, True), (True, True)])
def test_close_checks_both_danger_projection_and_caller_incident_snapshot(
    history: bool, open_incident: bool
) -> None:
    rejected(
        transition_mission(
            mission(danger_history=history),
            ev.DoctorCloseUnfulfilled(
                event_id="synthetic-close",
                actor_id="synthetic-doctor",
                reason="Synthetic close",
                open_incident=open_incident,
            ),
            NOW,
            POLICY,
        ),
        "danger_history_requires_disposition",
    )


def test_safety_annotation_does_not_duplicate_already_committed_urgent_effects() -> None:
    annotated = result(
        transition_mission(
            mission(MissionState.fulfilled),
            ev.SafetyIncidentRaised(event_id="synthetic-incident"),
            NOW,
            POLICY,
        )
    )
    assert mission_result(annotated).danger_history
    assert len(annotated.effects) == 1 and isinstance(annotated.effects[0], ev.RecordAudit)


@pytest.mark.parametrize("offset", [timedelta(), timedelta(seconds=-1), timedelta(days=20)])
def test_invalid_pause_and_barrier_bounds_are_rejected(offset: timedelta) -> None:
    for event in (
        ev.PauseContact(
            event_id="synthetic-pause", reason="Synthetic pause", resume_at=NOW + offset
        ),
        ev.BarrierRecorded(
            event_id="synthetic-barrier",
            barrier_type="access",
            reason="Synthetic barrier",
            resume_at=NOW + offset,
        ),
    ):
        rejected(transition_mission(mission(), event, NOW, POLICY), "pause_out_of_bounds")


def test_maximum_pause_is_accepted_and_resume_rejections_own_each_missing_authority() -> None:
    paused = mission_result(
        transition_mission(
            mission(),
            ev.PauseContact(
                event_id="synthetic-pause",
                reason="Synthetic pause",
                resume_at=NOW + POLICY.pause_max_interval,
            ),
            NOW,
            POLICY,
        )
    )
    for field, kind in (
        ("doctor_active", ReviewKind.coverage_review),
        ("consent_active", ReviewKind.binding_review),
        ("order_active", ReviewKind.unmet_objective),
    ):
        event = ev.ResumeContact.model_validate(
            {
                "event_id": "synthetic-resume",
                "doctor_active": True,
                "consent_active": True,
                "order_active": True,
                field: False,
            }
        )
        failure = rejected(
            transition_mission(paused, event, NOW, POLICY), "contact_authority_required"
        )
        assert len(failure.effects) == 1
        effect = failure.effects[0]
        assert isinstance(effect, ev.CreateReview) and effect.review_kind == kind
        assert effect.review_at > NOW and effect.source_version == paused.version
    multiple = rejected(
        transition_mission(
            paused,
            ev.ResumeContact(
                event_id="synthetic-resume-all",
                doctor_active=False,
                consent_active=False,
                order_active=False,
            ),
            NOW,
            POLICY,
        ),
        "contact_authority_required",
    )
    assert {e.review_kind for e in multiple.effects if isinstance(e, ev.CreateReview)} == {
        ReviewKind.coverage_review,
        ReviewKind.binding_review,
        ReviewKind.unmet_objective,
    }
    resumed = mission_result(
        transition_mission(
            paused,
            ev.ResumeContact(
                event_id="synthetic-resume",
                doctor_active=True,
                consent_active=True,
                order_active=True,
            ),
            NOW,
            POLICY,
        )
    )
    assert resumed.state == MissionState.open and resumed.resume_at is None


def test_contact_proof_and_counter_changes_do_not_invent_completion() -> None:
    original = mission(contact_count=3, unanswered_delivered_count=2, evidence_request_count=1)
    rejected(
        transition_mission(
            original,
            ev.ContactExhausted(event_id="synthetic-exhausted", exhausted=False),
            NOW,
            POLICY,
        ),
        "contact_not_exhausted",
    )
    contacted = mission_result(
        transition_mission(original, ev.ContactAccepted(event_id="synthetic-contact"), NOW, POLICY)
    )
    assert (
        contacted.contact_count,
        contacted.unanswered_delivered_count,
        contacted.evidence_request_count,
    ) == (4, 3, 1)
    replied = mission_result(
        transition_mission(contacted, ev.PatientReplied(event_id="synthetic-reply"), NOW, POLICY)
    )
    assert replied.contact_count == 4 and replied.unanswered_delivered_count == 0
    assert replied.fulfillment_validity == FulfillmentValidity.not_fulfilled
    resolved = mission_result(
        transition_mission(
            mission(MissionState.blocked),
            ev.BarrierResolved(event_id="synthetic-barrier-resolved"),
            NOW,
            POLICY,
        )
    )
    assert resolved.barrier_type is None
    assert resolved.barrier_reason is None
    assert resolved.resume_at is None


def test_conservative_clock_guards_keep_due_deadlines_visible_until_handled() -> None:
    late = mission(
        due_at=NOW - timedelta(hours=1),
        escalation_at=NOW - timedelta(hours=1),
        review_at=NOW - timedelta(hours=1),
    )
    rejected(
        transition_mission(late, ev.PatientReplied(event_id="synthetic-late-reply"), NOW, POLICY),
        "deadline_requires_handling",
    )
    assert late.handled_deadline_generation == 0
    handled = mission_result(
        transition_mission(late, ev.DeadlineReached(event_id="synthetic-deadline"), NOW, POLICY)
    )
    assert handled.work_clock is not None and handled.work_clock.next_action_at > NOW
    expired_pause = mission(MissionState.blocked, resume_at=NOW)
    rejected(
        transition_mission(
            expired_pause, ev.SafetyIncidentRaised(event_id="synthetic-annotation"), NOW, POLICY
        ),
        "resume_required",
    )


def test_binding_requires_consent_and_replay_never_changes_deadlines() -> None:
    original = mission(MissionState.awaiting_link)
    rejected(
        transition_mission(
            original, ev.PatientBound(event_id="synthetic-bound", consent_active=False), NOW, POLICY
        ),
        "consent_required",
    )
    bound = mission_result(
        transition_mission(
            original, ev.PatientBound(event_id="synthetic-bound", consent_active=True), NOW, POLICY
        )
    )
    assert bound.due_at == original.due_at
    replay = result(
        transition_mission(
            bound,
            ev.PatientBound(event_id="synthetic-replay", consent_active=True),
            NOW + timedelta(minutes=1),
            POLICY,
        )
    )
    assert replay.noop and replay.aggregate is bound and not replay.effects
    late = mission(
        MissionState.awaiting_link,
        due_at=NOW - timedelta(hours=1),
        escalation_at=NOW - timedelta(hours=1),
        handled_deadline_generation=1,
    )
    assert (
        mission_result(
            transition_mission(
                late,
                ev.PatientBound(event_id="synthetic-late-bound", consent_active=True),
                NOW,
                POLICY,
            )
        ).state
        == MissionState.overdue
    )


def test_correction_failure_and_cancellation_preserve_disposition_without_done_or_resolution() -> (
    None
):
    invalidated = mission(
        MissionState.fulfilled, fulfillment_validity=FulfillmentValidity.invalidated_pending_review
    )
    checked = result(
        transition_mission(
            invalidated,
            ev.ValidateCorrection(
                event_id="synthetic-validate", predicate_result=predicate_result(False)
            ),
            NOW,
            POLICY,
        )
    )
    assert (
        mission_result(checked).fulfillment_validity
        == FulfillmentValidity.invalidated_pending_review
    )
    assert all(isinstance(e, ev.RecordAudit) for e in checked.effects)
    reopened_invalid = mission(fulfillment_validity=FulfillmentValidity.invalidated_pending_review)
    cancelled = result(
        transition_mission(
            reopened_invalid,
            ev.DoctorCancel(
                event_id="synthetic-cancel",
                actor_id="synthetic-doctor",
                reason="Synthetic disposition",
            ),
            NOW,
            POLICY,
        )
    )
    assert mission_result(cancelled).cancellation_reason == "Synthetic disposition"
    assert any(
        isinstance(e, ev.CreateReview) and e.review_kind == ReviewKind.correction_disposition
        for e in cancelled.effects
    )
    assert not any(
        isinstance(e, (ev.EmitIntent, ev.ResolveReviewEffect)) for e in cancelled.effects
    )
    original = mission(MissionState.fulfilled)
    valid_replay = result(
        transition_mission(
            original,
            ev.ValidateCorrection(
                event_id="synthetic-validate-valid", predicate_result=predicate_result(False)
            ),
            NOW,
            POLICY,
        )
    )
    assert valid_replay.aggregate is original and valid_replay.noop


def test_late_input_and_association_remain_distinct_from_correction_and_fulfillment() -> None:
    original = mission(MissionState.fulfilled)
    from sanad.domain import ObservationRef

    late = result(
        transition_mission(
            original,
            ev.LateInputRecorded(
                event_id="synthetic-late",
                observation_ref=ObservationRef(observation_id="synthetic-observation"),
            ),
            NOW,
            POLICY,
        )
    )
    assert mission_result(late).evidence_refs == original.evidence_refs
    assert [type(e) for e in late.effects] == [
        ev.RetainObservation,
        ev.CreateReview,
        ev.RecordAudit,
    ]
    associated = result(
        transition_mission(
            mission(),
            ev.EvidenceAssociated(event_id="synthetic-associated", evidence_ref=EVIDENCE),
            NOW,
            POLICY,
        )
    )
    assert mission_result(associated).state == MissionState.open
    assert mission_result(associated).evidence_refs == (EVIDENCE,)
    assert not any(isinstance(e, ev.EmitIntent) for e in associated.effects)


def test_unknown_anchor_deadline_and_suppression_keep_dates_unknown() -> None:
    unknown = followup_result(create_followup(followup_payload(anchor_time=None), NOW, POLICY))
    overdue_at = unknown.review_at + timedelta(days=10)
    overdue = result(
        transition_followup(
            unknown, ev.FollowUpDeadline(event_id="synthetic-followup-deadline"), overdue_at, POLICY
        )
    )
    task = followup_result(overdue)
    assert task.state == FollowUpState.overdue and task.deadline_handled
    assert task.anchor_time is task.prompt_at is task.due_at is None
    assert task.work_clock is not None and task.work_clock.next_action_at > overdue_at
    assert [type(e) for e in overdue.effects] == [ev.CreateReview, ev.EmitIntent, ev.RecordAudit]
    replay = result(
        transition_followup(
            task, ev.FollowUpDeadline(event_id="synthetic-followup-replay"), overdue_at, POLICY
        )
    )
    assert replay.noop and replay.aggregate is task and replay.effects == ()
    suppressed = followup_result(
        transition_followup(
            unknown,
            ev.SuppressFollowUpContact(event_id="synthetic-suppress", reason="Synthetic opt-out"),
            NOW,
            POLICY,
        )
    )
    assert suppressed.prompt_at is suppressed.due_at is None and suppressed.work_clock is not None
    rejected(
        transition_followup(
            suppressed,
            ev.ResponseReceived(
                event_id="synthetic-response",
                predicate_result=predicate_result(),
                objective_received_at=NOW,
                danger_flag=False,
            ),
            NOW,
            POLICY,
        ),
        "anchor_required",
    )
    cancelled = followup_result(
        transition_followup(
            suppressed,
            ev.CancelFollowUp(event_id="synthetic-cancel", reason="Synthetic disposition"),
            NOW,
            POLICY,
        )
    )
    assert cancelled.state == FollowUpState.cancelled and cancelled.work_clock is None


def test_implicit_anchor_preserves_doctor_date_but_explicit_reanchor_recomputes_all_dates() -> None:
    task = followup(anchor_kind="doctor_specified_date")
    implicit = ev.AnchorConfirmed(
        event_id="synthetic-implicit-anchor",
        anchor_kind="reported_effective_start",
        anchor_time=NOW,
    )
    preserved = result(transition_followup(task, implicit, NOW, POLICY))
    assert preserved.noop and preserved.aggregate is task
    explicit = ev.AnchorConfirmed.model_validate(implicit.model_dump() | {"replace_existing": True})
    updated = followup_result(transition_followup(task, explicit, NOW, POLICY))
    assert updated.prompt_at == NOW + POLICY.medication_day3_offset
    assert (
        updated.due_at == updated.review_at == updated.prompt_at + POLICY.followup_response_window
    )
    rejected(
        transition_followup(
            followup(kind=FollowUpKind.CLINICAL_CHECKIN, anchor_time=None), explicit, NOW, POLICY
        ),
        "explicit_prompt_required",
    )


def test_followup_early_prompt_late_anchor_missing_prompt_and_late_responses() -> None:
    scheduled = followup_result(create_followup(followup_payload(anchor_time=NOW), NOW, POLICY))
    rejected(
        transition_followup(
            scheduled, ev.PromptAccepted(event_id="synthetic-early-prompt"), NOW, POLICY
        ),
        "prompt_not_due",
    )
    rejected(
        transition_followup(
            scheduled, ev.FollowUpDeadline(event_id="synthetic-early-deadline"), NOW, POLICY
        ),
        "deadline_not_reached",
    )
    rejected(
        create_followup(
            followup_payload(kind=FollowUpKind.CLINICAL_CHECKIN, prompt_at=None), NOW, POLICY
        ),
        "timing_needs_clarification",
    )
    unknown = followup_result(create_followup(followup_payload(anchor_time=None), NOW, POLICY))
    rejected(
        transition_followup(
            unknown,
            ev.AnchorConfirmed(
                event_id="synthetic-late-anchor",
                anchor_kind="reported_effective_start",
                anchor_time=NOW - timedelta(days=10),
            ),
            NOW,
            POLICY,
        ),
        "deadline_requires_handling",
    )
    late = followup(FollowUpState.overdue)
    completed = followup_result(
        transition_followup(
            late,
            ev.ResponseReceived(
                event_id="synthetic-late-response",
                predicate_result=predicate_result(),
                objective_received_at=NOW,
                danger_flag=False,
            ),
            NOW,
            POLICY,
        )
    )
    assert completed.timeliness.value == "late"


# Independently transcribed A23 permissions; no use of ALLOWED_REVIEW_ACTIONS.
REVIEW_ACTIONS = {
    "result_review": {"review"},
    "correction_disposition": {"review"},
    "incident_response": {"resolve_incident"},
    "unmet_objective": {"extend", "close", "cancel", "review"},
    "question_answer": {"answer", "close"},
    "media_failure": {"dispose", "review"},
    "delivery_failure": {"dispose"},
    "binding_review": {"review", "dispose"},
    "coverage_review": {"restore_coverage", "review"},
    "followup_disposition": {"dispose", "review"},
    "evidence_association": {"associate", "dispose"},
    "intake_clarification": {"clarify", "dispose"},
}


@pytest.mark.parametrize(
    ("kind", "action"), [(kind, action) for kind in REVIEW_ACTIONS for action in ReviewAction]
)
def test_review_resolution_requires_the_exact_action_for_its_kind(
    kind: str, action: ReviewAction
) -> None:
    obligation = create_review(review_payload(review_kind=kind), NOW, POLICY).aggregate
    assert isinstance(obligation, ReviewObligation)
    outcome = transition_review(
        obligation,
        ev.ResolveReview(
            event_id="synthetic-resolve",
            action=action,
            expected_source_version=obligation.source_version,
            actor_id="synthetic-doctor",
            reason="Synthetic explicit disposition",
        ),
        NOW,
        POLICY,
    )
    if action in REVIEW_ACTIONS[kind]:
        resolved = result(outcome).aggregate
        assert isinstance(resolved, ReviewObligation) and resolved.state == ReviewState.resolved
        assert (
            resolved.work_clock is None and resolved.resolved_action_event_id == "synthetic-resolve"
        )
    else:
        rejected(outcome, "review_action_not_allowed")


def test_review_source_keys_and_factory_identity_are_scoped_versioned_and_collision_free() -> None:
    original = review_payload()
    identities = set()
    for changes in (
        {},
        {"owner_doctor_id": "synthetic-other-doctor"},
        {"source_type": "followup"},
        {"source_id": "synthetic-other-source"},
        {"source_version": 3},
        {"review_kind": ReviewKind.incident_response},
    ):
        aggregate = create_review(review_payload(**changes), NOW, POLICY).aggregate
        assert isinstance(aggregate, ReviewObligation)
        identities.add((aggregate.id, aggregate.unique_source_key))
    assert len(identities) == 6
    first = create_review(original, NOW, POLICY).aggregate
    retry = create_review(original, NOW + timedelta(minutes=1), POLICY).aggregate
    assert first.id == retry.id
    assert review_source_key("a:b", "c", "d", 1, ReviewKind.result_review) != review_source_key(
        "a", "b:c", "d", 1, ReviewKind.result_review
    )
    assert review_source_key("a", "b", "c:d", 1, ReviewKind.result_review) != review_source_key(
        "a", "b:c", "d", 1, ReviewKind.result_review
    )


def test_acknowledgment_coverage_and_elapsed_time_do_not_resolve_review() -> None:
    original = review()
    at = original.review_at + timedelta(days=50)
    acknowledged = result(
        transition_review(
            original,
            ev.AcknowledgeReview(event_id="synthetic-ack", actor_id="synthetic-doctor"),
            at,
            POLICY,
        )
    ).aggregate
    assert isinstance(acknowledged, ReviewObligation)
    assert acknowledged.state == ReviewState.acknowledged and acknowledged.resolved_at is None
    assert (
        acknowledged.review_at == original.review_at
        and acknowledged.next_action_at == at + POLICY.overdue_review_interval
    )
    replay = result(
        transition_review(
            acknowledged,
            ev.AcknowledgeReview(event_id="synthetic-ack-replay", actor_id="synthetic-doctor"),
            at,
            POLICY,
        )
    )
    assert replay.noop and replay.aggregate is acknowledged
    blocked = result(
        transition_review(acknowledged, ev.BlockCoverage(event_id="synthetic-block"), at, POLICY)
    ).aggregate
    assert (
        isinstance(blocked, ReviewObligation) and blocked.coverage_status == CoverageStatus.blocked
    )
    restored = result(
        transition_review(blocked, ev.RestoreCoverage(event_id="synthetic-restore"), at, POLICY)
    ).aggregate
    assert (
        isinstance(restored, ReviewObligation)
        and restored.coverage_status == CoverageStatus.covered
    )
    assert restored.state == ReviewState.acknowledged and restored.review_at == original.review_at
    rejected(
        transition_review(
            restored,
            ev.ResolveReview(
                event_id="synthetic-stale-resolution",
                action=ReviewAction.review,
                expected_source_version=1,
                actor_id="synthetic-doctor",
                reason="Synthetic review",
            ),
            at,
            POLICY,
        ),
        "source_version_mismatch",
    )


def test_material_changes_use_supplied_policy_and_do_not_change_source_identity() -> None:
    original = review()
    policy = DoctorTimingPolicy.model_validate(
        POLICY.model_dump() | {"material_change_review_interval": timedelta(hours=13)}
    )
    changed = result(
        transition_review(
            original, ev.MaterialChange(event_id="synthetic-material", version=3), NOW, policy
        )
    ).aggregate
    assert isinstance(changed, ReviewObligation)
    assert changed.last_material_change_version == 3 and changed.review_at == NOW + timedelta(
        hours=13
    )
    assert (
        changed.source_version == original.source_version
        and changed.unique_source_key == original.unique_source_key
    )
    rejected(
        transition_review(
            changed, ev.MaterialChange(event_id="synthetic-stale-material", version=2), NOW, policy
        ),
        "stale_material_change",
    )
