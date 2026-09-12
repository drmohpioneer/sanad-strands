"""Contract 17c through real receipts, durable clocks and captured transport."""

from datetime import datetime, timedelta

import pytest
from harness import FakeClock
from pydantic import ValidationError

from sanad.auth.service import revise
from sanad.channels.telegram.wording import render
from sanad.channels.transport import SendOutcome
from sanad.contact import bundle
from sanad.contact import question_digest as digest
from sanad.domain import Mission, MissionState
from sanad.domain.entities import review_source_key
from sanad.ops.sweep import sweep_due
from sanad.steward.service import Steward
from sanad.steward.types import records
from sanad.store._base import StoreBase
from sanad.store.records import (
    Doctor,
    OutboundIntent,
    QuestionDigestSchedule,
    from_record,
    to_record,
)
from store.concierge_fixtures import PatientWorld
from store.contact_fixtures import dispatch, required
from store.executors_15_fixtures import doctor, question, world


def schedule(w: PatientWorld) -> QuestionDigestSchedule:
    return required(digest.load(w.store, w.doctor.scope))


def tick(w: PatientWorld) -> None:
    result = sweep_due(w.runtime, w.store, elapsed_clock=lambda: 0)
    assert result["errors"] == [], result


def packed(w: PatientWorld) -> list[OutboundIntent]:
    return [
        from_record(r, OutboundIntent)
        for r in records(w.store, w.doctor.scope, "outbound_intent")
        if r.body.get("template_id") == "doctor_question_digest"
    ]


def two(w: PatientWorld) -> tuple[Mission, Mission]:
    first = question(w)
    w.clock.advance(timedelta(seconds=1))
    # Another independently created patient question, using the real relay path.
    _, reply = w.send("talk to the doctor about my appointment")
    assert reply.template_id == "patient_question_forwarded"
    missions = [from_record(r, Mission) for r in w.rows("mission") if r.body["kind"] == "QUESTION"]
    assert len(missions) == 2
    return first, next(m for m in missions if m.id != first.id)


def overdue(w: PatientWorld, *missions: Mission) -> None:
    w.clock.now = max(m.due_at for m in missions)
    tick(w)
    for m in missions:
        assert required(w.store.get_mission(w.patient_scope, m.id)).state == "overdue"
    assert not [
        i
        for i in [from_record(r, OutboundIntent) for r in w.rows("outbound_intent")]
        if i.notification_purpose == "DEADLINE"
    ]
    assert any(r.body.get("event_type") == "QUESTION_DIGEST_ARMED" for r in w.rows("audit_event"))


@pytest.mark.parametrize(
    "argument,at,packing",
    [
        ("", "20:00", "one"),
        ("20:00", "20:00", "one"),
        ("each", "20:00", "each"),
        ("one", "20:00", "one"),
        ("21:30 each", "21:30", "each"),
    ],
)
def test_digest_command(
    store: StoreBase, clock: FakeClock, argument: str, at: str, packing: str
) -> None:
    w = world(store, clock)
    previous = w.doctor
    reply = doctor(w, "/digest " + argument)
    current = w.doctor
    assert current.digest_time == at and current.digest_packing == packing
    expected = (
        f"Next digest: today at {at} Cairo. Nothing is waiting right now."
        if argument
        else "Patient questions are collected and sent to you once a day at 20:00 Cairo "
        "(packing: one message). Nothing is waiting right now."
    )
    assert reply.payload == {"text": expected}
    assert current.version == previous.version + bool(argument)
    assert digest.load(store, w.doctor.scope) is None


@pytest.mark.parametrize(
    "argument", ["25:00", "7pm", "24:00", "2:30", "21:60", "each one", "21:30 each extra"]
)
def test_invalid_setting_changes_nothing(store: StoreBase, clock: FakeClock, argument: str) -> None:
    w = world(store, clock)
    before = w.doctor
    reply = doctor(w, "/digest " + argument)
    assert reply.template_id == "scribe_digest_usage"
    assert w.doctor == before


@pytest.mark.parametrize("answer_n", [1, 2])
def test_two_question_captured_scenario_and_numbered_answer(
    store: StoreBase, clock: FakeClock, answer_n: int
) -> None:
    w = world(store, clock)
    first, second = two(w)
    overdue(w, first, second)
    armed = schedule(w)
    assert armed.version == 2  # Both deadlines fence the same durable schedule.
    assert armed.next_action_at == clock().replace(hour=17, minute=0, second=0, microsecond=0)
    before = len(w.transport.calls)
    clock.now = first.created_at + timedelta(hours=48)
    tick(w)
    assert len(w.transport.calls) == before and not packed(w)
    clock.now = required(armed.next_action_at) - timedelta(seconds=1)
    tick(w)
    assert len(w.transport.calls) == before and not packed(w)
    # A new Steward process consumes the same durable schedule.
    restarted = Steward(store, clock, w.runtime.steward.policy_provider)
    clock.advance(timedelta(seconds=1))
    digest.wake(restarted, to_record(armed, w.doctor.scope))
    intent = packed(w)[0]
    assert intent.question_listing_token is None
    sent = dispatch(w, intent)
    assert sent.status == "provider_accepted" and not sent.notice_feedback_pending
    assert len(w.transport.calls) == before + 1
    assert sent.question_listing_targets == (
        (w.patient_scope.patient_id, first.id),
        (w.patient_scope.patient_id, second.id),
    )
    assert sent.question_listing_expires_at == clock() + timedelta(
        hours=1, seconds=sent.delivery_lease_seconds
    )
    text = str(w.transport.calls[-1].payload["text"])
    assert "1. Synthetic Patient" in text and "2. Synthetic Patient" in text
    assert "/answer 1 … · /close 1" in text and "/answer 2 … · /close 2" in text
    assert "No active medication plan is recorded." in text
    for _, _, review in digest.due(store, w.doctor.scope):
        assert review.first_notice_at == clock()
    assert bundle.eligible(store, w.doctor.scope, clock()) == ()
    assert store.get(w.doctor.scope, "bundle_schedule", w.doctor.id)
    reply = doctor(w, f"/answer {answer_n} Please contact the clinic.", 3001)
    assert reply.template_id == "doctor_question_recorded"
    answered, remaining = (first, second) if answer_n == 1 else (second, first)
    assert required(store.get_mission(w.patient_scope, answered.id)).state == "fulfilled"
    assert required(store.get_mission(w.patient_scope, remaining.id)).state == "overdue"
    print("CAPTURED DIGEST:\n" + text)


def test_answered_before_fire_and_empty_clear(store: StoreBase, clock: FakeClock) -> None:
    w = world(store, clock)
    m = question(w)
    overdue(w, m)
    at = required(schedule(w).next_action_at)
    doctor(w, "/questions")
    doctor(w, "/close 1", 3001)
    clock.now = at
    events_before = list(records(store, w.doctor.scope, "audit_event"))
    digest.wake(w.runtime.steward, to_record(schedule(w), w.doctor.scope))
    assert not packed(w) and schedule(w).next_action_at is None
    assert list(records(store, w.doctor.scope, "audit_event")) == events_before


def test_setting_reclocks_but_pending_delivery_blocks(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(store, clock)
    m = question(w)
    overdue(w, m)
    doctor(w, "/digest 21:30 each")
    assert schedule(w).next_action_at == clock().replace(hour=18, minute=30)
    doctor(w, "/digest 20:00 one", 3001)
    clock.now = required(schedule(w).next_action_at)
    digest.wake(w.runtime.steward, to_record(schedule(w), w.doctor.scope))
    pending = schedule(w)
    with monkeypatch.context() as patch:
        patch.setattr("sanad.api.internal.dispatch_inline", lambda *args: None)
        doctor(w, "/digest 22:00 each", 3002)
    assert schedule(w) == pending
    sent = dispatch(w, packed(w)[0])
    assert sent.status == "provider_accepted"
    assert schedule(w).next_action_at == clock().replace(hour=19)


@pytest.mark.parametrize("packing", ["one", "each"])
def test_rotation_25_and_recurrence(store: StoreBase, clock: FakeClock, packing: str) -> None:
    w = world(store, clock)
    doctor(w, "/digest " + packing)
    m = question(w)
    overdue(w, m)
    m = required(store.get_mission(w.patient_scope, m.id))
    review = digest.due(store, w.doctor.scope)[0][2]
    # Synthetic distinct records exercise the bounded fan-out, retaining true review kinds.
    for n in range(24):
        copy = m.model_copy(
            update={
                "id": f"q-{n:02}",
                "details": m.details.model_copy(
                    update={"question_text": "Synthetic long question. " * 30}
                )
                if packing == "one"
                else m.details,
                "state": MissionState.overdue,
                "handled_deadline_generation": m.deadline_generation,
                "created_at": m.created_at + timedelta(seconds=n + 1),
            }
        )
        w.seed(copy)
        w.seed(
            review.model_copy(
                update={
                    "id": f"r-{n:02}",
                    "source_id": copy.id,
                    "source_mission_id": copy.id,
                    "unique_source_key": review_source_key(
                        w.doctor.id, "mission", copy.id, review.source_version, review.review_kind
                    ),
                }
            )
        )
    expected_first = [(p.id, q.id) for p, q, _ in digest.due(store, w.doctor.scope)[:20]]
    clock.now = required(schedule(w).next_action_at)
    tick(w)
    assert len(schedule(w).last_shown_ids) == 20
    if packing == "one":
        assert len(packed(w)) == 1
        assert list(packed(w)[0].question_listing_targets) == expected_first
        assert "… and 5 more, /questions" in str(w.transport.calls[-1].payload["text"])
        assert len(str(w.transport.calls[-1].payload["text"])) <= 4096
        assert all(
            f"/answer {n} … · /close {n}" in str(w.transport.calls[-1].payload["text"])
            for n in range(1, 21)
        )
    else:
        outgoing = [
            i
            for i in [from_record(r, OutboundIntent) for r in w.rows("outbound_intent")]
            if i.notification_purpose == "DEADLINE"
        ]
        assert len(outgoing) == 20
        assert all(i.status == "provider_accepted" for i in outgoing)
    next_expected = [(p.id, q.id) for p, q, _ in digest.due(store, w.doctor.scope)[:20]]
    assert len(set(next_expected) - set(expected_first)) == 5
    clock.now = required(schedule(w).next_action_at)
    tick(w)
    assert len(schedule(w).last_shown_ids) == 20
    if packing == "one":
        assert len(packed(w)) == 2
        assert (
            list(max(packed(w), key=lambda i: i.created_at).question_listing_targets)
            == next_expected
        )
    else:
        assert (
            len(
                [
                    i
                    for i in [from_record(r, OutboundIntent) for r in w.rows("outbound_intent")]
                    if i.notification_purpose == "DEADLINE"
                ]
            )
            == 40
        )


@pytest.mark.parametrize("status", ["uncertain", "failed"])
def test_unresolved_delivery_blocks_next_fire(
    store: StoreBase, clock: FakeClock, status: str
) -> None:
    w = world(store, clock)
    m = question(w)
    overdue(w, m)
    clock.now = required(schedule(w).next_action_at) + timedelta(hours=2)
    digest.wake(w.runtime.steward, to_record(schedule(w), w.doctor.scope))
    w.transport.script.append(SendOutcome.model_validate({"status": status, "code": "synthetic"}))
    sent = dispatch(w, packed(w)[0])
    assert sent.status == status
    assert all(r.first_notice_at is None for _, _, r in digest.due(store, w.doctor.scope))
    clock.now = required(schedule(w).next_action_at)
    digest.wake(w.runtime.steward, to_record(schedule(w), w.doctor.scope))
    assert len(packed(w)) == 1 and schedule(w).pending_intent_id == sent.id


@pytest.mark.parametrize(
    "now,setting,expected",
    [
        ("2026-04-23T20:00:00+00:00", "00:30", "2026-04-23T22:00:00+00:00"),
        ("2026-10-29T19:00:00+00:00", "23:30", "2026-10-29T20:30:00+00:00"),
        ("2026-10-29T20:45:00+00:00", "23:30", "2026-10-30T21:30:00+00:00"),
    ],
)
def test_cairo_dst(
    store: StoreBase, clock: FakeClock, now: str, setting: str, expected: str
) -> None:
    w = world(store, clock)
    d = w.doctor.model_copy(update={"digest_time": setting})
    assert digest.next_instant(d, datetime.fromisoformat(now)) == datetime.fromisoformat(expected)


def test_schema_and_scoped_clock(store: StoreBase, clock: FakeClock) -> None:
    w = world(store, clock)
    with pytest.raises(ValidationError):
        Doctor.model_validate(w.doctor.model_dump() | {"digest_time": "25:00"})
    armed = required(digest.arm(store, w.doctor, clock()))
    w.seed(armed)
    assert digest.load(store, w.doctor.scope) == armed
    assert store.get(w.patient_scope, "question_digest_schedule", w.doctor.id) is None
    with pytest.raises(ValidationError):
        QuestionDigestSchedule.model_validate(armed.model_dump() | {"next_action_at": None})
    assert render("scribe_digest_usage", "ar")


@pytest.mark.parametrize(
    "command,extra",
    [
        ("/digest 21:30 each", {"language": "ar"}),
        ("/digest 21:30 each", {"status": "suspended"}),
        ("/lang ar", {"digest_time": "22:00"}),
        ("/lang ar", {"digest_packing": "each"}),
    ],
)
def test_setting_store_guards_refuse_other_fields(
    store: StoreBase,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    extra: dict[str, str],
) -> None:
    from sanad.store.records import CommitRequest, CommitResult
    from store.account_fixtures import APPLICANT, update

    w = world(store, clock)
    before = w.doctor
    commit = store.commit
    outcomes = []

    def forged(request: CommitRequest) -> CommitResult:
        if request.command.payload.get("type") in {"ScribeLanguage", "ScribeDigest"}:
            changed = from_record(request.puts[0], Doctor).model_copy(update=extra)
            request = request.model_copy(update={"puts": (to_record(changed, changed.scope),)})
        result = commit(request)
        outcomes.append(result.status)
        return result

    monkeypatch.setattr(store, "commit", forged)
    w.post(update(APPLICANT, command, 3100))
    assert "forbidden" in outcomes and w.doctor == before


def test_browser_digest_matches_bot_and_refuses_mixed(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from store.login_fixtures import ORIGIN, browser_login

    w = world(store, clock)
    m = question(w)
    overdue(w, m)
    from sanad.store.records import InboundAccept, StoredRecord

    captured = []
    accept = store.accept_inbound

    def capture_receipt(key: str, receipt: StoredRecord) -> InboundAccept:
        captured.append(receipt)
        return accept(key, receipt)

    monkeypatch.setattr(store, "accept_inbound", capture_receipt)
    with w.client() as client:
        browser_login(client, w.login_path())
        pref = client.get("/api/preferences").json()
        assert (pref["digest_time"], pref["digest_packing"]) == ("20:00", "one")
        headers = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies["sanad_csrf"]}
        before = len(w.transport.calls)
        body = {
            "digest_time": "21:30",
            "digest_packing": "each",
            "expected_version": pref["version"],
            "command_id": "digest-browser",
        }
        assert (
            client.post(
                "/api/preferences", headers=headers, json=body | {"language": "ar"}
            ).status_code
            == 422
        )
        assert client.post("/api/preferences", headers=headers, json=body).status_code == 200
        assert len(w.transport.calls) == before
        assert w.doctor.digest_time == "21:30" and w.doctor.digest_packing == "each"
        assert schedule(w).next_action_at == clock().replace(hour=18, minute=30)
        assert client.post("/api/preferences", headers=headers, json=body).status_code == 409
        assert any(r.body.get("transport") == "web-digest" for r in captured)
    doctor(w, "/digest 21:30 each", 3100)
    assert w.doctor.digest_time == "21:30" and w.doctor.digest_packing == "each"


def test_listing_expiry_and_newest_across_partitions(store: StoreBase, clock: FakeClock) -> None:
    w = world(store, clock)
    first, second = two(w)
    doctor(w, "/questions", 3000)
    overdue(w, first, second)
    clock.now = required(schedule(w).next_action_at)
    tick(w)
    assert packed(w)[0].status == "provider_accepted"
    clock.now = required(packed(w)[0].question_listing_expires_at)
    reply = doctor(w, "/answer 1 Bring your diary.", 3001)
    assert reply.template_id == "doctor_question_list_stale"
    clock.advance(timedelta(seconds=1))
    doctor(w, "/questions", 3002)
    reply = doctor(w, "/answer 1 Bring your diary.", 3003)
    assert reply.template_id == "doctor_question_recorded"
    assert required(store.get_mission(w.patient_scope, first.id)).state == "fulfilled"


def test_suspended_doctor_never_receives_digest(store: StoreBase, clock: FakeClock) -> None:
    w = world(store, clock)
    m = question(w)
    overdue(w, m)
    clock.now = required(schedule(w).next_action_at)
    digest.wake(w.runtime.steward, to_record(schedule(w), w.doctor.scope))
    before = len(w.transport.calls)
    intent = packed(w)[0]
    w.seed(revise(w.doctor, clock(), status="suspended"))
    sent = dispatch(w, intent)
    assert sent.status == "suppressed" and len(w.transport.calls) == before


def test_tampered_command_exact_status_and_no_mutation(store: StoreBase, clock: FakeClock) -> None:
    from sanad.liaison import templates
    from store.test_inbox_17 import command, offered, setup_review

    w, _ = setup_review(store, clock)
    _, _, offers = offered(w)
    offer = offers[0]
    original = command(w, offer, "tamper-17c")
    review_before = store.get(offer.snapshot.scope, "review", offer.snapshot.review_ref.id)
    offer_before = store.get(w.doctor.scope, "review_offer", offer.id)
    wrong = original.model_copy(update={"payload": original.payload | {"action": "cancel"}})
    result = w.runtime.steward.handle(wrong)
    assert (result.status, result.reason_code) == ("invalid_action", "invalid_action")
    assert store.get(offer.snapshot.scope, "review", offer.snapshot.review_ref.id) == review_before
    assert store.get(w.doctor.scope, "review_offer", offer.id) == offer_before
    assert (
        templates.render("invalid_action", "en")
        == "That button is not valid any more. Open /inbox for the current list."
    )
    clock.now = offer.expires_at
    result = w.runtime.steward.handle(original)
    assert (result.status, result.reason_code) == ("stale_version", "offer_expired_or_used")


def test_feedback_conflict_recovers_without_resending(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from sanad.store.records import StoredRecord

    w = world(store, clock)
    m = question(w)
    overdue(w, m)
    clock.now = required(schedule(w).next_action_at)
    digest.wake(w.runtime.steward, to_record(schedule(w), w.doctor.scope))
    actual = store.complete_delivery
    attempts = []

    def conflict(*args: Any, **kwargs: Any) -> StoredRecord | None:
        resolution = kwargs.get("resolution")
        if resolution and resolution.question_stamps:
            attempts.append(1)
            return None
        return actual(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, "complete_delivery", conflict)
        sent = dispatch(w, packed(w)[0])
    assert len(attempts) == 2 and sent.notice_feedback_pending
    assert sent.status == "provider_accepted"
    before = len(w.transport.calls)
    clock.now = required(sent.work_clock).next_action_at
    tick(w)
    assert len(w.transport.calls) == before
    assert not packed(w)[0].notice_feedback_pending
    assert digest.due(store, w.doctor.scope)[0][2].first_notice_at == sent.accepted_at


def test_digest_and_weekly_are_separate_and_stamp_once(store: StoreBase, clock: FakeClock) -> None:
    w = world(store, clock)
    m = question(w)
    overdue(w, m)
    clock.now = required(schedule(w).next_action_at)
    tick(w)
    first = packed(w)[0].accepted_at
    clock.advance(timedelta(days=7))
    before = len(w.transport.calls)
    tick(w)
    outgoing = [
        from_record(r, OutboundIntent) for r in records(store, w.doctor.scope, "outbound_intent")
    ]
    assert len(w.transport.calls) == before + 2
    assert (
        sum(
            i.template_id == "doctor_weekly_bundle" and i.status == "provider_accepted"
            for i in outgoing
        )
        == 1
    )
    assert (
        sum(
            i.template_id == "doctor_question_digest" and i.status == "provider_accepted"
            for i in outgoing
        )
        == 2
    )
    assert digest.due(store, w.doctor.scope)[0][2].first_notice_at == first


def test_listing_saved_before_transport_and_stale_lease_refused(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    w = world(store, clock)
    first, second = two(w)
    overdue(w, first, second)
    clock.now = required(schedule(w).next_action_at)
    digest.wake(w.runtime.steward, to_record(schedule(w), w.doctor.scope))
    observed = []
    send = w.transport.send

    def inspect(recipient: str, payload: Any) -> SendOutcome:
        intent = packed(w)[0]
        assert intent.status == "sending" and intent.question_listing_token
        assert len(intent.question_listing_targets) == 2
        assert store.save_question_listing(intent, (), clock()) is None
        expired = intent.model_copy(
            update={
                "delivery_claim": required(intent.delivery_claim).model_copy(
                    update={"expires_at": clock()}
                )
            }
        )
        assert (
            store.save_question_listing(
                expired, digest.payload_snapshot(store, intent, clock())[1], clock()
            )
            is None
        )
        observed.append(True)
        return send(recipient, payload)

    monkeypatch.setattr(w.transport, "send", inspect)
    assert dispatch(w, packed(w)[0]).status == "provider_accepted"
    assert observed == [True]


def test_two_tenants_keep_patients_and_listings_separate(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.domain import PatientScope
    from sanad.store.records import Patient
    from store.account_fixtures import AccountWorld

    w = world(store, clock)
    m = question(w)
    overdue(w, m)
    patient, mission, review = digest.due(store, w.doctor.scope)[0]
    other = AccountWorld.approve(w, "20003")
    other_scope = PatientScope(doctor_id=other.id, patient_id="other-patient")
    other_patient = Patient.model_validate(
        patient.model_dump()
        | {
            "id": other_scope.patient_id,
            "scope": other_scope.model_dump(),
            "display_name": "Other synthetic patient",
        }
    )
    w.seed(other_patient)
    w.seed(
        w.profile.model_copy(
            update={
                "id": other_scope.patient_id,
                "doctor_id": other.id,
                "patient_id": other_scope.patient_id,
                "recipient_ref": "30004",
                "recipient_subject": "30004",
            }
        )
    )
    w.seed(
        mission.model_copy(
            update={
                "id": "other-question",
                "doctor_id": other.id,
                "patient_id": other_scope.patient_id,
            }
        )
    )
    w.seed(
        review.model_copy(
            update={
                "id": "other-review",
                "owner_doctor_id": other.id,
                "patient_id": other_scope.patient_id,
                "source_id": "other-question",
                "source_mission_id": "other-question",
                "unique_source_key": review_source_key(
                    other.id, "mission", "other-question", review.source_version, review.review_kind
                ),
            }
        )
    )
    other_schedule = required(digest.arm(store, other, clock()))
    w.seed(other_schedule)
    clock.now = required(other_schedule.next_action_at)
    tick(w)
    messages = [
        c for c in w.transport.calls if str(c.payload.get("text", "")).startswith("These questions")
    ]
    assert {x.recipient_ref for x in messages} == {w.doctor.private_chat_id, other.private_chat_id}
    mine = next(x for x in messages if x.recipient_ref == w.doctor.private_chat_id)
    foreign = next(x for x in messages if x.recipient_ref == other.private_chat_id)
    assert "Other synthetic patient" not in str(mine.payload)
    assert "Other synthetic patient" in str(foreign.payload)
    assert packed(w)[0].question_listing_targets == ((w.patient_scope.patient_id, m.id),)


def test_schedule_cannot_be_changed_by_generic_doctor_command(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.store.records import CommandEnvelope, CommitRequest
    from store.account_fixtures import APPLICANT

    w = world(store, clock)
    armed = required(digest.arm(store, w.doctor, clock()))
    row = to_record(armed, w.doctor.scope)
    request = CommitRequest(
        command=CommandEnvelope(
            command_id="forged-schedule",
            principal=w.actor(APPLICANT),
            scope=w.doctor.scope,
            requested_at=clock(),
            payload={"type": "Bogus"},
        ),
        puts=(row,),
        expected=(row.ref,),
    )
    assert store.commit(request).status == "forbidden"
    assert digest.load(store, w.doctor.scope) is None


def test_tampered_inbox_command_persists_exact_reply(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.steward.types import CommandResult
    from sanad.store.records import CommandEnvelope
    from store.test_inbox_17 import offered, setup_review

    w, review = setup_review(store, clock)
    _, _, offers = offered(w)
    offer = next(o for o in offers if o.action == "review")
    handle = w.runtime.steward.handle

    def tamper(command: CommandEnvelope) -> CommandResult:
        if command.payload.get("type") == "ResolveReview":
            command = command.model_copy(update={"payload": command.payload | {"action": "cancel"}})
        return handle(command)

    monkeypatch.setattr(w.runtime.steward, "handle", tamper)
    reply = doctor(w, f"/resolve {offer.id} Reviewed the report.", 3100)
    assert reply.template_id == "liaison_invalid_action"
    assert reply.payload == {
        "text": "That button is not valid any more. Open /inbox for the current list."
    }
    assert w.receipt(3100).state == "completed"
    assert required(store.get_review(w.patient_scope, review.id)).state == "open"


def test_change_after_render_never_sends_old_patient_name(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from sanad.liaison import agent
    from sanad.store.records import Patient

    w = world(store, clock)
    m = question(w)
    overdue(w, m)
    clock.now = required(schedule(w).next_action_at)
    digest.wake(w.runtime.steward, to_record(schedule(w), w.doctor.scope))
    before = len(w.transport.calls)

    def change(*args: Any) -> agent.Refusal:
        patient = from_record(
            required(store.get(w.patient_scope, "patient", w.patient_scope.patient_id)), Patient
        )
        w.seed(revise(patient, clock(), display_name="Updated synthetic name"))
        return agent.Refusal("stale_source")

    with monkeypatch.context() as patch:
        patch.setattr(agent, "bounded_call", change)
        sent = dispatch(w, packed(w)[0])
    assert sent.status == "queued" and len(w.transport.calls) == before
    clock.now = required(sent.work_clock).next_action_at
    sent = dispatch(w, sent)
    assert sent.status == "provider_accepted"
    assert "Updated synthetic name" in str(w.transport.calls[-1].payload["text"])
