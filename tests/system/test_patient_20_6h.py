"""F21 exact patient sequence with overlapping real actions and both store adapters."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from providers.fixtures import png
from store import evidence_fixtures as f
from store.account_fixtures import update
from store.test_browser_upload import UploadWorld
from store.test_patient_browser_controls import headers

from sanad.evidence.doctor import decide
from sanad.media.upload import upload_state
from sanad.store.records import (
    Evidence,
    InboundReceipt,
    MediaWork,
    OperationalClock,
    OutboundIntent,
    from_record,
    record_item,
    to_record,
)
from system.test_walkthrough_20_6e import browser as browser
from system.test_walkthrough_20_6e import clock as clock


def test_f21_page_sequence_while_answer_and_telegram_association_land(
    browser: UploadWorld, caplog: pytest.LogCaptureFixture
) -> None:
    w, client = browser.world, browser.client
    uploaded = client.post("/api/patient/uploads", content=png(), headers=browser.headers())
    assert uploaded.status_code == 202
    message = client.post(
        "/api/patient/messages",
        json={"command_id": "f21-message", "text": "can I double the dose?"},
        headers=headers(browser),
    )
    assert message.status_code == 200
    for action in ("stop", "resume"):
        response = client.post(
            "/api/patient/preferences",
            json={"command_id": "f21-" + action, "reminders": action},
            headers=headers(browser),
        )
        assert response.status_code == 200
    token = response.json()["confirmation_token"]
    assert token
    assert (
        client.post(
            "/api/patient/preferences/confirm",
            json={"command_id": "f21-confirm", "token": token},
            headers=headers(browser),
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/patient/preferences",
            json={"command_id": "f21-quiet", "quiet_hours": ["22:00", "07:00"]},
            headers=headers(browser),
        ).status_code
        == 200
    )
    f.mission(w)
    lab = f.lab(printed_name="Foreign Person")
    f.providers(w, lab, lab)
    assert f.upload(w, id=8100) == "accepted"
    e = f.current(w)
    assert (
        decide(w.runtime.steward, w.owner, e, "confirm_identity", "f21-identity").status
        == "accepted"
    )
    assert w.post(update(w.owner.subject, "/questions", 8101)).status_code == 200
    assert w.post(update(w.owner.subject, "/evidence", 8102)).status_code == 200
    cards = [
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if r.body.get("template_id") == "doctor_evidence_card"
    ]
    card = cards[-1]
    assert card.payload
    markup: Any = card.payload["reply_markup"]
    raw = next(
        b["callback_data"]
        for row in markup["inline_keyboard"]
        for b in row
        if b["text"].startswith("Associate")
    )
    barrier = Barrier(3)

    def polls(path: str) -> list[int]:
        barrier.wait(timeout=15)
        return [client.get(path).status_code for _ in range(12)]

    def actions() -> None:
        barrier.wait(timeout=15)
        assert (
            w.post(
                update(w.owner.subject, "/answer 1 Please follow the recorded plan.", 8103)
            ).status_code
            == 200
        )
        w.tap(raw=raw, id=8104)
        assert w.receipt(8104).state == "completed"

    with ThreadPoolExecutor(max_workers=3) as pool:
        reads = [
            pool.submit(polls, path)
            for path in ("/api/patient/conversation", "/api/patient/uploads")
        ]
        work = pool.submit(actions)
        results = [r.result(timeout=180) for r in reads]
        work.result(timeout=180)
    assert results == [[200] * 12, [200] * 12]
    assert "reason=unhandled" not in caplog.text
    assert f.current(w).association_state == "accepted"
    assert any("Please follow the recorded plan." in str(i.payload) for i in w.patient_intents())
    assert w.profile.routine_contact_enabled
    assert client.get("/api/patient/preferences").json()["quiet_hours"] == ["22:00", "07:00"]


@pytest.mark.parametrize(
    "family,state,expected",
    [
        *[
            ("receipt", s, "received" if s == "pending" else "processing")
            for s in ("pending", "processing", "completed", "needs_attention")
        ],
        *[
            ("media", s, "rejected" if s == "needs_attention" else "processing")
            for s in ("pending", "processing", "completed", "needs_attention")
        ],
        *[
            ("evidence", s, result)
            for s, result in [
                ("unmatched", "needs_doctor_review"),
                ("candidate", "needs_doctor_review"),
                ("accepted_pending_identity", "needs_doctor_review"),
                ("accepted", "accepted"),
                ("rejected", "rejected"),
                ("detached", "not_used"),
            ]
        ],
    ],
)
def test_every_receipt_media_and_evidence_state_projects(
    browser: UploadWorld, family: str, state: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = browser.world
    f.mission(w)
    f.providers(w)
    f.upload(w)
    evidence, media, receipt = f.current(w), f.work(w), w.receipt(1100)
    if family == "receipt":
        receipt = InboundReceipt.model_validate(
            receipt.model_dump()
            | {
                "state": state,
                "processing_claim": None,
                "work_clock": None
                if state == "completed"
                else OperationalClock(next_action_at=w.clock(), work_lane="ingress"),
            }
        )
        result = upload_state(receipt, None, None)
    elif family == "media":
        media = MediaWork.model_validate(
            media.model_dump()
            | {
                "state": state,
                "processing_claim": None,
                "work_clock": None
                if state == "completed"
                else OperationalClock(next_action_at=w.clock(), work_lane="media"),
                "last_error": "unreadable",
                "review_obligation_id": "review",
                "resend_intent_id": "resend",
            }
        )
        result = upload_state(receipt, to_record(media, w.patient_scope), None)
    else:
        retained = state in {"accepted", "accepted_pending_identity"}
        evidence = Evidence.model_validate(
            evidence.model_dump()
            | {
                "association_state": state,
                "accepted_by": w.owner.subject if retained else None,
                "accepted_at": w.clock() if retained else None,
                "flags": ["identity_unverifiable"] if state == "accepted_pending_identity" else [],
                "correction_id": "correction",
                "rejection_reason": "unreadable",
                "supersedes_evidence_version": 1,
            }
        )
        result = upload_state(
            receipt, to_record(media, w.patient_scope), to_record(evidence, w.patient_scope)
        )
    assert result["state"] == expected
    original_get = w.store.get
    original_records = __import__("sanad.web.api_patient", fromlist=["records"]).records

    def get(scope: Any, kind: str, id: str) -> Any:
        if kind == "media_work":
            return None if family == "receipt" else to_record(media, w.patient_scope)
        return original_get(scope, kind, id)

    def rows(store: Any, scope: Any, kind: str) -> Any:
        if kind == "evidence":
            return (to_record(evidence, w.patient_scope),) if family == "evidence" else ()
        return original_records(store, scope, kind)

    monkeypatch.setattr(w.store, "get", get)
    monkeypatch.setattr(
        w.store,
        "patient_receipts",
        lambda *args, **kwargs: (
            (
                to_record(receipt, w.patient_scope).model_copy(
                    update={
                        "media_snapshot": record_item(to_record(media, w.patient_scope))
                        if family != "receipt"
                        else None
                    }
                ),
            ),
            None,
        ),
    )
    monkeypatch.setattr("sanad.web.api_patient.records", rows)
    response = browser.client.get("/api/patient/uploads")
    assert response.status_code == 200 and response.json()["items"][0]["state"] == expected
    conversation = browser.client.get("/api/patient/conversation")
    assert conversation.status_code == 200
    uploaded = next(
        item["upload"] for item in conversation.json()["items"] if item["id"] == receipt.id
    )
    assert uploaded["state"] == expected


def test_unhandled_logs_innermost_class_and_frame_without_payload(
    browser: UploadWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken_projection(*args: Any, **kwargs: Any) -> None:
        raise ValueError("private patient content and credentials")

    monkeypatch.setattr(browser.world.store, "patient_receipts", broken_projection)
    for path in ("/api/patient/conversation", "/api/patient/uploads"):
        response = browser.client.get(path)
        assert response.status_code == 500 and response.json()["reason"] == "unhandled"
    assert (
        "exception_class=ValueError module=system.test_patient_20_6h function=broken_projection"
        in caplog.text
    )
    assert "private patient content" not in caplog.text
