"""Later T38 stages retain the same three patients, database and clinical history."""

from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from typing import cast

import pytest
from providers.fixtures import FakeTelegramFiles, ScriptedConverter, ScriptedVision, document, png
from pydantic import JsonValue
from resolver.fixtures import Capture
from store.account_fixtures import update
from store.contact_fixtures import required
from store.login_fixtures import browser_login
from store.test_admin_boundary import admin_path, headers
from store.test_browser_upload import mount
from store.test_corrections_19 import act

from sanad.channels.telegram.router import route_receipt
from sanad.channels.transport import CapturedSend
from sanad.contact import bundle, question_digest
from sanad.contact.bundle import ENGLISH_KINDS
from sanad.domain import FollowUpTask, Principal, ReviewObligation, VersionRef
from sanad.liaison.records import Notice
from sanad.media.retrieve import MediaRetriever
from sanad.media.storage import MediaScope, S3MediaStore
from sanad.media.upload import patient_source
from sanad.media.vision import VisionAdapter
from sanad.steward.types import records
from sanad.store.keys import ScopedKey
from sanad.store.protocol import Store
from sanad.store.records import (
    Evidence,
    InboundReceipt,
    OutboundIntent,
    WebSession,
    from_record,
    to_record,
)
from system.subjects import DOCTOR_A, DOCTOR_B, PATIENT_A, SubjectWorld
from system.test_journey import mission_kind, missions, tick

EXPECTED_PLACE_NAMES = (
    "Synthetic Cedar Laboratory",
    "Synthetic Palm Clinic",
    "Synthetic Nile Hospital",
)


def sent_during(peer: SubjectWorld, step: Callable[[], object]) -> tuple[int, list[CapturedSend]]:
    before = len(peer.transport.calls)
    step()
    return before, peer.transport.calls[before:]


def bound_text(
    peer: SubjectWorld,
    intent: OutboundIntent,
    sends: tuple[int, list[CapturedSend]],
    *,
    expect_notice: bool,
) -> str:
    assert intent.status == "provider_accepted"
    message_id = required(intent.accepted_message_id)
    assert message_id.startswith("captured-")
    position = message_id.removeprefix("captured-")
    assert position.isascii() and position.isdecimal()
    index = int(position) - 1
    start, step_sends = sends
    assert 0 <= start <= index < start + len(step_sends) <= len(peer.transport.calls)
    call = peer.transport.calls[index]
    assert call.recipient_ref == intent.recipient_ref
    text = call.payload["text"]
    if intent.audience == "doctor":
        notices = [
            from_record(row, Notice)
            for row in records(peer.store, peer.doctor.scope, "liaison_notice")
            if row.body["intent_id"] == intent.id
        ]
        assert len(notices) == int(expect_notice), (intent.template_id, intent.id, len(notices))
        if expect_notice:
            assert notices[0].payload["text"] == text
    else:
        assert intent.audience == "patient"
        binding = required(
            peer.store.authorize(peer.runtime.settings.bot_id, peer.patient_subject).binding
        )
        assert intent.recipient_ref == binding.private_chat_id
        assert intent.delivered_text == text
    assert isinstance(text, str) and text
    return text


def upload(w: SubjectWorld, reading: str, *, browser: bool, id: int) -> Evidence:
    with w.client() as client:
        assert browser_login(client, w.login_path(w.patient_subject, id=id)).status_code == 303
        session = required(w.login.session(client.cookies["sanad_session"]))
        u = mount(w, client, WebSession.model_validate(session.model_dump()))
        storage = cast(S3MediaStore, u.ingress.storage)
        previous = getattr(w.app.state, "media_store", None)

        class RetainedMedia:
            def get(self, scope: MediaScope, reference: str, limit: int) -> bytes:
                if reference.startswith("s3://" + storage.bucket + "/"):
                    return storage.get(scope, reference, limit)
                assert isinstance(previous, S3MediaStore)
                return previous.get(scope, reference, limit)

        # The doctor photo and later upload fixtures use separate private buckets.
        w.app.state.media_store = RetainedMedia()
        vision = ScriptedVision(reading, reading)
        files = FakeTelegramFiles(png(3 if browser else 4, 3))

        def factory(receipt: InboundReceipt, actor: Principal) -> MediaRetriever:
            binding = required(w.store.authorize(w.runtime.settings.bot_id, actor.subject).binding)
            return MediaRetriever(
                w.runtime.steward,
                storage,
                patient_source(receipt, actor, w.store, storage, files),
                ScriptedConverter(),
                w.patient_scope,
                actor,
                lambda: w.concierge.valid(actor, binding),
                w.runtime.steward.policy_provider(w.patient_scope),
            )

        w.concierge.media_factory = factory
        w.concierge.vision_factory = lambda source: VisionAdapter(
            vision, source, w.runtime.safety_policy
        )

        def submit(key: ScopedKey) -> None:
            route_receipt(w.runtime, key)

        u.ingress.submit = submit
        if browser:
            response = client.post("/api/patient/uploads", content=png(3, 3), headers=u.headers())
            assert response.status_code == 202
            receipt_id = response.json()["receipt_id"]
        else:
            body = update(w.patient_subject, "", id + 1)
            message = body["message"]
            assert isinstance(message, dict)
            message.pop("text")
            message["photo"] = [
                {"file_id": "synthetic-record", "file_unique_id": "record", "width": 4, "height": 3}
            ]
            assert w.post(body).status_code == 200
            receipt_id = w.receipt(id + 1).id
        assert not vision.calls
        tick(w)
        assert len(vision.calls) == 2 and len(files.calls) == (0 if browser else 1)
        assert any(
            r.body["observation_id"] == receipt_id and r.body["association_state"] == "accepted"
            for r in w.rows("evidence")
        ), [
            (
                r.body.get("association_state"),
                r.body.get("flags"),
                r.body.get("category"),
                r.body.get("predicate_result"),
            )
            for r in w.rows("evidence")
        ]
        value = next(
            from_record(r, Evidence)
            for r in w.rows("evidence")
            if r.body["observation_id"] == receipt_id and r.body["association_state"] == "accepted"
        )
        assert value.observation_id == receipt_id
        assert (
            required(w.store.get_mission(w.patient_scope, value.mission_id or "")).state
            == "fulfilled"
        )
        return value


def evidence_and_accountability(
    a: SubjectWorld,
    b: SubjectWorld,
    c: SubjectWorld,
    follow: FollowUpTask,
    monkeypatch: pytest.MonkeyPatch,
    done_sends: dict[str, tuple[int, list[CapturedSend]]],
) -> None:
    # Barrier before evidence: a bounded practical question, original deadline retained.
    capture = Capture()
    a.concierge.places_provider = capture.provider()
    due = mission_kind(a, "TEST").due_at
    _, barrier = a.send(
        "The Potassium lab is too expensive",
        {"step": "ask_patient", "question": "Which area or neighbourhood should I search near?"},
        id=1020,
    )
    test = mission_kind(a, "TEST")
    assert test.barrier_attempts and test.due_at == due
    assert not capture.requests and "Which area" in str(barrier.payload)
    practical_sends = sent_during(
        a,
        lambda: a.send(
            "Nasr City",
            {"step": "find_places", "question": "What practical difficulty do you need help with?"},
            id=1022,
        ),
    )
    practical = next(
        i for i in a.patient_intents() if "patient-turn:" + a.receipt(1022).id in i.source_event_ids
    )
    practical_text = bound_text(a, practical, practical_sends, expect_notice=False)
    helped = mission_kind(a, "TEST")
    assert len(capture.requests) == 2
    assert helped.barrier_attempts[-1].outcome == "places_offered"
    assert EXPECTED_PLACE_NAMES
    assert tuple(place.name for place in helped.barrier_attempts[-1].places) == EXPECTED_PLACE_NAMES
    assert all(name in practical_text for name in EXPECTED_PLACE_NAMES)
    assert "These places are near the area you named." in practical_text
    assert (
        "I cannot see their prices or stock, cannot confirm they have what you need, "
        "and have not booked anything."
    ) in practical_text
    assert helped.due_at == due and helped.state == "blocked"
    question_text = "Where should I bring my appointment diary?"
    b.send(question_text, id=1021)
    question = mission_kind(b, "QUESTION")
    assert question.state == "open" and question.grace_seconds == 0
    assert not [
        r for r in b.rows("outbound_intent") if r.body["notification_purpose"] == "DEADLINE"
    ]
    lab = document(
        printed_name="Synthetic Alice",
        items=[{"name": "Potassium", "value": "4.5", "unit": "mmol/L"}],
    )
    accepted = upload(a, lab, browser=True, id=1030)
    assert mission_kind(a, "TEST").fulfillment_validity == "valid"
    assert any(
        r.body["review_kind"] == "result_review" and r.body["state"] == "open"
        for r in a.rows("review")
    )
    old_heads = c.rows("care_order_head")
    old_rx = document(
        document_type="prescription",
        printed_name="Synthetic Cara",
        printed_date="2025-01-01",
        items=[{"name": "Concor", "dose": "2.5 mg"}],
    )
    upload(c, old_rx, browser=False, id=1040)
    assert c.rows("care_order_head") == old_heads
    assert mission_kind(c, "SEND_RECORDS").state == "fulfilled"

    a.clock.now = required(follow.prompt_at)
    tick(a)
    current_follow = from_record(a.rows("followup")[0], FollowUpTask)
    assert current_follow.state == "waiting_response"
    a.send("The medicine is going well", id=1050)
    assert from_record(a.rows("followup")[0], FollowUpTask).state == "fulfilled"
    done = [
        r for r in a.rows("outbound_intent") if r.body["notification_purpose"] == "DONE:FULFILLMENT"
    ]
    assert len(done) == 3
    from sanad.concierge.records import ReportFactPayload
    from sanad.scribe.records import ClinicalFact

    for w, kind, report_kind, text, receipt_id in (
        (a, "MEDICATION", "medication_start", "I started Forxiga today", 1001),
        (b, "VISIT", "visit_booking", "booked Cardiology", 1010),
        (b, "TASK", "task_done", "done bring your diary", 1011),
    ):
        mission = mission_kind(w, kind)
        facts = [from_record(r, ClinicalFact) for r in w.rows("clinical_fact")]
        reports = [
            f
            for f in facts
            if isinstance(f.payload, ReportFactPayload) and f.payload.report_kind == report_kind
        ]
        assert len(reports) == 1
        fact = reports[0]
        assert isinstance(fact.payload, ReportFactPayload)
        assert fact.payload.text == text
        assert required(fact.payload.target_ref).id == mission.id
        assert fact.provenance.source_kind == "patient_report"
        assert fact.provenance.source_observation_id == w.receipt(receipt_id).id
        assert fact.provenance.actor_id == w.patient_subject
        notices = [
            from_record(r, OutboundIntent)
            for r in w.rows("outbound_intent")
            if r.body["notification_purpose"] == "DONE:FULFILLMENT"
        ]
        notice = next(i for i in notices if any(ref.id == mission.id for ref in i.source_versions))
        assert mission.danger_history is False
        text = bound_text(
            w,
            notice,
            done_sends[kind],
            expect_notice=mission.kind in {"TEST", "MONITOR", "SEND_RECORDS"}
            or mission.danger_history,
        )
        assert mission.title in text
        assert (
            "Self-reported; pending doctor acceptance."
            if kind == "TASK"
            else "does not confirm clinical review"
        ) in text
    final_follow = from_record(a.rows("followup")[0], FollowUpTask)
    report = from_record(
        required(a.store.get(a.patient_scope, "clinical_fact", final_follow.source_report_ids[-1])),
        ClinicalFact,
    )
    assert isinstance(report.payload, ReportFactPayload)
    assert report.payload.text == "The medicine is going well"
    assert report.provenance.source_observation_id == a.receipt(1050).id
    assert report.provenance.source_kind == "patient_report"
    assert accepted.extracted_values[0].value == "4.5"
    assert accepted.extracted_values[0].unit == "mmol/L"
    assert accepted.provenance.source_observation_id == accepted.observation_id
    assert any(ref.fact_id == accepted.evidence_id for ref in mission_kind(a, "TEST").evidence_refs)

    assert any(
        any(
            ref.entity_type == "followup" and ref.id == follow.id
            for ref in from_record(r, OutboundIntent).source_versions
        )
        for r in done
    )
    # One of four MONITOR slots is present; elapsed time cannot fill the other three.
    assert mission_kind(a, "MONITOR").state == "overdue"
    notices = [
        from_record(r, OutboundIntent)
        for r in a.rows("outbound_intent")
        if r.body["notification_purpose"] == "DEADLINE"
    ]
    assert notices and any(i.status == "provider_accepted" for i in notices)

    a.clock.now = max(a.clock(), question.due_at)
    tick(b)
    armed = required(question_digest.load(b.store, b.doctor.scope))
    a.clock.now = required(armed.next_action_at)
    tick(b)
    digests = [
        from_record(r, OutboundIntent)
        for r in records(b.store, b.doctor.scope, "outbound_intent")
        if r.body.get("template_id") == "doctor_question_digest"
    ]
    assert len(digests) == 1 and digests[0].status == "provider_accepted"
    assert (b.patient_scope.patient_id, question.id) in digests[0].question_listing_targets
    b.doctor_says("/answer 1 Please bring your diary to the clinic.", id=1060)
    assert mission_kind(b, "QUESTION").state == "fulfilled"
    b.doctor_says("/reuse", id=1061)
    assert len(list(records(b.store, b.doctor.scope, "reusable_answer"))) == 1
    model, reused = b.send(question_text, id=1062)
    assert not model.script.calls and "From your doctor" in str(reused.payload)
    assert len([r for r in b.rows("mission") if r.body["kind"] == "QUESTION"]) == 1

    reviews_before = {r.id for r in a.rows("review")}
    old = to_record(accepted, accepted.scope)
    assert (
        act(
            a,
            {
                "type": "CorrectRecord",
                "operation": "detach",
                "predecessor": old.ref.model_dump(),
                "reason": "Synthetic wrong attachment",
            },
            "t38-correction",
        ).status
        == "accepted"
    )
    assert a.store.get(a.patient_scope, "evidence", old.id) == old
    assert mission_kind(a, "TEST").fulfillment_validity == "invalidated_pending_review"
    corrections = a.rows("correction")
    assert len(corrections) == 1
    correction = corrections[0]
    new_reviews = [
        from_record(r, ReviewObligation) for r in a.rows("review") if r.id not in reviews_before
    ]
    assert len(new_reviews) == 1
    review = new_reviews[0]
    assert (review.source_type, review.source_id, review.review_kind, review.state) == (
        "correction",
        correction.id,
        "correction_disposition",
        "open",
    )
    assert review.owner_doctor_id == a.doctor.id
    assert review.review_at > a.clock() and review.work_clock is not None
    assert review.work_clock.next_action_at == review.review_at
    a.clock.now = review.review_at
    tick(a)
    stamped = from_record(
        required(a.store.get(a.patient_scope, "review", review.id)), ReviewObligation
    )
    assert stamped.state == "open" and stamped.work_clock is not None
    assert stamped.work_clock.next_action_at > a.clock()
    a.clock.advance(timedelta(days=7))
    owed = {
        r.id: (name, r)
        for w, name in ((a, "Synthetic Alice"), (b, "Synthetic Bob"), (c, "Synthetic Cara"))
        for row in w.rows("review")
        if (r := from_record(row, ReviewObligation)).state != "resolved"
        and r.first_notice_at is not None
        and r.first_notice_at <= a.clock() - timedelta(days=7)
    }
    assert owed and len(owed) <= 20
    expected_lines = [
        f"{name}: {ENGLISH_KINDS[r.review_kind]}: "
        f"{(a.clock() - required(r.first_notice_at)).days} days since first notice"
        for name, r in owed.values()
    ]
    observed: dict[str, tuple[VersionRef, ...]] = {}
    original_snapshot = bundle.payload_snapshot

    def record_snapshot(
        store: Store, intent: OutboundIntent, now: object
    ) -> tuple[dict[str, JsonValue], tuple[VersionRef, ...]]:
        from datetime import datetime

        assert isinstance(now, datetime)
        payload, refs = original_snapshot(store, intent, now)
        observed[intent.id] = refs
        return payload, refs

    with monkeypatch.context() as patch:
        patch.setattr(bundle, "payload_snapshot", record_snapshot)
        bundle_sends = sent_during(a, lambda: tick(a))
    bundles = [
        from_record(r, OutboundIntent)
        for r in records(a.store, a.doctor.scope, "outbound_intent")
        if r.body.get("template_id") == "doctor_weekly_bundle"
    ]
    latest = max(bundles, key=lambda i: i.created_at)
    assert latest.status == "provider_accepted"
    assert {ref.id for ref in observed[latest.id]} == set(owed)
    bundle_text = bound_text(a, latest, bundle_sends, expect_notice=True)
    heading, *item_lines = bundle_text.split("\n\n", 1)[0].splitlines()
    assert heading == "Items still needing follow-up:"
    rendered_lines = [
        line
        for line in item_lines
        if not (line.startswith("and ") and line.endswith(" more items"))
    ]
    assert Counter(expected_lines) == Counter(rendered_lines)
    # Resolve the exact correction review without declaring detached evidence valid.
    current_review = required(a.store.get(a.patient_scope, "review", review.id))
    assert (
        act(
            a,
            {
                "type": "CorrectionResponse",
                "correction_id": correction.id,
                "review_ref": current_review.ref.model_dump(),
                "reason": "Reviewed wrong attachment; collection remains invalidated",
            },
            "t38-reviewed-correction",
        ).status
        == "accepted"
    )
    resolved = from_record(
        required(a.store.get(a.patient_scope, "review", review.id)), ReviewObligation
    )
    assert resolved.state == "resolved" and resolved.work_clock is None
    assert resolved.resolved_by == a.owner.subject
    assert mission_kind(a, "TEST").fulfillment_validity == "invalidated_pending_review"


def projections_and_isolation(a: SubjectWorld, b: SubjectWorld, c: SubjectWorld) -> None:
    with a.client() as client:
        assert browser_login(client, a.login_path(DOCTOR_A, id=2000)).status_code == 303
        listed = client.get("/api/patients").json()
        assert {r["patient_id"] for r in listed} == {w.patient_scope.patient_id for w in (a, b, c)}
        assert {r["display_name"] for r in listed} == {
            "Synthetic Alice",
            "Synthetic Bob",
            "Synthetic Cara",
        }
        for path, view in (
            ("/a", "patients"),
            ("/a/inbox", "inbox"),
            ("/a/history", "history"),
            ("/a/preferences", "preferences"),
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert f'data-view="{view}"' in response.text and a.doctor.name in response.text
        for w in (a, b, c):
            detail = client.get("/a/patients/" + w.patient_scope.patient_id)
            assert detail.status_code == 200
            assert f'data-patient="{w.patient_scope.patient_id}"' in detail.text
            record = client.get("/api/patients/" + w.patient_scope.patient_id).json()
            assert {m["id"] for m in record["missions"]} == set(missions(w))
            assert (
                record["display_name"]
                == required(w.claims.patient(w.doctor.id, w.patient_scope.patient_id)).display_name
            )
            if w is a:
                test = next(m for m in record["missions"] if m["kind"] == "TEST")
                assert test["fulfillment_validity"] == "invalidated_pending_review"
                correction = w.rows("correction")[0]
                assert any(
                    r["source_id"] == correction.id and r["state"] == "resolved"
                    for r in record["review_history"]
                )
                assert (
                    record["orders"][0]["current_version"]["structured_instruction"]["dose"]
                    == "10 mg"
                )
            elif w is b:
                assert all(m["state"] == "fulfilled" for m in record["missions"])
            else:
                assert record["medication_list_seen"][0]["active_order"] is False
                assert (
                    record["orders"][0]["current_version"]["structured_instruction"]["dose"]
                    == "5 mg"
                )
        response = client.get("/api/browser/reviews")
        assert response.status_code == 200
        assert any(
            r["source_id"] == mission_kind(a, "MONITOR").id and r["state"] == "open"
            for r in response.json()
        )
        response = client.get("/api/browser/reviews?history=true")
        assert response.status_code == 200
        assert response.json() == []  # This endpoint contains only resolved unassigned reviews.
        response = client.get("/api/questions")
        assert response.status_code == 200 and response.json()["questions"] == []
        response = client.get("/api/preferences")
        assert response.status_code == 200
        assert (
            response.json()["language"],
            response.json()["digest_time"],
            response.json()["digest_packing"],
        ) == ("en", "20:00", "one")
    for index, w in enumerate((a, b, c)):
        with w.client() as client:
            assert (
                browser_login(client, w.login_path(w.patient_subject, id=2010 + index)).status_code
                == 303
            )
            for path in (
                "/pp",
                "/api/patient/me",
                "/api/patient/plan",
                "/api/patient/conversation",
                "/api/patient/uploads",
                "/api/patient/preferences",
            ):
                response = client.get(path)
                assert response.status_code == 200, (path, response.status_code)
                name = required(
                    w.claims.patient(w.doctor.id, w.patient_scope.patient_id)
                ).display_name
                if path == "/pp":
                    assert name in response.text and 'id="patient-upload-form"' in response.text
                    if w is a:
                        assert "Forxiga" in response.text and "10 mg" in response.text
                    elif w is c:
                        assert "Concor" in response.text and "5 mg" in response.text
                elif path == "/api/patient/me":
                    assert response.json()["display_name"] == name
                elif path == "/api/patient/plan":
                    plan = response.json()
                    assert plan["doctor_name"] == w.doctor.name
                    expected = (
                        [("Forxiga", "10 mg")] if w is a else [("Concor", "5 mg")] if w is c else []
                    )
                    assert [(o["drug"], o["dose"]) for o in plan["orders"]] == expected
                elif path == "/api/patient/conversation":
                    items = response.json()["items"]
                    if w is a:
                        assert any(i.get("text") == "The medicine is going well" for i in items)
                    elif w is b:
                        assert any(i.get("text") == "done bring your diary" for i in items)
                    else:
                        assert any(
                            i.get("upload", {}).get("state") == "accepted"
                            for i in items
                            if i.get("upload")
                        )
                elif path == "/api/patient/uploads":
                    expected_states = ["not_used"] if w is a else ["accepted"] if w is c else []
                    assert [u["state"] for u in response.json()] == expected_states
                    if w is not b:
                        assert response.json()[0]["id"] in {
                            r.body["observation_id"] for r in w.rows("evidence")
                        }
                elif path == "/api/patient/preferences":
                    assert response.json()["quiet_hours"] == ["22:00", "08:00"]
                    assert response.json()["timezone"] == "Africa/Cairo"
                assert all(
                    other not in response.text
                    for other in ("Synthetic Alice", "Synthetic Bob", "Synthetic Cara")
                    if other
                    != required(
                        w.claims.patient(w.doctor.id, w.patient_scope.patient_id)
                    ).display_name
                )
    application = a.apply(DOCTOR_B)
    with a.client() as admin:
        assert browser_login(admin, admin_path(a)).status_code == 303
        admin_page = admin.get("/admin")
        assert admin_page.status_code == 200 and "Sign out everywhere" in admin_page.text
        applications = admin.get("/api/admin/applications")
        assert applications.status_code == 200
        pending = next(item for item in applications.json() if item["id"] == application.id)
        assert pending["status"] == "pending"
        assert (
            admin.post(
                f"/api/admin/applications/{application.id}/approve",
                headers=headers(admin),
                json={"command_id": "t38-approve-b", "expected_version": application.version},
            ).status_code
            == 200
        )
    second = SubjectWorld.for_subjects(a.store, a.clock, doctor=DOCTOR_B, patient=PATIENT_A)
    assert second.doctor.id != a.doctor.id
    from system.test_adversarial import probe_foreign_chart

    for index, victim in enumerate((a, b, c)):
        probe_foreign_chart(second, victim, login_id=3000 + index)
