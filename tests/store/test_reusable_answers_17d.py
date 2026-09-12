"""Reusable answers through real receipts, guards and browser sessions."""

from datetime import timedelta

import pytest
from harness import FakeClock
from pydantic import JsonValue

from sanad.auth.service import revise
from sanad.concierge import reuse
from sanad.concierge.text import normalized
from sanad.domain import Mission
from sanad.liaison.records import ReusableAnswer, ReuseOffer
from sanad.steward.types import records
from sanad.store._base import StoreBase
from sanad.store.records import CommandEnvelope, from_record
from store.account_fixtures import APPLICANT, callback
from store.concierge_fixtures import PatientWorld
from store.executors_15_fixtures import doctor, get, question, world

QUESTION = "Where should I bring my appointment diary?"
ANSWER = "Please bring your diary to the clinic."


def library(w: PatientWorld) -> tuple[Mission, ReuseOffer]:
    m = question(w)
    m = m.model_copy(update={"details": m.details.model_copy(update={"question_text": QUESTION})})
    w.seed(m)
    doctor(w, "/questions")
    reply = doctor(w, "/answer 1  " + ANSWER + "  ", 3001)
    assert reply.template_id == "doctor_question_recorded", reply.payload
    offers = list(records(w.store, w.doctor.scope, "reuse_offer"))
    assert len(offers) == 1
    offer = from_record(offers[0], ReuseOffer)
    assert offer.answer_text == ANSWER
    assert offer.mission_version == get(w, m).version
    assert reply.payload and "Reuse this answer for similar questions" in str(reply.payload)
    assert not list(records(w.store, w.doctor.scope, "reusable_answer"))
    return m, offer


@pytest.mark.parametrize("button", [False, True])
def test_explicit_single_use_and_exact_automatic_answer(
    store: StoreBase, clock: FakeClock, button: bool
) -> None:
    w = world(store, clock)
    m, offer = library(w)
    if button:
        w.post(callback(offer.id, APPLICANT, 3002))
    else:
        response = doctor(w, "/reuse", 3002)
        assert response.payload and "Synthetic Patient" in str(response.payload)
    assert len(list(records(store, w.doctor.scope, "reusable_answer"))) == 1
    before = len(w.rows("mission"))
    model, reply = w.send(QUESTION)
    assert not model.script.calls
    assert (
        reply.payload and ANSWER in str(reply.payload) and "From your doctor" in str(reply.payload)
    )
    assert len(w.rows("mission")) == before
    result = w.runtime.steward.handle(
        CommandEnvelope(
            command_id="reuse-again",
            principal=w.actor(APPLICANT),
            scope=w.patient_scope,
            requested_at=clock(),
            payload={"type": "ReuseAnswer", "offer_id": offer.id},
        )
    )
    assert result.status != "accepted"
    assert get(w, m).state == "fulfilled"


@pytest.mark.parametrize("change", ["expiry", "epoch", "mission"])
def test_offer_stale(store: StoreBase, clock: FakeClock, change: str) -> None:
    w = world(store, clock)
    m, offer = library(w)
    if change == "expiry":
        clock.advance(timedelta(hours=1))
    elif change == "epoch":
        w.seed(revise(offer, clock(), auth_epoch=offer.auth_epoch + 1))
    else:
        w.seed(revise(get(w, m), clock()))
    result = w.runtime.steward.handle(
        CommandEnvelope(
            command_id="reuse-stale",
            principal=w.actor(APPLICANT),
            scope=w.patient_scope,
            requested_at=clock(),
            payload={"type": "ReuseAnswer", "offer_id": offer.id},
        )
    )
    assert result.status != "accepted"
    assert not list(records(store, w.doctor.scope, "reusable_answer"))


@pytest.mark.parametrize(
    "text",
    [
        "talk to the doctor",
        "can I stop my medicine?",
        "Where should I bring my appointment diary tomorrow?",
    ],
)
def test_explicit_and_near_matches_remain_tickets(
    store: StoreBase, clock: FakeClock, text: str
) -> None:
    w = world(store, clock)
    _, offer = library(w)
    doctor(w, "/reuse", 3002)
    source_row = store.get(w.doctor.scope, "reusable_answer", offer.id)
    assert source_row
    source = from_record(source_row, ReusableAnswer)
    if text != "Where should I bring my appointment diary tomorrow?":
        w.seed(revise(source, clock(), question_text=text, normalized_question=normalized(text)))
    before = len(w.rows("mission"))
    w.send(text)
    assert len(w.rows("mission")) == before + 1
    listing = doctor(w, "/questions", 3003)
    assert listing.payload and "Proposed reply:" in str(listing.payload)


@pytest.mark.parametrize("change", ["none", "mission", "answer", "listing", "expiry"])
def test_send_snapshot_and_replay(store: StoreBase, clock: FakeClock, change: str) -> None:
    w = world(store, clock)
    _, offer = library(w)
    doctor(w, "/reuse", 3002)
    w.send("Where should I bring my appointment diary tomorrow?")
    listing = doctor(w, "/questions", 3003)
    b = listing.question_bindings[0]
    payload: dict[str, JsonValue] = {
        "type": "SendQuestion",
        "mission_id": b.mission_id,
        "listing_token": listing.question_listing_token,
        "n": 1,
        "mission_version": b.mission_version,
        "reusable_id": b.reusable_id,
        "reusable_version": b.reusable_version,
    }
    command = CommandEnvelope(
        command_id="send-test",
        principal=w.actor(APPLICANT),
        scope=w.patient_scope,
        requested_at=clock(),
        payload=payload,
    )
    if change == "mission":
        row = store.get(w.patient_scope, "mission", b.mission_id)
        assert row
        w.seed(revise(from_record(row, Mission), clock()))
    elif change == "answer":
        row = store.get(w.doctor.scope, "reusable_answer", offer.id)
        assert row
        w.seed(revise(from_record(row, ReusableAnswer), clock()))
    elif change == "listing":
        doctor(w, "/questions", 3004)
    elif change == "expiry":
        clock.advance(timedelta(hours=1))
    result = w.runtime.steward.handle(command)
    assert (result.status == "accepted") == (change == "none"), result
    if change == "none":
        clock.advance(timedelta(hours=2))
        assert w.runtime.steward.handle(command) == result
        assert any(
            r.body.get("event_type") == "QUESTION_PROPOSED_REPLY_SENT"
            for r in w.rows("audit_event")
        )


@pytest.mark.parametrize("later", [False, True])
def test_defer_rearms_review_without_shortening(
    store: StoreBase, clock: FakeClock, later: bool
) -> None:
    w = world(store, clock)
    m = question(w)
    if not later:
        clock.now = m.due_at + timedelta(hours=1)
    before = m.due_at
    doctor(w, "/questions")
    reply = doctor(w, "/defer 1", 3001)
    assert reply.template_id == "doctor_question_deferred", reply.payload
    changed = get(w, m)
    assert changed.due_at == max(before, clock() + timedelta(hours=24))
    review = next(r for r in w.rows("review") if r.body.get("review_kind") == "question_answer")
    assert review.body["review_at"] == changed.due_at.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("action", ["answer", "close", "defer", "send", "reuse"])
def test_browser_twins(store: StoreBase, clock: FakeClock, action: str) -> None:
    from store.login_fixtures import ORIGIN, browser_login

    w = world(store, clock)
    m = question(w)
    if action in {"send", "reuse"}:
        # Library helper uses its own world question; reset that helper's source lookup.
        doctor(w, "/questions")
        doctor(w, "/answer 1 " + ANSWER, 3001)
        offer = from_record(next(iter(records(store, w.doctor.scope, "reuse_offer"))), ReuseOffer)
        if action == "send":
            doctor(w, "/reuse", 3002)
            w.send("talk to the doctor")
    with w.client() as client:
        browser_login(client, w.login_path())
        response = client.get("/api/questions")
        assert response.status_code == 200, response.text
        data = response.json()
        headers = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies["sanad_csrf"]}
        if action == "reuse":
            target = m.id
            body = {"offer_id": offer.id, "command_id": "browser-action"}
        else:
            item = data["questions"][0]
            target = item["id"]
            body = {"expected_version": item["version"], "command_id": "browser-action"}
            if action == "answer":
                body["text"] = ANSWER
            elif action == "send":
                source = item["proposed_reply"]
                body = {
                    "command_id": "browser-action",
                    "listing_token": data["listing_token"],
                    "n": item["n"],
                    "mission_version": item["version"],
                    "reusable_id": source["reusable_id"],
                    "reusable_version": source["version"],
                }
        result = client.post(f"/api/questions/{target}/{action}", headers=headers, json=body)
        assert result.status_code == 200, result.text
        assert (
            client.post(f"/api/questions/{target}/{action}", headers=headers, json=body).json()
            == result.json()
        )
        if action == "answer":
            assert result.json()["offer_id"]


@pytest.mark.parametrize("answer", ["Take warfarin 5 mg tonight.", "a" * 701, "stop my medicine"])
def test_rejected_or_held_never_offered(store: StoreBase, clock: FakeClock, answer: str) -> None:
    w = world(store, clock)
    question(w)
    doctor(w, "/questions")
    doctor(w, "/answer 1 " + answer, 3001)
    assert not list(records(store, w.doctor.scope, "reuse_offer"))


@pytest.mark.parametrize("answer", ["Take warfarin 5 mg tonight.", "Your number is 42.", "a" * 701])
def test_recipient_validation_withholds_library_text(
    store: StoreBase, clock: FakeClock, answer: str
) -> None:
    w = world(store, clock)
    _, offer = library(w)
    doctor(w, "/reuse", 3002)
    row = store.get(w.doctor.scope, "reusable_answer", offer.id)
    assert row
    w.seed(revise(from_record(row, ReusableAnswer), clock(), answer_text=answer))
    before = len(w.rows("mission"))
    model, reply = w.send(QUESTION)
    assert not model.script.calls
    assert len(w.rows("mission")) == before + 1
    assert reply.payload and answer not in str(reply.payload)
    listing = doctor(w, "/questions", 3003)
    assert listing.payload and "Proposed reply:" in str(listing.payload)


def test_overlap_threshold_exact_priority_and_ties(store: StoreBase, clock: FakeClock) -> None:
    w = world(store, clock)
    m = question(w)
    base = ReusableAnswer(
        id="a",
        scope=w.doctor.scope,
        question_text="alpha beta gamma",
        normalized_question="alpha beta gamma",
        answer_text=ANSWER,
        source_mission_id=m.id,
        source_patient_id=m.patient_id,
        created_at=clock(),
        updated_at=clock(),
    )
    w.seed(base)
    assert reuse.best(store, w.doctor.id, "alpha beta gamma delta epsilon") == base
    assert reuse.best(store, w.doctor.id, "alpha beta gamma delta epsilon zeta") is None
    assert reuse.best(store, w.doctor.id, "alpha beta gamma delta", exact=True) is None
    w.seed(base.model_copy(update={"id": "z"}))
    chosen = reuse.best(store, w.doctor.id, "alpha beta gamma")
    assert chosen and chosen.id == "z"
    clock.advance(timedelta(seconds=1))
    w.seed(base.model_copy(update={"id": "new", "created_at": clock()}))
    chosen = reuse.best(store, w.doctor.id, "alpha beta gamma")
    assert chosen and chosen.id == "new"
    assert reuse.best(store, "another-doctor", "alpha beta gamma") is None


def test_atomic_offer_survives_acknowledgment_crash(store: StoreBase, clock: FakeClock) -> None:
    from store.account_fixtures import update

    w = world(store, clock)
    question(w)
    doctor(w, "/questions")

    def crash(name: str) -> None:
        if name == "doctor_clinical_committed":
            raise RuntimeError("synthetic crash")

    w.scribe.checkpoint = crash
    w.post(update(APPLICANT, "/answer 1 " + ANSWER, 3001))
    assert w.receipt(3001).state != "completed"
    offers = list(records(store, w.doctor.scope, "reuse_offer"))
    assert len(offers) == 1
    clock.advance(timedelta(hours=2))
    w.scribe.checkpoint = lambda _: None
    w.post(update(APPLICANT, "/answer 1 " + ANSWER, 3001))
    assert len(list(records(store, w.doctor.scope, "reuse_offer"))) == 1
    assert w.receipt(3001).state == "completed"


def test_guard_rejects_forged_library_write(store: StoreBase, clock: FakeClock) -> None:
    from sanad.store.records import CommitRequest, to_record

    w = world(store, clock)
    _, offer = library(w)
    forged = to_record(
        offer.model_copy(update={"id": "forged", "answer_text": "invented"}), offer.scope
    )
    request = CommitRequest(
        command=CommandEnvelope(
            command_id="forged",
            principal=w.actor(APPLICANT),
            scope=w.patient_scope,
            requested_at=clock(),
            payload={"type": "RecordPatientReply"},
        ),
        puts=(forged,),
        expected=(forged.ref,),
    )
    assert store.commit(request).status == "forbidden"
    assert store.get(offer.scope, "reuse_offer", "forged") is None


@pytest.mark.parametrize("missing", ["session", "csrf"])
def test_browser_authentication_required(store: StoreBase, clock: FakeClock, missing: str) -> None:
    from store.login_fixtures import browser_login

    w = world(store, clock)
    m = question(w)
    with w.client() as client:
        if missing == "csrf":
            browser_login(client, w.login_path())
        result = client.post(
            f"/api/questions/{m.id}/close",
            json={"command_id": "unauthorized", "expected_version": m.version},
        )
        assert result.status_code == (401 if missing == "session" else 403)
        assert get(w, m).state == "open"


@pytest.mark.parametrize("operation", ["send", "defer", "reuse"])
def test_bot_recovery_after_new_listing(store: StoreBase, clock: FakeClock, operation: str) -> None:
    from store.account_fixtures import update

    w = world(store, clock)
    library(w)
    if operation != "reuse":
        doctor(w, "/reuse", 3002)
        w.send("Where should I bring my appointment diary tomorrow?")
        doctor(w, "/questions", 3003)

    def crash(name: str) -> None:
        if name == "doctor_clinical_committed":
            raise RuntimeError("synthetic crash")

    w.scribe.checkpoint = crash
    text = "/reuse" if operation == "reuse" else "/" + operation + " 1"
    w.post(update(APPLICANT, text, 3010))
    clock.advance(timedelta(hours=2))
    w.scribe.checkpoint = lambda _: None
    doctor(w, "/questions", 3011)
    before = len(w.rows("audit_event"))
    w.post(update(APPLICANT, text, 3010))
    assert w.receipt(3010).state == "completed"
    assert len(w.rows("audit_event")) == before


@pytest.mark.parametrize("packing", ["one", "each"])
def test_digest_proposals_have_exact_bound_versions(
    store: StoreBase, clock: FakeClock, packing: str
) -> None:
    from store.test_question_digest_17c import schedule, tick

    w = world(store, clock)
    _, offer = library(w)
    doctor(w, "/reuse", 3002)
    doctor(w, "/digest " + packing, 3003)
    w.send("Where should I bring my appointment diary tomorrow?")
    m = next(from_record(r, Mission) for r in w.rows("mission") if r.body["state"] == "open")
    clock.now = m.due_at
    tick(w)
    next_at = schedule(w).next_action_at
    assert next_at
    clock.now = next_at
    tick(w)
    listing = max(
        reuse.listings(store, w.doctor.id),
        key=lambda i: (i.created_at, i.conversation_sequence, i.id),
    )
    assert listing.question_bindings[0].reusable_id == offer.id
    assert listing.question_bindings[0].reusable_version == 1
    assert "Proposed reply: " + ANSWER in str(w.transport.calls[-1].payload)
    result = doctor(w, "/send 1", 3004)
    assert result.template_id == "doctor_question_recorded", result.payload


def test_each_overflow_keeps_scheduled_targets_and_numbering(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.contact import question_digest as digest
    from sanad.domain.entities import review_source_key
    from store.test_question_digest_17c import schedule, tick

    w = world(store, clock)
    m = question(w)
    doctor(w, "/digest each")
    clock.now = m.due_at
    tick(w)
    source = get(w, m)
    review = digest.due(store, w.doctor.scope)[0][2]
    for index in range(24):
        copy = source.model_copy(
            update={
                "id": f"overflow-{index:02d}",
                "created_at": source.created_at + timedelta(seconds=index + 1),
            }
        )
        w.seed(copy)
        w.seed(
            review.model_copy(
                update={
                    "id": f"review-{index:02d}",
                    "source_id": copy.id,
                    "source_mission_id": copy.id,
                    "unique_source_key": review_source_key(
                        m.doctor_id, "mission", copy.id, review.source_version, review.review_kind
                    ),
                }
            )
        )
    expected = [(p.id, m.id) for p, m, _ in digest.due(store, w.doctor.scope)[:20]]
    at = schedule(w).next_action_at
    assert at
    clock.now = at
    tick(w)
    sent = [i for i in reuse.listings(store, w.doctor.id) if digest.individual(i)]
    assert len(sent) == 20
    assert all(list(i.question_listing_targets) == expected for i in sent)
    texts = [str(call.payload.get("text")) for call in w.transport.calls]
    for n in range(1, 21):
        assert any(f"/answer {n} … · /close {n} · /defer {n}" in text for text in texts)


def test_six_maximal_rows_with_held_answers_stay_bounded(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.concierge.answer_command import listing_text
    from sanad.domain.entities import QuestionDetails
    from sanad.store.records import Patient

    w = world(store, clock)
    m = question(w)
    assert isinstance(m.details, QuestionDetails)
    row = store.get(w.patient_scope, "patient", m.patient_id)
    assert row
    patient = from_record(row, Patient).model_copy(update={"display_name": "Synthetic " * 20})
    text = "Synthetic question " * 35
    m = m.model_copy(
        update={
            "details": m.details.model_copy(
                update={"question_text": text, "held_answer": "Private held answer " * 30}
            )
        }
    )
    w.seed(
        ReusableAnswer(
            id="long",
            scope=w.doctor.scope,
            question_text=text,
            normalized_question=normalized(text),
            answer_text="Stored answer " * 45,
            source_mission_id=m.id,
            source_patient_id=m.patient_id,
            created_at=clock(),
            updated_at=clock(),
        )
    )
    rendered = listing_text(store, [(patient, m)] * 6, w.doctor, clock(), 1, 34)
    assert len(rendered) < 3500
    assert rendered.count("Proposed reply:") == 6
