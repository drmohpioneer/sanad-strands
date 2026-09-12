"""F14: both real ingress adapters reach a solicited inbox decision and safe replay."""

from pathlib import Path
from typing import cast

import pytest
from providers.fixtures import FakeTelegramFiles, ScriptedConverter, ScriptedVision
from store import evidence_fixtures as evidence
from store.account_fixtures import APPLICANT, update
from store.test_browser_upload import UploadWorld
from store.test_concierge_media import media_message

from sanad.channels.telegram.router import route_receipt
from sanad.domain import EvidencePredicate, Principal, TaskDetails
from sanad.media.retrieve import MediaRetriever
from sanad.media.storage import S3MediaStore
from sanad.media.upload import patient_source
from sanad.media.vision import VisionAdapter
from sanad.ops.sweep import sweep_due
from sanad.store.records import InboundReceipt
from system.test_walkthrough_20_6e import browser as browser
from system.test_walkthrough_20_6e import clock as clock


@pytest.mark.parametrize("channel", ["telegram", "browser"])
def test_upload_inbox_accept_and_callback_replay(browser: UploadWorld, channel: str) -> None:
    w = browser.world
    evidence.mission(
        w,
        kind="TASK",
        order_refs=(),
        details=TaskDetails(
            category="proof", instruction="Send your document", completion_rule="doctor_acceptance"
        ),
        objective_predicate=EvidencePredicate(evaluator="task_evidence"),
    )
    reading = (
        evidence.lab()
        if channel == "telegram"
        else evidence.lab(
            document_type="prescription", items=[{"name": "Exforge", "dose": "5/160"}]
        )
    )
    vision = ScriptedVision(reading, reading)
    data = Path(
        "tests/data/09b/" + ("lab_synthetic.png" if channel == "telegram" else "rx_synthetic.png")
    ).read_bytes()
    telegram = FakeTelegramFiles(data)
    storage = cast(S3MediaStore, browser.ingress.storage)

    def media(receipt: InboundReceipt, actor: Principal) -> MediaRetriever:
        binding = w.store.authorize(w.runtime.settings.bot_id, actor.subject).binding
        assert binding
        return MediaRetriever(
            w.runtime.steward,
            storage,
            patient_source(receipt, actor, w.store, storage, telegram),
            ScriptedConverter(),
            w.patient_scope,
            actor,
            lambda: w.concierge.valid(actor, binding),
            w.runtime.steward.policy_provider(w.patient_scope),
        )

    w.concierge.media_factory = media
    w.concierge.vision_factory = lambda source: VisionAdapter(
        vision, source, w.runtime.safety_policy
    )
    from sanad.store.keys import ScopedKey

    def submit(key: ScopedKey) -> None:
        route_receipt(w.runtime, key)

    browser.ingress.submit = submit
    if channel == "browser":
        response = browser.client.post(
            "/api/patient/uploads",
            content=data,
            headers=browser.headers("Synthetic prescription for doctor review"),
        )
        assert response.status_code == 202
        receipt_id = response.json()["receipt_id"]
    else:
        assert w.post(media_message("photo", 6100)).status_code == 200
        receipt_id = w.receipt(6100).id
    assert any(r.body["receipt_id"] == receipt_id for r in w.rows("media_work"))
    sweep_due(w.runtime, w.store, upload_handler=browser.ingress.recover)
    current = evidence.current(w)
    assert len(vision.calls) == 2 and current.association_state == "candidate"
    assert evidence.work(w).state == "completed"
    uploads = browser.client.get("/api/patient/uploads").json()
    assert any(r["id"] == receipt_id and r["state"] == "needs_doctor_review" for r in uploads), (
        uploads
    )
    assert w.post(update(APPLICANT, "/inbox", 6200)).status_code == 200
    card = next(
        r for r in w.rows("outbound_intent") if r.body.get("template_id") == "doctor_evidence_card"
    )
    payload = card.body["payload"]
    assert isinstance(payload, dict) and isinstance(payload["reply_markup"], dict)
    rows = payload["reply_markup"]["inline_keyboard"]
    assert isinstance(rows, list)
    buttons = [b for row in rows if isinstance(row, list) for b in row if isinstance(b, dict)]
    raw = str(next(b["callback_data"] for b in buttons if str(b["text"]).startswith("Accept")))
    w.tap(raw=raw, id=6201)
    assert evidence.current(w).association_state == "accepted"
    assert all(
        r.body["state"] == "resolved"
        for r in w.rows("review")
        if r.body["review_kind"] == "evidence_association"
    )
    version = evidence.current(w).version
    w.tap(raw=raw, id=6202)
    assert w.receipt(6202).state == "completed" and evidence.current(w).version == version
    assert any("already handled" in str(c.payload) for c in w.transport.calls)
