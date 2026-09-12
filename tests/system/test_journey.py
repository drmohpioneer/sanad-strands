"""T38: one continuous English journey, on each store, with real command boundaries."""

from collections import Counter

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate, document
from store import photo_fixtures
from store.test_scribe_voice_web import providers as voice_providers
from store.test_scribe_voice_web import voice

from sanad.concierge import education
from sanad.domain import FollowUpTask, Mission
from sanad.domain.entities import MonitorDetails
from sanad.ops.sweep import sweep_due
from sanad.scribe.card import render_card
from sanad.store._base import StoreBase
from sanad.store.records import from_record
from system.subjects import DOCTOR_A, PATIENT_A, PATIENT_B, PATIENT_C, SubjectWorld


def tick(w: SubjectWorld) -> None:
    assert sweep_due(w.runtime, w.store, elapsed_clock=lambda: 0)["errors"] == []


def missions(w: SubjectWorld) -> dict[str, Mission]:
    return {r.id: from_record(r, Mission) for r in w.rows("mission")}


def mission_kind(w: SubjectWorld, kind: str) -> Mission:
    matching = [m for m in missions(w).values() if m.kind == kind]
    assert len(matching) == 1, (kind, [m.id for m in matching])
    return matching[0]


def assert_kinds(w: SubjectWorld, *kinds: str) -> None:
    assert Counter(m.kind for m in missions(w).values()) == Counter(kinds)


def build_t38_world(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> tuple[SubjectWorld, SubjectWorld, SubjectWorld]:
    clock.now = clock().replace(hour=7)
    a = SubjectWorld.for_subjects(store, clock, doctor=DOCTOR_A, patient=PATIENT_A)
    a.approve(DOCTOR_A, language="en")
    alice = a.named_stub("Synthetic Alice", subject=DOCTOR_A)
    p = a.dictate(
        "Synthetic Alice. Let's give her Forxiga 10 mg. Order Potassium. "
        "Monitor blood pressure 2 times a day for 2 days.",
        {
            "patient": {"name_as_spoken": "Synthetic Alice"},
            "orders": [
                {
                    "action": "start",
                    "drug": "Forxiga",
                    "dose": "10 mg",
                    "action_quote": "Let's give her",
                }
            ],
            "missions": [
                {"kind": "TEST", "text": "Potassium"},
                {"kind": "MONITOR", "text": "Monitor blood pressure 2 times a day for 2 days"},
            ],
        },
        id=10,
    )
    assert not p.blocked("all"), p.issues
    assert all(
        t.resolved.due_reason and t.resolved.escalation_at == t.resolved.due_at for t in p.timings
    )
    assert "TEST: K" in render_card(p)[0]
    a.tap(id=11)
    assert a.scribe.repo.pending(a.doctor.scope) is None
    a.patient_scope = alice.scope
    assert_kinds(a, "TEST", "MONITOR", "MEDICATION")
    assert all(m.state == "awaiting_link" for m in missions(a).values())
    a.bind(alice, id=100)
    assert all(m.state == "open" for m in missions(a).values())

    b = SubjectWorld.for_subjects(store, clock, doctor=DOCTOR_A, patient=PATIENT_B)
    bob = b.named_stub("Synthetic Bob", subject=DOCTOR_A)
    text = "Synthetic Bob. Arrange Cardiology visit. Please bring your diary."
    speech, files, _ = voice_providers(b, text)
    value = {
        "patient": {"name_as_spoken": "Synthetic Bob"},
        "missions": [
            {"kind": "VISIT", "text": "Arrange Cardiology visit"},
            {"kind": "TASK", "text": "bring your diary"},
        ],
    }
    model = ScriptedModel(candidate(value), candidate(value))
    b.scribe.model_factory = lambda registry, role: model
    assert b.post(voice(30)).status_code == 200
    assert len(speech.calls) == 1 and len(files.calls) == 1 and len(model.script.calls) == 2
    assert b.proposal.source_transcript_ref
    b.tap(id=31)
    assert b.scribe.repo.pending(b.doctor.scope) is None
    b.bind(bob, id=110)
    assert_kinds(b, "VISIT", "TASK")

    c = SubjectWorld.for_subjects(store, clock, doctor=DOCTOR_A, patient=PATIENT_C)
    cara = c.enroll_named("Synthetic Cara", id=120)
    c.dictate(
        "Synthetic Cara. Send old prescription.",
        {
            "patient": {"name_as_spoken": "Synthetic Cara"},
            "missions": [{"kind": "SEND_RECORDS", "text": "old prescription"}],
        },
        id=40,
    )
    c.tap(id=41)
    assert_kinds(c, "SEND_RECORDS")
    rx = document(
        document_type="prescription",
        printed_name="Synthetic Cara",
        items=[{"name": "Concor", "dose": "5 mg"}],
    )
    vision, _, _ = photo_fixtures.providers(c, rx, rx)
    assert c.post(photo_fixtures.photo("Synthetic Cara", id=50)).status_code == 200
    assert c.proposal.photo and c.proposal.photo.reads
    assert len(vision.calls) == 2
    assert not c.proposal.blocked("order:0")
    c.tap(id=51)
    assert c.scribe.repo.pending(c.doctor.scope) is None
    heads = c.rows("care_order_head")
    assert len(heads) == 1
    order = c.store.get(
        c.patient_scope, "care_order_version", str(heads[0].body["current_version_id"])
    )
    assert order is not None
    instruction = order.body["structured_instruction"]
    assert isinstance(instruction, dict)
    assert (instruction["drug"], instruction["dose"]) == ("Concor", "5 mg")
    assert len({alice.id, bob.id, cara.id}) == 3

    entry = education.retrieve("What is hypertension?", synthetic=True)[0]
    _, answer = a.send(
        "What is hypertension?",
        {"reply": entry.lines("en")[0], "kind": "education", "needs_doctor": False},
        id=1000,
    )
    assert answer.template_id == "patient_answer" and "Source" in str(answer.payload)
    from system.journey_stages import sent_during

    done_sends = {"MEDICATION": sent_during(a, lambda: a.send("I started Forxiga today", id=1001))}
    assert mission_kind(a, "MEDICATION").state == "fulfilled"
    follow = from_record(a.rows("followup")[0], FollowUpTask)
    assert follow.state == "scheduled" and follow.prompt_at
    monitor = mission_kind(a, "MONITOR")
    assert isinstance(monitor.details, MonitorDetails)
    clock.now = monitor.details.slots[0]
    a.send("BP 120/80", id=1002)
    assert mission_kind(a, "MONITOR").state != "fulfilled"
    done_sends["VISIT"] = sent_during(b, lambda: b.send("booked Cardiology", id=1010))
    assert mission_kind(b, "VISIT").state == "fulfilled"
    done_sends["TASK"] = sent_during(b, lambda: b.send("done bring your diary", id=1011))
    assert mission_kind(b, "TASK").state == "fulfilled"

    # Remaining stages extend this same persisted journey: evidence, accountability,
    # correction and a second approved doctor's refusal against the populated chart.
    from system.journey_stages import evidence_and_accountability, projections_and_isolation

    evidence_and_accountability(a, b, c, follow, monkeypatch, done_sends)
    projections_and_isolation(a, b, c)
    return a, b, c


def test_t38_complete_journey(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_t38_world(store, clock, monkeypatch)


def test_t38_unresolved_barrier_reaches_deadline(store: StoreBase, clock: FakeClock) -> None:
    from resolver.fixtures import Capture

    from sanad.store.records import OutboundIntent

    w = SubjectWorld.for_subjects(store, clock, doctor=DOCTOR_A, patient=PATIENT_A)
    w.approve(DOCTOR_A, language="en")
    w.enroll_named("Synthetic Barrier Patient", id=100)
    w.dictate(
        "Synthetic Barrier Patient. Order Potassium.",
        {
            "patient": {"name_as_spoken": "Synthetic Barrier Patient"},
            "missions": [{"kind": "TEST", "text": "Potassium"}],
        },
        id=10,
    )
    w.tap(id=11)
    assert_kinds(w, "TEST")
    original = mission_kind(w, "TEST")
    capture = Capture(places={"elements": []})
    w.concierge.places_provider = capture.provider()
    w.send(
        "The Potassium lab is too expensive",
        {"step": "ask_patient", "question": "Which area or neighbourhood should I search near?"},
        id=1000,
    )
    _, reply = w.send(
        "Nasr City",
        {"step": "find_places", "question": "What practical difficulty do you need help with?"},
        id=1001,
    )
    current = mission_kind(w, "TEST")
    assert current.barrier_attempts[-1].outcome == "places_unavailable"
    assert current.barrier_attempts[-1].searches_spent == 2
    assert len(capture.requests) == 4
    assert "could not verify any options" in str(reply.payload).lower()
    assert current.state == "blocked" and current.due_at == original.due_at
    clock.now = original.escalation_at
    tick(w)
    current = mission_kind(w, "TEST")
    assert current.state == "overdue" and current.fulfilled_at is None
    assert current.barrier_attempts[-1].state == "unresolved"
    review = next(
        r
        for r in w.rows("review")
        if r.body["source_id"] == current.id and r.body["review_kind"] == "unmet_objective"
    )
    assert review.body["owner_doctor_id"] == w.doctor.id and review.body["state"] == "open"
    assert review.body["work_clock"]
    notices = [
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if r.body["notification_purpose"] == "DEADLINE"
        and r.body["review_obligation_id"] == review.id
    ]
    assert len(notices) == 1 and notices[0].status == "provider_accepted"
    assert any(ref.id == current.id for ref in notices[0].source_versions)
    assert any(
        "unresolved" in str(call.payload).lower() and "cost" in str(call.payload)
        for call in w.transport.calls
    )
