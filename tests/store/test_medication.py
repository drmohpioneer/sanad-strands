"""Contract 14 outcomes through real transactions on MemoryStore and DynamoDB Local."""

from datetime import timedelta
from typing import cast

import pytest
from harness import FakeClock
from pydantic import JsonValue

from sanad.concierge.plan import projection
from sanad.contact.delivery import doctor_payload
from sanad.contact.scheduler import schedule
from sanad.domain import FollowUpTask, Mission, ReviewObligation
from sanad.store._base import StoreBase
from sanad.store.records import OutboundIntent, from_record, to_record
from store.concierge_fixtures import PatientWorld
from store.contact_fixtures import dispatch, required
from store.medication_fixtures import confirm, send, snapshot, world


@pytest.fixture
def w(store: StoreBase, clock: FakeClock) -> PatientWorld:
    value = world(store, clock)
    confirm(value, {"action": "start", "drug": "Atorvastatin", "dose": "20 mg"})
    return value


def medication(w: PatientWorld, action: str = "START") -> Mission:
    return next(
        m
        for m in snapshot(w).missions
        if m.details.kind == "MEDICATION" and m.details.action == action
    )


def task(w: PatientWorld) -> FollowUpTask:
    return from_record(w.rows("followup")[0], FollowUpTask)


def done(w: PatientWorld) -> list[OutboundIntent]:
    return [
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if r.body["notification_purpose"] == "DONE:FULFILLMENT"
    ]


@pytest.mark.parametrize(
    "action,text",
    [
        ("stop", "I stopped Atorvastatin"),
        ("change", "I took the new dose"),
        ("change", "I started Atorvastatin"),
    ],
)
def test_acknowledgments_and_supersession(w: PatientWorld, action: str, text: str) -> None:
    old = medication(w)
    confirm(
        w,
        {
            "action": action,
            "drug": "Atorvastatin",
            **({"dose": "40 mg"} if action == "change" else {}),
        },
    )
    assert required(w.store.get(w.patient_scope, "mission", old.id)).body["state"] == "superseded"
    assert task(w).state == "cancelled"
    assert len(w.rows("followup")) == 1
    reply = send(w, text)
    assert reply.template_id == "patient_" + action + "_recorded"
    current = next(
        r
        for r in w.rows("mission")
        if cast(dict[str, JsonValue], r.body["details"])["action"] == action.upper()
    )
    assert current.body["state"] == "fulfilled"
    assert len(done(w)) == 1
    assert "as reported by the patient" in str(doctor_payload(w.store, done(w)[0])["text"])
    assert dispatch(w, done(w)[0]).status == "provider_accepted"
    assert (
        cast(list[dict[str, str]], projection(snapshot(w))["medication_reports"])[0]["label"]
        == "Self-reported"
    )
    if action == "stop":
        assert not snapshot(w).orders
        assert projection(snapshot(w))["orders"] == []


def test_stop_without_instruction_is_ticket_without_head_mutation(w: PatientWorld) -> None:
    head = w.rows("care_order_head")
    reply = send(w, "I stopped Atorvastatin")
    assert reply.template_id == "patient_stop_missing"
    assert head == w.rows("care_order_head")
    assert medication(w).state == "open"
    assert any(r.body["kind"] == "QUESTION" for r in w.rows("mission"))


@pytest.mark.parametrize("action", ["stop", "change"])
def test_multiple_acknowledgments_use_current_buttons(w: PatientWorld, action: str) -> None:
    confirm(w, {"action": "start", "drug": "Forxiga", "dose": "10 mg"})
    confirm(
        w,
        {"action": action, "drug": "Atorvastatin", "dose": "40 mg"},
        {"action": action, "drug": "Forxiga", "dose": "5 mg"},
    )
    reply = send(w, "I stopped it" if action == "stop" else "I changed it")
    assert reply.template_id == "patient_" + action + "_choose"
    w.press(reply)
    assert w.receipt(2000).state == "completed"
    assert len(done(w)) == 1
    w.press(reply, id=2001)
    assert len(done(w)) == 1


@pytest.mark.parametrize(
    "text", ["I stopped the old medicine and started the new medicine", "وقفت القديم وبدأت الجديد"]
)
def test_combined_stop_and_start_is_atomic(w: PatientWorld, text: str) -> None:
    confirm(
        w,
        {"action": "stop", "drug": "Atorvastatin"},
        {"action": "start", "drug": "Forxiga", "dose": "10 mg"},
    )
    reply = send(w, text)
    assert reply.template_id == "patient_stop_start_recorded"
    assert len(done(w)) == 2
    kinds = {
        cast(dict[str, str], r.body["payload"])["report_kind"] for r in w.rows("clinical_fact")
    }
    assert {"medication_stop", "medication_start"} <= kinds
    assert next(f for f in snapshot(w).followups if f.state != "cancelled").state == "scheduled"


@pytest.mark.parametrize(
    "date_text,days",
    [("yesterday", 1), ("أول امبارح", 2), ("Thursday", 3), ("2026-09-05", 1), ("من 3 أيام", 3)],
)
def test_pending_clarification_resolves_without_ticket_or_model(
    w: PatientWorld, date_text: str, days: int
) -> None:
    reply = send(w, "I started it a while ago")
    assert reply.template_id == "patient_start_date"
    assert w.profile.pending_start_clarification
    assert not any(r.body["kind"] == "QUESTION" for r in w.rows("mission"))
    send(w, date_text)
    assert w.profile.pending_start_clarification is None
    assert task(w).anchor_time == w.clock() - timedelta(days=days)
    assert task(w).prompt_at == w.clock() + timedelta(days=3 - days)
    assert len(done(w)) == 1


def test_direct_date_skips_question(w: PatientWorld) -> None:
    reply = send(w, "I started Atorvastatin on Thursday")
    assert reply.template_id == "patient_start_recorded"
    assert w.profile.pending_start_clarification is None
    assert task(w).anchor_time == w.clock() - timedelta(days=3)


def test_old_date_reports_unknown_and_retains_review(w: PatientWorld) -> None:
    from sanad.domain import CreateReview, ReviewKind, create_review

    m = medication(w)
    review = create_review(
        CreateReview(
            event_id="synthetic-overdue",
            review_kind=ReviewKind.unmet_objective,
            source_type="mission",
            source_id=m.id,
            source_version=m.version,
            source_mission_id=m.id,
            owner_doctor_id=m.doctor_id,
            patient_id=m.patient_id,
            review_at=w.clock() + timedelta(days=1),
        ),
        w.clock(),
        w.runtime.steward.policy_provider(w.patient_scope).timing,
    ).aggregate
    w.seed(review)
    send(w, "I started it a while ago")
    reply = send(w, "2026-08-01")
    assert reply.template_id == "patient_start_unknown"
    assert task(w).state == "awaiting_anchor" and task(w).anchor_time is None
    assert required(w.store.get(w.patient_scope, "review", review.id)).body["state"] == "open"
    assert "start date is uncertain" in str(doctor_payload(w.store, done(w)[0])["text"])


def test_future_and_expired_clarifications(w: PatientWorld) -> None:
    send(w, "I started it a while ago")
    reply = send(w, "tomorrow")
    assert reply.template_id == "patient_start_date" and not done(w)
    w.clock.advance(timedelta(days=2))
    reply = send(w, "yesterday")
    assert reply.template_id == "patient_start_date_expired"
    assert w.profile.pending_start_clarification is None and not done(w)
    assert not any(r.body["kind"] == "QUESTION" for r in w.rows("mission"))
    send(w, "I started it a while ago")
    assert required(w.profile.pending_start_clarification).asked_at == w.clock()


@pytest.mark.parametrize(
    "text,kind",
    [
        ("I can't afford it", "cost"),
        ("not available", "availability"),
        ("I forgot", "forgot"),
        ("I don't understand", "confusion"),
        ("nausea", "side_effect_experience"),
        ("I can't do it", "other"),
    ],
)
def test_barrier_pauses_contact_without_changing_deadline(
    w: PatientWorld, text: str, kind: str
) -> None:
    before = medication(w)
    reply = send(w, text)
    assert reply.template_id == "patient_barrier_recorded"
    blocked = medication(w)
    assert blocked.state == "blocked" and blocked.barrier_type == kind
    assert blocked.resume_at == w.clock() + timedelta(days=1)
    assert blocked.due_at == before.due_at and blocked.escalation_at == before.escalation_at
    assert not done(w)
    send(w, "I finally started Atorvastatin")
    current = w.store.get(w.patient_scope, "mission", before.id)
    assert (
        required(current).body["state"] == "fulfilled"
        and required(current).body["barrier_reason"] is None
    )
    assert any(r.body["event_type"] == "BARRIER_RESOLVED" for r in w.rows("audit_event"))


def waiting_checkin(w: PatientWorld) -> OutboundIntent:
    send(w, "I started Atorvastatin")
    f = task(w)
    w.clock.now = required(f.prompt_at)
    assert schedule(w.runtime.steward, to_record(f, w.patient_scope)).status == "accepted"
    prompt = next(i for i in w.patient_intents() if i.template_id == "patient_day3_prompt")
    assert dispatch(w, prompt).status == "provider_accepted"
    assert task(w).state == "waiting_response"
    return prompt


def test_day3_barrier_is_on_followup_not_fulfilled_parent(w: PatientWorld) -> None:
    waiting_checkin(w)
    parent = next(r for r in w.rows("mission") if r.body["kind"] == "MEDICATION")
    send(w, "I can't afford it")
    assert task(w).state == "fulfilled"
    assert w.store.get(w.patient_scope, "mission", parent.id) == parent
    assert len(task(w).source_report_ids) == 2
    notice = next(i for i in done(w) if any(r.entity_type == "followup" for r in i.source_versions))
    assert 'Barrier: cost - "I can\'t afford it"' in str(doctor_payload(w.store, notice)["text"])
    assert len(done(w)) == 2


def test_treatment_change_at_checkin_records_answer_and_question(w: PatientWorld) -> None:
    waiting_checkin(w)
    reply = send(w, "Can I reduce the dose?")
    assert reply.template_id == "patient_treatment_change_relay"
    assert task(w).state == "fulfilled"
    assert any(r.body["kind"] == "QUESTION" for r in w.rows("mission"))


def test_stop_then_dispatch_reports_order_inactive(w: PatientWorld) -> None:
    send(w, "I started Atorvastatin")
    f = task(w)
    w.clock.now = required(f.prompt_at)
    assert schedule(w.runtime.steward, to_record(f, w.patient_scope)).status == "accepted"
    prompt = next(i for i in w.patient_intents() if i.template_id == "patient_day3_prompt")
    confirm(w, {"action": "stop", "drug": "Atorvastatin"})
    assert task(w).state == "cancelled"
    assert (
        required(w.store.get(w.patient_scope, "outbound_intent", prompt.id)).body["status"]
        == "queued"
    )
    calls = len(w.transport.calls)
    refused = dispatch(w, prompt)
    assert refused.status == "suppressed" and refused.suppression_reason == "order_inactive"
    assert len(w.transport.calls) == calls
    assert dispatch(w, refused) == refused


@pytest.mark.parametrize("days", [5, 6, 7])
def test_late_but_recent_start_keeps_actual_anchor_and_due_wake(w: PatientWorld, days: int) -> None:
    reply = send(w, f"I started Atorvastatin {days} days ago")
    assert reply.template_id == "patient_start_recorded"
    current = task(w)
    assert current.anchor_time == w.clock() - timedelta(days=days)
    assert current.work_clock and current.work_clock.next_action_at == w.clock()
    assert current.due_at == w.clock() + timedelta(days=5 - days)


def test_day3_reply_does_not_reanchor(w: PatientWorld) -> None:
    waiting_checkin(w)
    before = task(w)
    send(w, "I started Atorvastatin yesterday")
    assert task(w).state == "fulfilled"
    assert (task(w).anchor_time, task(w).prompt_at, task(w).due_at) == (
        before.anchor_time,
        before.prompt_at,
        before.due_at,
    )
    assert len(done(w)) == 2


def test_two_start_barriers_choose_and_start_wins(w: PatientWorld) -> None:
    confirm(w, {"action": "start", "drug": "Forxiga", "dose": "10 mg"})
    reply = send(w, "I forgot")
    assert reply.template_id == "patient_barrier_choose"
    w.press(reply)
    blocked = next(m for m in snapshot(w).missions if m.state == "blocked")
    assert blocked.barrier_type == "forgot"
    send(w, f"I forgot but started {blocked.title}")
    assert (
        required(w.store.get(w.patient_scope, "mission", blocked.id)).body["state"] == "fulfilled"
    )
    remaining = next(m for m in snapshot(w).missions if m.details.kind == "MEDICATION")
    send(w, f"I can't afford {remaining.title}")
    send(w, f"I can't afford it but started {remaining.title}")
    assert (
        required(w.store.get(w.patient_scope, "mission", remaining.id)).body["state"] == "fulfilled"
    )


def test_supersession_resolves_only_unmet_review_atomically(w: PatientWorld) -> None:
    from sanad.domain import CreateReview, ReviewKind, create_review

    m = medication(w)
    reviews = []
    for kind in (ReviewKind.unmet_objective, ReviewKind.incident_response):
        value = create_review(
            CreateReview(
                event_id="synthetic:" + kind.value,
                review_kind=kind,
                source_type="mission",
                source_id=m.id,
                source_version=m.version,
                source_mission_id=m.id,
                owner_doctor_id=m.doctor_id,
                patient_id=m.patient_id,
                review_at=w.clock() + timedelta(days=1),
            ),
            w.clock(),
            w.runtime.steward.policy_provider(w.patient_scope).timing,
        ).aggregate
        w.seed(value)
        reviews.append(value)
    p = confirm(w, {"action": "change", "drug": "Atorvastatin", "dose": "40 mg"})
    unmet = from_record(
        required(w.store.get(w.patient_scope, "review", reviews[0].id)), type(reviews[0])
    )
    assert isinstance(unmet, ReviewObligation)
    assert unmet.state == "resolved" and unmet.resolved_reason == "superseded"
    assert w.store.get(w.patient_scope, "review", reviews[1].id) == to_record(
        reviews[1], w.patient_scope
    )
    event = next(
        r
        for r in w.scribe.repo.store.list_records(w.doctor.scope, "audit_event")[0]
        if r.body.get("command_id") == "synthetic-confirm:" + p.id
    )
    assert {m.id, task(w).id, reviews[0].id} <= {
        str(ref["id"]) for ref in cast(list[dict[str, JsonValue]], event.body["aggregate_refs"])
    }


def test_noop_and_history_preserve_clinical_work(w: PatientWorld) -> None:
    before = {
        kind: w.rows(kind) for kind in ("care_order_head", "care_order", "mission", "followup")
    }
    confirm(w, {"action": "continue", "drug": "Atorvastatin", "dose": "20 mg"})
    assert before == {kind: w.rows(kind) for kind in before}
    confirm(w, facts=({"category": "medication_history", "text": "Previously took a medicine"},))
    assert before == {kind: w.rows(kind) for kind in before}


def test_stop_after_optout_never_renews_contact(w: PatientWorld) -> None:
    send(w, "stop reminders")
    before = w.profile
    confirm(w, {"action": "change", "drug": "Atorvastatin", "dose": "40 mg"})
    assert not w.profile.routine_contact_enabled
    assert w.profile.consent_version == before.consent_version
    assert w.profile.delivery_epoch == before.delivery_epoch + 1
    assert task(w).state == "cancelled"
    current = medication(w, "CHANGE")
    schedule(w.runtime.steward, to_record(current, w.patient_scope))
    assert not any(
        i.notification_purpose == "routine_prompt" and i.status == "provider_accepted"
        for i in w.patient_intents()
    )


def test_thirty_prompts_scope_and_restart_recovery(w: PatientWorld) -> None:
    from sanad.steward.apply import make_intent
    from sanad.steward.service import Steward

    confirm(
        w,
        {"action": "start", "drug": "Forxiga", "dose": "10 mg"},
        {"action": "start", "drug": "Amlodipine", "dose": "5 mg"},
    )
    snap = snapshot(w)
    intents = []
    for m in snap.missions:
        for number in range(10):
            intent = make_intent(
                w.patient_scope,
                f"synthetic-queued:{m.id}:{number}",
                (to_record(m, w.patient_scope).ref,),
                "routine_prompt",
                "synthetic",
                w.clock(),
                w.runtime.steward.policy_provider(w.patient_scope),
                snap.authority,
                snap.profile,
                audience="patient",
                slot=f"synthetic:{m.id}:{number}",
                order_refs=m.order_refs,
            )
            w.seed(intent)
            intents.append(intent)
    unscoped = intents[0].model_copy(
        update={"id": "synthetic-unscoped", "logical_key": "synthetic-unscoped", "order_refs": ()}
    )
    w.seed(unscoped)
    old = medication(w)
    old = next(m for m in snap.missions if m.title == "Atorvastatin")
    confirm(w, {"action": "stop", "drug": "Atorvastatin"})
    assert all(
        required(w.store.get(w.patient_scope, "outbound_intent", i.id)).body["status"] == "queued"
        for i in intents
    )
    # Nothing ran between confirmation and reconstruction of the Steward.
    recovered = Steward(w.store, w.clock, w.runtime.steward.policy_provider)
    current = medication(w, "STOP")
    assert schedule(recovered, to_record(current, w.patient_scope)).status == "accepted"
    rows = [required(w.store.get(w.patient_scope, "outbound_intent", i.id)) for i in intents]
    assert sum(r.body["status"] == "suppressed" for r in rows) == 10
    for intent, row in zip(intents, rows, strict=True):
        assert (row.body["status"] == "suppressed") == bool(
            set(intent.order_refs).intersection(old.order_refs)
        )
    schedule(recovered, to_record(current, w.patient_scope))
    assert rows == [w.store.get(w.patient_scope, "outbound_intent", i.id) for i in intents]
    assert (
        required(w.store.get(w.patient_scope, "outbound_intent", unscoped.id)).body["status"]
        == "queued"
    )


def test_first_blocked_deadline_carries_barrier(w: PatientWorld) -> None:
    from sanad.steward.service import system_command

    send(w, "I can't afford it")
    current = medication(w)
    w.clock.now = current.due_at
    result = w.runtime.steward.handle(
        system_command(
            w.patient_scope,
            "synthetic-blocked-deadline",
            {"type": "_Deadline", "mission_id": current.id},
            w.clock(),
        )
    )
    assert result.status == "accepted"
    notice = next(
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if r.body["notification_purpose"] == "DEADLINE"
    )
    assert 'Barrier: cost - "I can\'t afford it"' in str(doctor_payload(w.store, notice)["text"])
    assert medication(w).due_at == current.due_at


def test_danger_at_checkin_retains_accepted_incident_path(w: PatientWorld) -> None:
    from store.account_fixtures import PATIENT, update

    waiting_checkin(w)
    before = task(w)
    assert w.post(update(PATIENT, "عندي ألم شديد في الصدر", 1600)).status_code == 200
    assert w.rows("incident")
    assert task(w) == before
    assert len(done(w)) == 1
    assert sum(r.body["notification_purpose"] == "DANGER" for r in w.rows("outbound_intent")) == 1


def test_answer_to_cancelled_checkin_is_not_a_day3_report(w: PatientWorld) -> None:
    from unittest.mock import Mock

    from sanad.concierge.reports import record_day3
    from sanad.steward.patient import PatientTurnCommit

    waiting_checkin(w)
    confirm(w, {"action": "change", "drug": "Atorvastatin", "dose": "40 mg"})
    assert task(w).state == "cancelled"
    tx = Mock(spec=PatientTurnCommit, snapshot=snapshot(w))
    assert not record_day3(tx, "done")
    tx.put.assert_not_called()
    assert task(w).state == "cancelled"


@pytest.mark.parametrize(
    "text,template",
    [("I forgot", "patient_barrier_choose"), ("I started it yesterday", "patient_start_choose")],
)
def test_voice_choice_retains_screened_words_without_a_provider(
    w: PatientWorld, text: str, template: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.domain import Principal, Provenance
    from sanad.media.speech import Transcript
    from sanad.models.io import CallMetadata
    from sanad.store.records import InboundReceipt
    from store.test_concierge_media import media_message

    confirm(w, {"action": "start", "drug": "Forxiga", "dose": "10 mg"})

    def voice(receipt: InboundReceipt, principal: Principal, source: Provenance) -> Transcript:
        return Transcript(
            text=text,
            spans=(),
            duration=1,
            model_id="synthetic-no-provider",
            numbers=(),
            numbers_line="parsed",
            heard_numbers=(),
            disputed_numbers=(),
            provenance=(source,),
            metadata=CallMetadata(
                model_id="synthetic-no-provider", policy_version="synthetic", latency_ms=0
            ),
        )

    monkeypatch.setattr(w.concierge, "_voice", voice)
    assert w.post(media_message()).status_code == 200
    assert w.receipt(1100).state == "completed"
    choice = next(i for i in w.patient_intents() if i.template_id == template)
    w.press(choice)
    assert w.receipt(2000).state == "completed"
    payloads = [cast(dict[str, JsonValue], r.body["payload"]) for r in w.rows("clinical_fact")]
    assert any(p["text"] == text for p in payloads)
    if template == "patient_barrier_choose":
        assert any(m.state == "blocked" for m in snapshot(w).missions)
    else:
        assert any(f.anchor_time == w.clock() - timedelta(days=1) for f in snapshot(w).followups)
