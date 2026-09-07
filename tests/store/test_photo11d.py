from datetime import timedelta
from typing import Any

import pytest
from harness import FakeClock
from providers.fixtures import document
from providers.photo11d_fixtures import paper
from providers.test_photo11d import Readers

from sanad.media.vision import VisionAdapter
from sanad.scribe.crosscheck import (
    AGREEMENT_WARNING,
    COLUMN_CAPTION,
    HANDWRITING_REPLY,
    SINGLE_READER_WARNING,
    render_card,
)
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.keys import IntakeScope
from sanad.store.records import MediaWork, OutboundIntent, StoredRecord, from_record
from store.photo_fixtures import photo, providers
from store.scribe_fixtures import ScribeWorld
from store.test_scribe_photos import intake_button


@pytest.fixture
def world(store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> ScribeWorld:
    monkeypatch.setattr("sanad.api.internal.dispatch_inline", lambda *args, **kwargs: 0)
    value = ScribeWorld.create(store, clock)
    value.approve()
    value.named_stub("أحمد رضا")
    return value


def prescription() -> str:
    return document(
        document_type="prescription",
        printed_name=None,
        items=[
            {"name": "Concor", "dose": "5 mg", "frequency": "اليوم"},
            {"name": "SyntheticSecond", "dose": "10 mg", "timing": "قبل الغذاء"},
        ],
    )


def disagreeing_prescription() -> str:
    return document(
        document_type="prescription",
        printed_name=None,
        items=[
            {"name": "Concor", "dose": "5 mg"},
            {"name": "Quartz", "dose": "10 mg"},
            {"name": "Winter", "dose": "20 mg"},
        ],
    )


def current_intents(world: ScribeWorld) -> list[OutboundIntent]:
    p = world.proposal
    return sorted(
        [
            i
            for i in world.cards()
            if i.source_versions
            and i.source_versions[0].id == p.id
            and i.source_versions[0].version == p.version
        ],
        key=lambda i: i.conversation_sequence,
    )


def test_crop_and_card_are_atomic_ordered_and_unconfirmed(world: ScribeWorld) -> None:
    _, _, media = providers(world, prescription(), prescription(), data=paper())
    world.post(photo())
    p = world.proposal
    assert p.status == "pending" and p.photo and p.photo.reads.instruction_crop
    assert all(not r.frequency and not r.timing for r in p.candidate.orders)
    assert all(term not in "\n".join(render_card(p)) for term in ("قبل الغذاء", "[غير مقروء]"))
    intents = current_intents(world)
    assert [i.template_id for i in intents] == ["scribe_card", "scribe_photo_column"]
    crop = intents[-1]
    scope = IntakeScope(doctor_id=world.doctor.id, intake_id=p.photo.intake_id)
    assert media.get(scope, p.photo.reads.instruction_crop.blob_ref, 8 * 1024 * 1024)
    dispatcher = world.runtime.dispatcher
    # Dispatching the crop first cannot overtake its card.
    row = world.store.get(crop.scope, "outbound_intent", crop.id)
    assert row is not None
    before = len(world.transport.calls)
    dispatcher.dispatch_one(row.scoped_key(crop.scope), "crop-first", world.clock())
    assert len(world.transport.calls) == before
    for intent in intents:
        row = world.store.get(intent.scope, "outbound_intent", intent.id)
        assert row is not None
        dispatcher.dispatch_one(row.scoped_key(intent.scope), "test", world.clock())
    assert world.transport.calls[-1].payload == {"text": COLUMN_CAPTION, "media_kind": "photo"}
    assert world.transport.photos[-1] == media.get(
        scope, p.photo.reads.instruction_crop.blob_ref, 8 * 1024 * 1024
    )
    patient = world.claims.patient(world.doctor.id, p.selected_patient_id or "")
    assert patient and not world.store.list_records(patient.scope, "care_order_head")[0]


@pytest.mark.parametrize("failed", [False, True])
def test_single_reader_or_two_dead_readers_finish_turn(world: ScribeWorld, failed: bool) -> None:
    providers(world, data=paper())
    caller = Readers(["bad", "bad"], ["bad", "bad"] if failed else [document(printed_name=None)])
    world.scribe.vision_factory = lambda source: VisionAdapter(
        caller, source, world.runtime.safety_policy
    )
    world.post(photo())
    assert world.receipt(10).state == "completed"
    if failed:
        assert world.scribe.repo.pending(world.doctor.scope) is None
        assert any(
            i.body.get("payload") == {"text": HANDWRITING_REPLY} for i in failure_intents(world)
        )
    else:
        assert world.scribe.repo.pending(world.doctor.scope) is None
        assert any(
            i.payload == {"text": HANDWRITING_REPLY + "\n" + SINGLE_READER_WARNING}
            for i in world.cards()
        )


@pytest.mark.parametrize("caption", ["أحمد رضا", ""])
@pytest.mark.parametrize("kind", ["prescription", "lab"])
@pytest.mark.parametrize("failed_index", [0, 1])
def test_single_reader_is_private_without_proposals_buttons_or_danger(
    world: ScribeWorld, caption: str, kind: str, failed_index: int
) -> None:
    from sanad.media.vision import DocumentRead
    from sanad.models.io import ModelUnavailable
    from sanad.store.records import IntakeDraft

    _, files, media = providers(world, data=paper())
    reply = document(
        document_type=kind,
        printed_name=None,
        items=[{"name": "InventedMedicine", "dose": "5 mg", "timing": "اليوم"}]
        if kind == "prescription"
        else [{"name": "Potassium", "value": "6.3", "unit": "mmol/L"}],
    )
    scripts: list[list[str | ModelUnavailable]] = [[reply], [reply]]
    scripts[failed_index] = [ModelUnavailable(reason="unavailable")] * 2
    caller = Readers(*scripts)
    world.scribe.vision_factory = lambda source: VisionAdapter(
        caller, source, world.runtime.safety_policy
    )
    world.post(photo(caption))
    assert world.receipt(10).state == "completed"
    assert not world.store.list_records(world.doctor.scope, "scribe_proposal")[0]
    assert not world.store.list_records(world.doctor.scope, "intake_callback")[0]
    draft = from_record(
        world.store.list_records(world.doctor.scope, "intake_draft")[0][0], IntakeDraft
    )
    scope = IntakeScope(doctor_id=world.doctor.id, intake_id=draft.id)
    assert draft.reads.single_reader and not draft.reads.instruction_crop
    assert not world.scribe.photos.danger_facts(draft.reads)
    assert not world.store.list_records(scope, "intake_concern")[0]
    if draft.selected_patient_id:
        patient = world.claims.patient(world.doctor.id, draft.selected_patient_id)
        assert patient is not None
        for entity in ("incident", "clinical_fact", "care_order_head", "care_order_version"):
            assert not world.store.list_records(patient.scope, entity)[0]
    replies = [i for i in world.cards() if i.template_id == "doctor_photo_unreadable"]
    assert len(replies) == 1
    assert replies[0].payload == {
        "text": HANDWRITING_REPLY + "\nقريت الورقة قراءة واحدة بس، مش هسجّل منها حاجة"
    }
    assert all(
        i.template_id != "scribe_card" and i.notification_purpose != "DANGER" for i in world.cards()
    )
    work = from_record(world.store.list_records(scope, "media_work")[0][0], MediaWork)
    assert work.transcript_ref
    saved = DocumentRead.model_validate_json(media.get(scope, work.transcript_ref, 8 * 1024 * 1024))
    assert saved == draft.reads
    # An ACK replay never calls the readers or refetches the image again.
    world.post(photo(caption))
    assert sum(map(len, caller.calls.values())) == 3 and len(files.calls) == 1


@pytest.mark.parametrize("disagreement", [False, True])
def test_cached_unreadable_read_and_intake_reopen_cannot_offer_editable_rows(
    world: ScribeWorld,
    disagreement: bool,
) -> None:
    from sanad.ops.sweep import sweep_due
    from store.account_fixtures import APPLICANT, update

    _, files, _ = providers(world, data=paper())
    caller = Readers(
        [prescription()], [disagreeing_prescription()] if disagreement else ["bad", "bad"]
    )
    world.scribe.vision_factory = lambda source: VisionAdapter(
        caller, source, world.runtime.safety_policy
    )

    def crash(stage: str) -> None:
        if stage == "photo_reads_persisted":
            raise RuntimeError("synthetic crash after private read checkpoint")

    world.scribe.checkpoint = crash
    world.post(photo(""))
    assert world.receipt(10).state != "completed"
    world.scribe.checkpoint = lambda stage: None
    world.clock.advance(timedelta(minutes=11))
    assert not sweep_due(world.runtime, world.store)["errors"]
    assert world.receipt(10).state == "completed"
    world.post(update(APPLICANT, "/intake", 11))
    assert world.receipt(11).state == "completed"
    assert not world.scribe.repo.pending(world.doctor.scope)
    assert sum(map(len, caller.calls.values())) == (2 if disagreement else 3)
    assert len(files.calls) == 1
    assert not world.store.list_records(world.doctor.scope, "intake_callback")[0]
    assert all("reply_markup" not in (i.payload or {}) for i in world.cards())


@pytest.mark.parametrize("disagreement", [False, True])
def test_association_reread_keeps_unreadable_result_privately_and_finishes_with_fallback(
    world: ScribeWorld,
    disagreement: bool,
) -> None:
    from sanad.store._base import Write
    from sanad.store.records import IntakeDraft, record_item
    from store.account_fixtures import APPLICANT, callback

    _, files, _ = providers(world, prescription(), prescription(), data=paper())
    world.post(photo(""))
    raw = intake_button(world, "أحمد رضا")
    row = world.store.list_records(world.doctor.scope, "intake_draft")[0][0]
    stale = world.store._revision(row, world.clock(), reader_policy_version="older-policy")
    assert world.store._atomic([Write(record_item(stale), row.version)], [])
    caller = Readers(
        [prescription()], [disagreeing_prescription()] if disagreement else ["bad", "bad"]
    )
    world.scribe.vision_factory = lambda source: VisionAdapter(
        caller, source, world.runtime.safety_policy
    )
    world.post(callback(raw, APPLICANT, 20))
    assert world.receipt(20).state == "completed"
    assert not world.scribe.repo.pending(world.doctor.scope)
    saved = from_record(
        world.store.list_records(world.doctor.scope, "intake_draft")[0][0], IntakeDraft
    )
    assert saved.reads.single_reader is not disagreement
    assert saved.state == "associated" and saved.proposal_id is None
    assert saved.reader_policy_version == world.runtime.safety_policy.policy_version
    assert any(
        i.payload
        == {
            "text": HANDWRITING_REPLY
            + "\n"
            + (AGREEMENT_WARNING if disagreement else SINGLE_READER_WARNING)
        }
        for i in world.cards()
    )
    assert sum(map(len, caller.calls.values())) == (2 if disagreement else 3)
    assert len(files.calls) == 1


@pytest.mark.parametrize("degradation", ["single_marker", "single_slots", "disagreement"])
def test_legacy_unreadable_proposal_cannot_render_buttons_or_be_confirmed(
    world: ScribeWorld, degradation: str
) -> None:
    from sanad.scribe.proposal import ScribeCallback
    from store.account_fixtures import APPLICANT

    reply = document(document_type="prescription", items=[{"name": "Concor", "dose": "5 mg"}])
    providers(world, reply, reply, data=paper())
    world.post(photo())
    original = world.proposal
    assert original.photo
    # Exercise old persisted candidates whose marker may be missing, and do
    # not rely on the issues computed when that older card was first created.
    reads = original.photo.reads.model_copy(
        update={
            "single_reader": degradation == "single_marker",
            "second": original.photo.reads.second.model_copy(
                update={
                    "status": "failed",
                    "failure_reason": "unavailable",
                    "items": (),
                }
            ),
        }
    )
    warning = SINGLE_READER_WARNING
    if degradation == "disagreement":
        from providers.test_photo_agreement import reading

        reads = reading(("Concor", "Alphamed", "Betatab"), ("Concor", "Quartz", "Winter"))
        warning = AGREEMENT_WARNING
    legacy = original.model_copy(
        update={"photo": original.photo.model_copy(update={"reads": reads}), "issues": ()}
    )
    actor = world.actor(APPLICANT)
    token = world.scribe.repo.load(
        original.scope, "scribe_callback", original.confirmation_nonce_hash, ScribeCallback
    )
    assert token
    assert legacy.blocked("all") and not world.scribe.photos.confirmable(legacy)
    assert world.scribe.buttons(legacy, actor, "unused") == ((), {"inline_keyboard": []})
    assert world.scribe.photos.buttons(legacy, actor) == ((), [])
    assert render_card(legacy) == (HANDWRITING_REPLY + "\n" + warning,)
    intents = world.scribe.photos.card_intents(
        legacy, world.doctor, {"inline_keyboard": [[{"text": "old button"}]]}
    )
    assert len(intents) == 1 and intents[0].template_id == "doctor_photo_unreadable"
    assert intents[0].payload == {"text": HANDWRITING_REPLY + "\n" + warning}
    result = world.scribe.committer.confirm(legacy, token, actor, "legacy-single-reader")
    assert result.status == "clarification" and result.patient_id is None
    patient = world.claims.patient(world.doctor.id, original.selected_patient_id or "")
    assert patient is not None
    assert not world.store.list_records(patient.scope, "care_order_head")[0]


@pytest.mark.parametrize("caption", ["أحمد رضا", ""])
def test_low_agreement_keeps_both_reads_private_and_completes_without_candidates(
    world: ScribeWorld, caption: str
) -> None:
    from sanad.media.agreement import agreed_rows
    from sanad.media.vision import DocumentRead
    from sanad.store.records import IntakeDraft

    caller, files, media = providers(
        world, prescription(), disagreeing_prescription(), data=paper()
    )
    world.post(photo(caption))
    assert world.receipt(10).state == "completed"
    assert not world.store.list_records(world.doctor.scope, "scribe_proposal")[0]
    assert not world.store.list_records(world.doctor.scope, "intake_callback")[0]
    draft = from_record(
        world.store.list_records(world.doctor.scope, "intake_draft")[0][0], IntakeDraft
    )
    assert agreed_rows(draft.reads) == 1 and len(draft.reads.second.items) == 3
    scope = IntakeScope(doctor_id=world.doctor.id, intake_id=draft.id)
    work = from_record(world.store.list_records(scope, "media_work")[0][0], MediaWork)
    assert work.transcript_ref
    saved = DocumentRead.model_validate_json(media.get(scope, work.transcript_ref, 8 * 1024 * 1024))
    assert saved == draft.reads and len(saved.readers) == 2
    assert not world.store.list_records(scope, "intake_concern")[0]
    if draft.selected_patient_id:
        patient = world.claims.patient(world.doctor.id, draft.selected_patient_id)
        assert patient is not None
        for entity in ("incident", "clinical_fact", "care_order_head", "care_order_version"):
            assert not world.store.list_records(patient.scope, entity)[0]
    assert [i.payload for i in world.cards() if i.template_id == "doctor_photo_unreadable"] == [
        {"text": HANDWRITING_REPLY + "\n" + AGREEMENT_WARNING}
    ]
    assert all(i.template_id not in {"scribe_card", "scribe_photo_column"} for i in world.cards())
    world.post(photo(caption))
    assert len(caller.calls) == 2 and len(files.calls) == 1


@pytest.mark.parametrize("corroborated", [False, True])
def test_low_agreement_danger_requires_the_same_critical_row_in_both_reads(
    world: ScribeWorld, corroborated: bool
) -> None:
    from sanad.store.records import IntakeDraft

    first = document(
        items=[
            {"name": "Potassium", "value": "6.3", "unit": "mmol/L"},
            {"name": "Alphamed"},
            {"name": "Betatab"},
        ]
    )
    second = document(
        items=[
            {"name": "Potassium", "value": "6.3" if corroborated else "4.1", "unit": "mmol/L"},
            {"name": "Quartz"},
            {"name": "Winter"},
        ]
    )
    providers(world, first, second)
    world.post(photo())
    assert world.receipt(10).state == "completed" and not world.scribe.repo.pending(
        world.doctor.scope
    )
    draft = from_record(
        world.store.list_records(world.doctor.scope, "intake_draft")[0][0], IntakeDraft
    )
    patient = world.claims.patient(world.doctor.id, draft.selected_patient_id or "")
    assert patient is not None
    assert bool(world.store.list_records(patient.scope, "incident")[0]) is corroborated
    assert not world.store.list_records(patient.scope, "clinical_fact")[0]
    assert any(
        i.payload == {"text": HANDWRITING_REPLY + "\n" + AGREEMENT_WARNING} for i in world.cards()
    )


@pytest.mark.parametrize("as_document", [False, True])
def test_photo_and_heif_wrappers_render_identical_fields(
    world: ScribeWorld, as_document: bool
) -> None:
    reply = document(
        document_type="prescription", printed_name=None, items=[{"name": "Concor", "dose": "5 mg"}]
    )
    vision, _, media = providers(
        world, reply, reply, data=paper("HEIF") if as_document else paper()
    )
    body = photo(as_document=as_document)
    if as_document:
        message = body["message"]
        assert isinstance(message, dict)
        attachment = message["document"]
        assert isinstance(attachment, dict)
        attachment["mime_type"] = "image/heic"
    world.post(body)
    p = world.proposal
    assert (
        p.photo and p.candidate.orders[0].drug == "Concor" and p.candidate.orders[0].dose == "5 mg"
    )
    assert "• Concor 5 mg (بداية)" in "\n".join(render_card(p))
    assert all(c[1][0]["image"]["format"] == "jpeg" for c in vision.calls)
    intake_scope = IntakeScope(doctor_id=world.doctor.id, intake_id=p.photo.intake_id)
    work = from_record(world.store.list_records(intake_scope, "media_work")[0][0], MediaWork)
    assert work.source_blob_ref != work.normalized_blob_ref
    assert vision.calls[0][1][0]["image"]["source"]["bytes"] == media.get(
        intake_scope, work.normalized_blob_ref or "", 8 * 1024 * 1024
    )
    # The expected entire card is independent of its channel wrapper.
    assert render_card(p) == EXPECTED_CARD


# Fixed literal built from the accepted 09b layout, with no time/ID in the comparison.
EXPECTED_CARD = (
    "المريض: أحمد رضا\nقريت في الصورة:\n"
    "للتعديل: صف 1: الاسم=...؛ القيمة=...؛ الوحدة=... (أو الجرعة=... للدوا)\n"
    "الأرقام: 5\n• Concor 5 mg (بداية)\n"
    "تأكيد بداية Concor: الأربعاء 9 سبتمبر، 10 الصبح (افتراضي 3 أيام)؛ "
    "لو متأكدش هبلّغك في نفس الموعد.\n"
    "متابعة اليوم الثالث من تاريخ البداية اللي المريض يبلّغنا بيه؛ التأكيد هنا مش دليل إنه بدأ.\n"
    "صالح لمدة 30 دقيقة",
)


def test_crash_after_normalization_recovers_without_refetch_or_preprocess(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    vision, files, media = providers(world, prescription(), prescription(), data=paper())
    factory = world.scribe.media_factory
    assert factory is not None

    def crash(stage: str) -> None:
        if stage == "committed_extract":
            raise RuntimeError("synthetic crash after normalized checkpoint")

    def failing_factory(*args: Any) -> Any:
        retriever = factory(*args)
        retriever.checkpoint = crash
        return retriever

    world.scribe.media_factory = failing_factory
    world.post(photo())
    assert world.receipt(10).state != "completed"
    assert len(files.calls) == 1 and not vision.calls

    def forbidden(data: bytes) -> bytes:
        raise AssertionError("normalization repeated after persisted checkpoint")

    monkeypatch.setattr("sanad.media.retrieve.normalize_document", forbidden)
    world.scribe.media_factory = factory
    from sanad.ops.sweep import sweep_due

    world.clock.advance(timedelta(minutes=11))
    result = sweep_due(world.runtime, world.store)
    assert not result["errors"]
    assert (
        world.receipt(10).state == "completed" and len(files.calls) == 1 and len(vision.calls) == 2
    )
    assert world.proposal.photo and world.proposal.photo.reads.instruction_crop


def test_invented_name_blocks_affected_row_despite_a_shared_item(world: ScribeWorld) -> None:
    first = document(
        document_type="prescription",
        items=[{"name": "Concor", "dose": "5 mg"}, {"name": "SyntheticReal", "dose": "10 mg"}],
    )
    second = document(
        document_type="prescription",
        items=[{"name": "Concor", "dose": "5 mg"}, {"name": "InventedDrug", "dose": "10 mg"}],
    )
    providers(world, first, second, data=paper())
    world.post(photo())
    assert world.proposal.photo and world.proposal.blocked("order:1")
    assert not world.proposal.blocked("order:0")
    assert not world.proposal.photo.reads.single_reader


def test_nine_megabyte_heif_is_durable_size_failure(world: ScribeWorld) -> None:
    data = paper("HEIF")
    data += b"\0" * (9 * 1024 * 1024 - len(data))
    vision, _, _ = providers(world, data=data)
    world.post(photo(as_document=True))
    assert not vision.calls and world.receipt(10).state == "completed"
    assert any("حجم الملف كبير" in str(i.body.get("payload")) for i in failure_intents(world))


def test_association_reread_persists_a_newly_required_column(world: ScribeWorld) -> None:
    from sanad.store._base import Write
    from sanad.store.records import record_item
    from store.account_fixtures import APPLICANT, callback

    initial = document(document_type="prescription", items=[{"name": "Concor", "dose": "5 mg"}])
    vision, files, media = providers(
        world, initial, initial, prescription(), prescription(), data=paper()
    )
    world.post(photo(""))
    raw = intake_button(world, "أحمد رضا")
    draft = world.store.list_records(world.doctor.scope, "intake_draft")[0][0]
    stale = world.store._revision(draft, world.clock(), reader_policy_version="older-policy")
    assert world.store._atomic([Write(record_item(stale), draft.version)], [])
    world.post(callback(raw, APPLICANT, 20))
    p = world.proposal
    assert len(vision.calls) == 4 and len(files.calls) == 1
    assert p.photo and p.photo.reads.instruction_crop
    assert current_intents(world)[-1].template_id == "scribe_photo_column"
    assert media.get(
        IntakeScope(doctor_id=world.doctor.id, intake_id=p.photo.intake_id),
        p.photo.reads.instruction_crop.blob_ref,
        8 * 1024 * 1024,
    )


def test_null_name_choice_and_empty_manual_name_remain_blocked(world: ScribeWorld) -> None:
    from store.account_fixtures import APPLICANT, update

    shared = {"name": "Potassium", "value": "4.1", "unit": "mmol/L"}
    first = document(items=[shared, {"name": "اليوم", "value": "140", "unit": "mmol/L"}])
    second = document(items=[shared, {"name": "Sodium", "value": "140", "unit": "mmol/L"}])
    providers(world, first, second, data=paper())
    world.post(photo())
    world.tap("قراءة 1: غير مقروء")
    assert world.proposal.blocked("fact:1")
    world.tap("قراءة 2: Sodium", id=21)
    assert not world.proposal.blocked("fact:1")
    world.tap("✏️ تعديل", id=22)
    world.post(update(APPLICANT, "صف 2: الاسم=بدون", 11))
    assert world.proposal.blocked("fact:1")


def test_readability_disagreement_can_be_resolved_on_the_same_card(world: ScribeWorld) -> None:
    items = [{"name": "Potassium", "value": "4.1", "unit": "mmol/L"}]
    providers(world, document(unreadable=True, items=items), document(items=items), data=paper())
    world.post(photo())
    proposal_id = world.proposal.id
    assert world.proposal.blocked("fact:0")
    world.tap("قراءة 2: False")
    assert world.proposal.id == proposal_id and not world.proposal.blocked("fact:0")
    assert world.proposal.status == "pending"


def test_crop_is_suppressed_when_doctor_suspended_during_private_blob_read(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, media = providers(world, prescription(), prescription(), data=paper())
    world.post(photo())
    card, crop = current_intents(world)
    dispatcher = world.runtime.dispatcher
    row = world.store.get(card.scope, "outbound_intent", card.id)
    assert row is not None
    dispatcher.dispatch_one(row.scoped_key(card.scope), "card", world.clock())
    original = media.get

    def amend_while_loading(*args: Any, **kwargs: Any) -> bytes:
        data = original(*args, **kwargs)
        from sanad.accounts.commands import SuspendDoctor

        doctor = world.doctor
        world.runtime.accounts.suspend(
            SuspendDoctor(
                command_id="mid-crop-read",
                actor=world.actor(),
                doctor_id=doctor.id,
                expected_doctor_version=doctor.version,
                reason_code="synthetic",
            )
        )
        return data

    monkeypatch.setattr(media, "get", amend_while_loading)
    row = world.store.get(crop.scope, "outbound_intent", crop.id)
    assert row is not None
    before = len(world.transport.calls)
    result = dispatcher.dispatch_one(row.scoped_key(crop.scope), "crop", world.clock())
    assert result and result.status == "suppressed"
    assert len(world.transport.calls) == before and not world.transport.photos


def failure_intents(world: ScribeWorld) -> tuple[StoredRecord, ...]:
    scope = IntakeScope(doctor_id=world.doctor.id, intake_id=keys.digest(world.receipt(10).id))
    return world.store.list_records(scope, "outbound_intent")[0]
