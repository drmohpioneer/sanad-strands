"""Removal fences, bounded recovery and retained doctor responsibility."""

from datetime import timedelta
from uuid import uuid4

import pytest
from domain_fixtures import mission

from sanad.domain import MissionState
from sanad.steward.removal import wake
from sanad.store.records import OutboundIntent, PatientProfile, from_record
from store.login_fixtures import ORIGIN, LoginWorld, browser_login
from store.test_login_claim import enrollment as enrollment


def remove(world: LoginWorld, patient_id: str) -> None:
    with world.client() as client:
        assert browser_login(client, world.login_path()).status_code == 303
        record = client.get("/api/patients/" + patient_id).json()
        result = client.post(
            "/api/patients/" + patient_id + "/remove",
            json={
                "expected_version": record["profile_version"],
                "command_id": uuid4().hex,
                "name": record["display_name"],
            },
            headers={"Origin": ORIGIN, "X-CSRF-Token": client.cookies["sanad_csrf"]},
        )
        assert result.status_code == 200, result.text


@pytest.mark.parametrize("linked", [False, True])
def test_contact_fence_and_recoverable_cancellation(enrollment: LoginWorld, linked: bool) -> None:
    w = enrollment
    patient = w.bound() if linked else w.stub()
    item = mission(MissionState.open, doctor_id=patient.scope.doctor_id, patient_id=patient.id)
    w.put_patient(w.doctor, item)
    before = w.store.get_patient_profile(patient.scope)
    assert before
    remove(w, patient.id)
    profile = w.store.get_patient_profile(patient.scope)
    assert profile and profile.removed_at == w.clock()
    assert profile.purge_due_at == w.clock() + timedelta(days=30)
    assert not profile.binding_active and not profile.consent_active
    assert profile.delivery_epoch == before.delivery_epoch + 1
    events = w.store.list_records(patient.scope, "audit_event")[0]
    assert len([r for r in events if r.body["event_type"] == "PatientRemoved"]) == 1
    for _ in range(3):
        row = w.store.get(patient.scope, "patient_removal", patient.id)
        assert row
        wake(w.runtime.steward, row)
        w.clock.advance(timedelta(seconds=1))
    saved = w.store.get_mission(patient.scope, item.id)
    assert saved and saved.state == "cancelled"
    assert w.store.get(patient.scope, "patient", patient.id)
    row = w.store.get(patient.scope, "patient_profile", patient.id)
    assert row and from_record(row, PatientProfile).removed_at


def drain(w: LoginWorld, patient_id: str) -> None:
    from sanad.domain import PatientScope

    scope = PatientScope(doctor_id=w.doctor.id, patient_id=patient_id)
    for _ in range(260):
        row = w.store.get(scope, "patient_removal", patient_id)
        assert row
        if row.body["phase"] == "completed":
            return
        wake(w.runtime.steward, row)
        w.clock.advance(timedelta(seconds=1))
    raise AssertionError("removal did not finish")


def test_large_cleanup_suppresses_first_and_bounds_transactions(
    enrollment: LoginWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domain_fixtures import followup, review_payload

    from sanad.domain import create_review
    from sanad.steward.apply import make_intent
    from sanad.store._base import Check, Write
    from sanad.store.records import DoctorAuthority

    w = enrollment
    patient = w.bound()
    scope = patient.scope
    profile = w.store.get_patient_profile(scope)
    doctor_row = w.store.get(scope, "doctor_authority", scope.doctor_id)
    assert profile and doctor_row
    doctor = from_record(doctor_row, DoctorAuthority)
    policy = w.runtime.steward.policy_provider(scope)
    for i in range(150):
        intent = make_intent(
            scope,
            "queued-" + str(i),
            (),
            "solicited_reply",
            "synthetic",
            w.clock(),
            policy,
            doctor,
            profile,
            audience="patient",
            template_id="patient_safety_ack",
        )
        w.put_patient(w.doctor, intent)
    for i in range(201):
        w.put_patient(
            w.doctor,
            mission(
                MissionState.open,
                id="open-" + str(i),
                doctor_id=scope.doctor_id,
                patient_id=scope.patient_id,
            ),
        )
    for i in range(3):
        w.put_patient(
            w.doctor,
            followup(id="follow-" + str(i), doctor_id=scope.doctor_id, patient_id=scope.patient_id),
        )
    retained = create_review(
        review_payload(owner_doctor_id=scope.doctor_id, patient_id=scope.patient_id),
        w.clock(),
        policy.timing,
    ).aggregate
    w.put_patient(w.doctor, retained)
    remove(w, patient.id)
    original = w.store._atomic
    sizes = []

    def atomic(writes: list[Write], checks: list[Check]) -> bool:
        sizes.append(len(writes) + len(checks))
        if any(x.item.get("entity_type") == "mission" for x in writes):
            assert not any(
                r.body["status"] in {"queued", "uncertain"} and r.body["audience"] == "patient"
                for r in w.store.list_records(scope, "outbound_intent", limit=500)[0]
            )
        return original(writes, checks)

    monkeypatch.setattr(w.store, "_atomic", atomic)
    drain(w, patient.id)
    assert sizes and max(sizes) <= 80
    assert all(
        r.body["state"] == "cancelled" for r in w.store.list_records(scope, "mission", limit=500)[0]
    )
    assert all(r.body["state"] == "cancelled" for r in w.store.list_records(scope, "followup")[0])
    assert w.store.get_review(scope, retained.id) == retained


@pytest.mark.parametrize("danger", [False, True])
def test_delayed_receipt_preserves_provenance_and_only_emergency(
    enrollment: LoginWorld, danger: bool
) -> None:
    from sanad.channels.telegram.router import route_receipt
    from sanad.store import keys
    from sanad.store.records import InboundReceipt
    from store.account_fixtures import BOT, PATIENT, update

    w = enrollment
    patient = w.bound()
    delayed = LoginWorld.create(w.store, w.clock, process=False)
    assert (
        delayed.post(update(PATIENT, "chest pain" if danger else "hello", 9700)).status_code == 200
    )
    row = w.store.get(
        patient.scope, "inbound_receipt", keys.inbound("telegram", keys.digest(f"{BOT}:9700")).pk
    )
    assert row
    before = from_record(row, InboundReceipt)
    assert before.state == "pending"
    remove(w, patient.id)
    count = len(w.store.list_records(patient.scope, "incident")[0])
    prior_intents = {r.id for r in w.store.list_records(patient.scope, "outbound_intent")[0]}
    result = route_receipt(w.runtime, row.scoped_key(patient.scope))
    assert result.status == "removed_patient_unbound"
    saved_row = w.store.get(patient.scope, "inbound_receipt", row.id)
    assert saved_row
    saved = from_record(saved_row, InboundReceipt)
    assert saved.state == "completed"
    assert (
        saved.scope,
        saved.principal,
        saved.source_subject,
        saved.source_chat,
        saved.payload,
        saved.received_at,
    ) == (
        before.scope,
        before.principal,
        before.source_subject,
        before.source_chat,
        before.payload,
        before.received_at,
    )
    assert saved.barrier_outcome is None
    assert len(w.store.list_records(patient.scope, "incident")[0]) == count
    queued = [
        from_record(r, OutboundIntent)
        for r in w.store.list_records(patient.scope, "outbound_intent")[0]
        if r.body["status"] == "queued" and r.id not in prior_intents
    ]
    assert len(queued) == int(danger)
    if danger:
        assert queued[0].template_id == "patient_emergency"
        assert w.dispatch(queued[0]).status == "provider_accepted"


def test_pending_claim_revocation_and_no_fresh_relink(enrollment: LoginWorld) -> None:
    from sanad.auth.commands import IssueInvitation
    from store.account_fixtures import PATIENT

    w = enrollment
    patient = w.stub()
    invitation = w.invite(patient)
    claim = w.consent(w.claim(invitation))
    remove(w, patient.id)
    current = w.claims.patient_claim(claim.id)
    assert current and current.state == "rejected"
    assert w.claims.confirm(w.confirm_command(current)).status != "accepted"
    assert (
        w.claims.issue_invitation(
            IssueInvitation(command_id=uuid4().hex, actor=w.owner, patient_id=patient.id)
        ).status
        == "forbidden"
    )
    assert w.actor(PATIENT).actor_kind == "unknown"


@pytest.mark.parametrize(
    "state",
    [
        "proposed",
        "open",
        "waiting_patient",
        "blocked",
        "unreachable",
        "overdue",
        "awaiting_link",
        "fulfilled",
        "cancelled",
        "closed_unfulfilled",
        "superseded",
    ],
)
def test_mission_states_and_danger_history(enrollment: LoginWorld, state: str) -> None:
    w = enrollment
    patient = w.stub()
    item = mission(MissionState(state), doctor_id=patient.scope.doctor_id, patient_id=patient.id)
    danger = mission(
        MissionState.open,
        id="danger-history",
        danger_history=True,
        doctor_id=patient.scope.doctor_id,
        patient_id=patient.id,
    )
    w.put_patient(w.doctor, item, danger)
    remove(w, patient.id)
    drain(w, patient.id)
    result = w.store.get_mission(patient.scope, item.id)
    assert result
    assert result.state == (
        state
        if state in {"fulfilled", "cancelled", "closed_unfulfilled", "superseded"}
        else "cancelled"
    )
    assert w.store.get_mission(patient.scope, danger.id) == danger


@pytest.mark.parametrize(
    "state",
    [
        "awaiting_anchor",
        "scheduled",
        "waiting_response",
        "overdue",
        "contact_suppressed",
        "fulfilled",
        "cancelled",
    ],
)
def test_followup_states(enrollment: LoginWorld, state: str) -> None:
    from domain_fixtures import followup

    from sanad.domain import FollowUpState

    w = enrollment
    patient = w.stub()
    item = followup(FollowUpState(state), doctor_id=patient.scope.doctor_id, patient_id=patient.id)
    w.put_patient(w.doctor, item)
    remove(w, patient.id)
    drain(w, patient.id)
    result = w.store.get_followup(patient.scope, item.id)
    assert result and result.state == ("fulfilled" if state == "fulfilled" else "cancelled")


def test_removed_media_finishes_without_fetch(enrollment: LoginWorld) -> None:
    from sanad.store import keys
    from sanad.store.records import MediaWork, OperationalClock
    from store.account_fixtures import BOT, PATIENT, update

    w = enrollment
    patient = w.bound()
    delayed = LoginWorld.create(w.store, w.clock, process=False)
    data = update(PATIENT, "", 9750)
    message = data["message"]
    assert isinstance(message, dict)
    message.pop("text")
    message["voice"] = {
        "file_id": "synthetic-removal-voice",
        "file_unique_id": "synthetic",
        "duration": 2,
    }
    assert delayed.post(data).status_code == 200
    receipt_id = keys.inbound("telegram", keys.digest(f"{BOT}:9750")).pk
    row = w.store.get(patient.scope, "inbound_receipt", receipt_id)
    assert row
    work = MediaWork(
        id=keys.digest(receipt_id),
        scope=patient.scope,
        receipt_id=receipt_id,
        provider_handle_ref=str(row.body["provider_media_handle"]),
        created_at=w.clock(),
        updated_at=w.clock(),
        work_clock=OperationalClock(next_action_at=w.clock(), work_lane="media"),
    )
    w.put_patient(w.doctor, work)
    remove(w, patient.id)
    row = w.store.get(patient.scope, "media_work", work.id)
    assert row
    concierge = w.app.state.concierge

    def forbidden_fetch(*args: object) -> None:
        raise AssertionError("removed media was fetched")

    concierge.media_factory = forbidden_fetch
    concierge.sweep(row)
    saved_row = w.store.get(patient.scope, "media_work", work.id)
    assert saved_row
    saved = from_record(saved_row, MediaWork)
    assert saved.state == "removed" and saved.work_clock is None
    assert saved.source_blob_ref is None and saved.normalized_blob_ref is None
    assert saved.transcript_ref is None and saved.stage == "fetch"


@pytest.mark.parametrize("text", ["hello", "/login", "chest pain"])
def test_fresh_removed_sender_only_gets_fixed_emergency(enrollment: LoginWorld, text: str) -> None:
    from store.account_fixtures import PATIENT, update

    w = enrollment
    patient = w.bound()
    remove(w, patient.id)
    before = len([call for call in w.transport.calls if call.recipient_ref == PATIENT])
    assert w.post(update(PATIENT, text, 9780)).status_code == 200
    delivered = [call for call in w.transport.calls if call.recipient_ref == PATIENT][before:]
    assert len(delivered) == int(text == "chest pain")
    assert w.store.list_records(patient.scope, "incident")[0] == ()


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("held", [False, True])
def test_removed_answer_outcome_is_durable_and_held_review_stays(
    enrollment: LoginWorld, linked: bool, held: bool
) -> None:
    from domain_fixtures import review_payload

    from sanad.domain import (
        DoctorAnswerPredicate,
        ObservationRef,
        ReviewObligation,
        TransitionResult,
        create_review,
    )
    from sanad.domain.entities import QuestionDetails
    from sanad.presentation.removal import words
    from sanad.store.records import CommandEnvelope

    w = enrollment
    patient = w.bound() if linked else w.stub()
    scope = patient.scope
    question = mission(
        doctor_id=scope.doctor_id,
        patient_id=scope.patient_id,
        kind="QUESTION",
        objective_predicate=DoctorAnswerPredicate(),
        order_refs=(),
        details=QuestionDetails(
            question_text="May I bring my diary?",
            source_observation_ref=ObservationRef(observation_id="synthetic-question"),
        ),
    )
    creation = create_review(
        review_payload(
            owner_doctor_id=scope.doctor_id,
            patient_id=scope.patient_id,
            review_kind="question_answer",
            source_id=question.id,
            source_mission_id=question.id,
            source_version=question.version,
        ),
        w.clock(),
        w.runtime.steward.policy_provider(scope).timing,
    )
    assert isinstance(creation, TransitionResult) and isinstance(
        creation.aggregate, ReviewObligation
    )
    review = creation.aggregate
    w.put_patient(w.doctor, question, review)
    remove(w, patient.id)
    command = CommandEnvelope(
        command_id="removed-answer",
        scope=scope,
        principal=w.owner,
        requested_at=w.clock(),
        payload={
            "type": "AnswerQuestion",
            "mission_id": question.id,
            "expected_version": question.version,
            "answer_text": "stop my medicine" if held else "Bring your diary.",
        },
    )
    result = w.runtime.steward.handle(command)
    assert result.status == "accepted", result
    assert result.reason_code == "patient_removed"
    replay = w.runtime.steward.handle(command)
    assert replay == result
    assert words("")["not_sent"] == "Not sent: this patient was removed."
    intents = [
        r
        for r in w.store.list_records(scope, "outbound_intent")[0]
        if r.body.get("suppression_reason") == "patient_removed"
    ]
    assert len(intents) == int(linked)
    if held:
        saved = w.store.get_mission(scope, question.id)
        assert saved and isinstance(saved.details, QuestionDetails)
        assert saved.details.held_answer_consumed_at == w.clock()
        assert not saved.details.held_answer_ready
        assert w.store.get_review(scope, review.id) == review


def test_all_removed_contact_commands_refused(enrollment: LoginWorld) -> None:
    from sanad.steward.removal import REFUSED
    from sanad.store.records import CommandEnvelope

    w = enrollment
    patient = w.stub()
    remove(w, patient.id)
    for kind in sorted(REFUSED):
        result = w.runtime.steward.handle(
            CommandEnvelope(
                command_id="refused-" + kind,
                scope=patient.scope,
                principal=w.owner,
                requested_at=w.clock(),
                payload={"type": kind},
            )
        )
        assert (result.status, result.reason_code) == ("forbidden", "patient_removed"), (
            kind,
            result,
        )


def test_uncertain_suppressed_and_sending_untouched(enrollment: LoginWorld) -> None:
    from sanad.steward.apply import make_intent
    from sanad.store._base import Write
    from sanad.store.records import DoctorAuthority, record_item, to_record

    w = enrollment
    patient = w.bound()
    profile = w.store.get_patient_profile(patient.scope)
    doctor_row = w.store.get(patient.scope, "doctor_authority", w.doctor.id)
    assert profile and doctor_row
    values = []
    for status in ("uncertain", "sending"):
        value = make_intent(
            patient.scope,
            status,
            (),
            "solicited_reply",
            "synthetic",
            w.clock(),
            w.runtime.steward.policy_provider(patient.scope),
            from_record(doctor_row, DoctorAuthority),
            profile,
            audience="patient",
            template_id="patient_safety_ack",
        ).model_copy(update={"status": status})
        row = to_record(value, patient.scope)
        assert w.store._atomic([Write(record_item(row), None)], [])
        values.append(value)
    remove(w, patient.id)
    drain(w, patient.id)
    for value in values:
        saved_row = w.store.get(patient.scope, "outbound_intent", value.id)
        assert saved_row
        if value.status == "sending":
            assert from_record(saved_row, OutboundIntent) == value
        else:
            assert saved_row.body["status"] == "suppressed"
            assert saved_row.body["suppression_reason"] == "patient_removed"


@pytest.mark.parametrize("binding_status", ["frozen", "revoked"])
def test_queued_ack_and_removal_without_active_binding(
    enrollment: LoginWorld, binding_status: str
) -> None:
    from sanad.accounts.delivery import account_freshness
    from sanad.auth.commands import RevokeBinding
    from sanad.channels.telegram.router import route_receipt
    from sanad.store._base import Write
    from sanad.store.records import record_item
    from store.account_fixtures import PATIENT, update

    w = enrollment
    patient = w.bound()
    assert (
        w.claims.revoke_binding(
            RevokeBinding(
                command_id="revoke-before-removal",
                actor=w.owner,
                patient_id=patient.id,
                reason_code="wrong_identity",
            )
        ).status
        == "accepted"
    )
    if binding_status == "frozen":
        for scope, kind, id in (
            (patient.scope, "patient_binding", patient.active_binding_id),
            (w.claims.scope, "subject_binding", PATIENT),
        ):
            assert id
            row = w.store.get(scope, kind, id)
            assert row
            changed = row.model_copy(
                update={
                    "version": row.version + 1,
                    "body": row.body | {"status": "frozen", "version": row.version + 1},
                }
            )
            assert w.store._atomic([Write(record_item(changed), row.version)], [])
    delayed = LoginWorld.create(w.store, w.clock, process=False)
    assert delayed.post(update(PATIENT, "hello", 9810)).status_code == 200
    receipt = delayed.receipt(9810)
    row = w.store.get(receipt.scope, "inbound_receipt", receipt.id)
    assert row
    assert (
        route_receipt(w.runtime, row.scoped_key(receipt.scope)).template_id == "patient_safety_ack"
    )
    ack = next(i for i in w.intents() if i.template_id == "patient_safety_ack")
    assert ack.status == "queued"
    assert account_freshness(w.store, ack, w.clock(), w.store._identity) is None
    remove(w, patient.id)
    saved = w.store.get_patient_profile(patient.scope)
    assert saved and saved.removed_at and not saved.consent_active
    outcome = w.dispatch(ack)
    assert (outcome.status, outcome.suppression_reason) == ("suppressed", "patient_removed")


def test_unlinked_removed_patient_suppresses_queued_claim_rejection(
    enrollment: LoginWorld,
) -> None:
    from sanad.auth.commands import RejectClaim

    w = enrollment
    patient = w.stub()
    pending = w.consent(w.claim(w.invite(patient)))
    assert (
        w.claims.reject(
            RejectClaim(command_id="reject-before-removal", actor=w.owner, claim_id=pending.id)
        ).status
        == "accepted"
    )
    intent = next(i for i in w.intents() if i.template_id == "claim_rejected")
    assert intent.status == "queued"
    remove(w, patient.id)
    outcome = w.dispatch(intent)
    assert (outcome.status, outcome.suppression_reason) == ("suppressed", "patient_removed")
