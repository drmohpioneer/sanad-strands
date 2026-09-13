"""F14: both real ingress adapters reach a solicited inbox decision and safe replay."""

from datetime import timedelta
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
from sanad.models.io import ModelUnavailable
from sanad.ops.sweep import sweep_due
from sanad.store.records import InboundReceipt
from system.test_walkthrough_20_6e import browser as browser
from system.test_walkthrough_20_6e import clock as clock


@pytest.mark.parametrize("channel", ["telegram", "browser", "telegram_identity", "browser_retry"])
def test_upload_inbox_accept_and_callback_replay(
    browser: UploadWorld, channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        if channel.startswith("telegram")
        else evidence.lab(
            document_type="prescription", items=[{"name": "Exforge", "dose": "5/160"}]
        )
    )
    if channel == "telegram_identity":
        reading = evidence.lab(printed_name="Foreign Person")
    vision = ScriptedVision(
        *([ModelUnavailable(reason="unavailable")] * 12 if channel == "browser_retry" else []),
        reading,
        reading,
    )
    data = Path(
        "tests/data/09b/"
        + ("lab_synthetic.png" if channel.startswith("telegram") else "rx_synthetic.png")
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
    if channel.startswith("browser"):
        from sanad.store.records import Claim, MediaWork

        original_commit = MediaRetriever._commit
        interrupted = False

        def interrupt_after_normalize(
            self: MediaRetriever,
            work: MediaWork,
            claim: Claim | None = None,
            *,
            failure: str | None = None,
        ) -> bool:
            nonlocal interrupted
            accepted = original_commit(self, work, claim, failure=failure)
            if accepted and work.stage == "extract" and not interrupted:
                interrupted = True
                raise RuntimeError("synthetic restart after normalization")
            return accepted

        if channel == "browser":
            monkeypatch.setattr(MediaRetriever, "_commit", interrupt_after_normalize)

        response = browser.client.post(
            "/api/patient/uploads",
            content=data,
            headers=browser.headers("Synthetic prescription for doctor review"),
        )
        assert response.status_code == 202
        receipt_id = response.json()["receipt_id"]
        sweep_due(w.runtime, w.store, upload_handler=browser.ingress.recover)
        pending = evidence.work(w)
        if channel == "browser":
            assert interrupted and pending.stage == "extract" and pending.work_clock
            assert pending.work_clock.next_action_at <= w.clock()
            assert len(vision.calls) == 0
        else:
            for count, minutes in enumerate((1, 5, 15), start=1):
                pending = evidence.work(w)
                assert pending.state == "pending" and pending.work_clock
                assert pending.work_clock.next_action_at - w.clock() < timedelta(minutes=minutes)
                assert pending.work_clock.next_action_at - w.clock() > timedelta(
                    minutes=minutes - 1
                )
                assert len(vision.calls) == count * 4
                sweep_due(w.runtime, w.store, upload_handler=browser.ingress.recover)
                assert len(vision.calls) == count * 4  # No read before the retry clock.
                w.clock.advance(timedelta(minutes=minutes))
                if count < 3:
                    sweep_due(w.runtime, w.store, upload_handler=browser.ingress.recover)
    else:
        assert w.post(media_message("photo", 6100)).status_code == 200
        receipt_id = w.receipt(6100).id
    assert any(r.body["receipt_id"] == receipt_id for r in w.rows("media_work"))
    sweep_due(w.runtime, w.store, upload_handler=browser.ingress.recover)
    current = evidence.current(w)
    assert len(vision.calls) == (14 if channel == "browser_retry" else 2)
    assert current.association_state == "candidate"
    assert evidence.work(w).state == "completed"
    if channel == "telegram_identity":
        name_card = next(
            i for i in reversed(w.patient_intents()) if i.payload and "reply_markup" in i.payload
        )
        assert "Is this your document?" in str(name_card.payload)
        lease = w.store.acquire_patient(
            w.patient_scope, "parallel-turn", w.clock(), timedelta(minutes=5)
        )
        assert lease
        w.press(name_card, id=6101)
        assert w.receipt(6101).state == "pending" and w.receipt(6101).processing_claim is None
        w.store.release_patient(lease)
        w.press(name_card, id=6101)
        assert w.receipt(6101).state == "completed"
        assert evidence.current(w).patient_match_provenance == "patient_choice"
    uploads = browser.client.get("/api/patient/uploads").json()["items"]
    assert any(r["id"] == receipt_id and r["state"] == "needs_doctor_review" for r in uploads), (
        uploads
    )
    if channel == "telegram_identity":
        from sanad.evidence.doctor import decide

        assert (
            decide(
                w.runtime.steward, w.owner, evidence.current(w), "confirm_identity", "confirm-name"
            ).status
            == "accepted"
        )
    assert w.post(update(APPLICANT, "/inbox", 6200)).status_code == 200
    card = next(
        r
        for r in reversed(w.rows("outbound_intent"))
        if r.body.get("template_id") == "doctor_evidence_card"
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
