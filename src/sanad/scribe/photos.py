"""Recoverable doctor photos, explicit association and human reading choices."""

import asyncio
import json
import re
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel, JsonValue

from sanad.auth.service import read_of, revise
from sanad.auth.tokens import issue_token
from sanad.channels.telegram import wording
from sanad.channels.telegram.router import RouteResult
from sanad.domain import (
    DRAFT_POLICY_2026_09,
    ImageRegion,
    PatientScope,
    Principal,
    Provenance,
    TenantScope,
)
from sanad.media.images import instruction_column
from sanad.media.limits import MediaInvalid, image_info
from sanad.media.retrieve import MediaRetriever, fetch_telegram_file
from sanad.media.telegram import MediaFailure
from sanad.media.vision import (
    VISION_PROMPT_VERSION,
    DocumentCrop,
    DocumentFailure,
    DocumentRead,
    read_document,
)
from sanad.scribe import amend
from sanad.scribe.card import plain
from sanad.scribe.crosscheck import (
    COLUMN_CAPTION,
    HANDWRITING_REPLY,
    PhotoReview,
    candidate_from,
    grade_row,
    lab_text,
    render_card,
    review_issues,
    shift_guard,
    source_text,
    two_readers,
    unreadable_read,
    unreadable_reply,
)
from sanad.scribe.extract import (
    DictationCandidate,
    PatientCandidate,
    ProposalIssue,
    candidate_issues,
)
from sanad.scribe.intake import IntakeService
from sanad.scribe.patients import lookup
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.scribe.proposal import Proposal, ScribeCallback
from sanad.scribe.timing import candidate_timings
from sanad.steward.types import records
from sanad.store import keys
from sanad.store.records import (
    Authorization,
    Claim,
    Doctor,
    IdentityRead,
    InboundReceipt,
    IntakeCallback,
    IntakeConcern,
    IntakeDraft,
    MediaWork,
    OutboundIntent,
    PatientMedia,
    PhotoAssociationWork,
    from_record,
)

if TYPE_CHECKING:
    from sanad.scribe.turn import ScribeTurn

_KIND = re.compile(r"روشتة|روشته|prescription|تحاليل|تحليل|lab|نتيجة", re.I)
_REASONS = {
    "too_large": "حجم الملف كبير",
    "dimensions_exceeded": "أبعاد الصورة كبيرة",
    "unsupported_type": "نوع الملف مش صورة مدعومة",
    "invalid_image": "ملف الصورة تالف",
    "unreadable": "الكتابة مش واضحة",
    "file_expired": "الملف مبقاش متاح",
    "two_documents": "الصورة فيها أكتر من مستند",
    "conversion_failed": "مش قادر أفتح صيغة الصورة دي",
    "expired_handle": "الملف مبقاش متاح",
}


def unreadable(reason: str) -> str:
    if reason in {
        "unreadable",
        "invalid_document_json",
        "template_echo",
        "readers_failed",
        "timeout",
        "unavailable",
        "budget_exhausted",
    }:
        return HANDWRITING_REPLY
    return wording.render(
        "doctor_photo_unreadable", reason=_REASONS.get(reason, "الصورة ماوصلتش أو القراءة ماكملتش")
    )


def caption_patient(caption: str) -> PatientCandidate:
    text = _KIND.sub("", caption).strip(" :،-؛")
    text = re.sub(
        r"^(?:للمريض|للمريضة|المريض|المريضة|patient|for|لـ)\s*[:：]?\s*", "", text, flags=re.I
    )
    return PatientCandidate(name_as_spoken=text or None)


def kind_hint(caption: str) -> str:
    if re.search(r"روشت[ةه]|prescription", caption, re.I):
        return "prescription"
    if re.search(r"تحليل|تحاليل|lab|نتيجة", caption, re.I):
        return "lab"
    return "unknown"


class PhotoTurn:
    def __init__(self, turn: "ScribeTurn"):
        self.turn, self.repo = turn, turn.repo
        self.intake = IntakeService(turn)

    def route(self, receipt: InboundReceipt, auth: Authorization) -> RouteResult | None:
        actor = auth.principal
        if receipt.kind == "callback" and actor.doctor_id:
            token = self.repo.load(
                TenantScope(doctor_id=actor.doctor_id),
                "intake_callback",
                str((receipt.payload or {}).get("callback_token_hash", "")),
                IntakeCallback,
            )
            if token:
                return self.associate(receipt, actor, token)
        if actor.actor_kind == "doctor" and receipt.kind in {"photo", "document"}:
            return self.run(receipt, actor)
        return None

    def run(self, receipt: InboundReceipt, actor: Principal) -> RouteResult:
        try:
            return self._run(receipt, actor)
        finally:
            self.turn._finish_media(receipt, actor)

    def _run(self, receipt: InboundReceipt, actor: Principal) -> RouteResult:
        claimed = self.turn._claim(receipt)
        if claimed is None:
            return RouteResult(route="busy", status="processing")
        receipt, claim = claimed
        doctor = self.turn.claims.doctor(actor)
        if doctor is None:
            return self.turn._reply(receipt, actor, claim, "scribe_stale", "forbidden")
        if self.turn.media_factory is None or self.turn.vision_factory is None:
            return self.turn._reply(
                receipt,
                actor,
                claim,
                "doctor_photo_unreadable",
                "media_unavailable",
                text=unreadable("unavailable"),
            )
        retriever = self.turn.media_factory(receipt, actor)
        retriever.failure_template = "doctor_photo_unreadable"
        retriever.failure_text = unreadable("unavailable")
        retriever.failure_renderer = unreadable
        media = fetch_telegram_file(
            receipt.provider_media_handle or "", retriever=retriever, receipt_id=receipt.id
        )
        if isinstance(media, MediaFailure):
            if media.durable:
                self.turn.runtime.accounts.finish_receipt(receipt, claim, result_code=media.reason)
                return RouteResult(
                    route="doctor", status=media.reason, template_id="doctor_photo_unreadable"
                )
            # A busy claim or temporary storage outage must remain recoverable.
            return RouteResult(route="busy", status=media.reason)
        work = self.repo.load(retriever.scope, "media_work", media.work_id, MediaWork)
        assert work is not None
        cached: DocumentRead | None = None
        if work.transcript_ref:
            try:
                cached = DocumentRead.model_validate_json(
                    retriever.media_store.get(
                        retriever.scope, work.transcript_ref, DRAFT_SCRIBE_POLICY.max_photo_bytes
                    )
                )
            except (ValueError, KeyError):
                cached = None
        source = Provenance(
            source_observation_id=receipt.id,
            actor_kind="doctor",
            actor_id=actor.subject,
            source_kind="document_observation",
            received_at=receipt.received_at,
            source_region=ImageRegion(
                asset_ref=media.normalized_blob_ref,
                x=0,
                y=0,
                width=1,
                height=1,
            ),
        )
        caption = str((receipt.payload or {}).get("text", ""))
        if cached and all(
            r.provenance.prompt_version == VISION_PROMPT_VERSION
            for r in (cached.first, cached.second)
        ):
            result: DocumentRead | DocumentFailure = cached
        else:
            try:
                data = retriever.media_store.get(
                    retriever.scope, media.normalized_blob_ref, DRAFT_SCRIBE_POLICY.max_photo_bytes
                )
                info = image_info(data)
            except MediaInvalid as error:
                result = DocumentFailure(reason=str(error))
            except Exception:
                return RouteResult(route="busy", status="storage_unavailable")
            else:
                result = asyncio.run(
                    read_document(
                        data,
                        info.format,
                        kind_hint=kind_hint(caption),
                        adapter=self.turn.vision_factory(source),
                    )
                )
        if isinstance(result, DocumentFailure):
            failure = retriever.extraction_result(receipt.id, failure=result.reason)
            if isinstance(failure, MediaFailure) and failure.durable:
                self.turn.runtime.accounts.finish_receipt(receipt, claim, result_code=result.reason)
                return RouteResult(
                    route="doctor", status=result.reason, template_id="doctor_photo_unreadable"
                )
            return RouteResult(route="busy", status="stale_work")
        if cached is not result:
            result = self.with_column(result, retriever, media.normalized_blob_ref)
            reference = retriever.media_store.put(
                retriever.scope, result.model_dump_json().encode(), "application/json"
            )
            if not retriever.extraction_result(receipt.id, transcript_ref=reference):
                return RouteResult(route="busy", status="stale_work")
        self.turn.checkpoint("photo_reads_persisted")
        kind = result.first.document_type
        draft = self.intake.create(actor, receipt.id, result, kind)
        patient = caption_patient(caption)
        previous = self.repo.pending(doctor.scope)
        pending_id = (
            previous.selected_patient_id
            if previous and previous.status == "pending" and previous.expires_at > self.repo.clock()
            else None
        )
        choices = lookup(self.repo.store, doctor.scope, patient) if patient.name_as_spoken else ()
        selected = (
            choices[0].patient_id
            if len(choices) == 1
            else pending_id
            if not patient.name_as_spoken
            else None
        )
        danger = self.danger_facts(result)
        if danger and selected:
            self.raise_patient(result, selected, actor)
        elif danger:
            draft = self.intake.concern(draft, actor, danger)
        if unreadable_read(result):
            routed = self.unreadable_card(receipt, actor, claim, doctor, draft, selected)
        elif not patient.name_as_spoken and not selected:
            routed = self.pending(receipt, actor, claim, doctor, draft)
        else:
            routed = self.propose(receipt, actor, claim, doctor, draft, patient, selected)
        return routed.model_copy(
            update={
                "delivery_patient": PatientScope(doctor_id=doctor.id, patient_id=selected)
                if selected
                else None
            }
        )

    @staticmethod
    def with_column(
        reads: DocumentRead, retriever: MediaRetriever, normalized_blob_ref: str
    ) -> DocumentRead:
        if (
            not two_readers(reads)
            or not any(r.document_type == "prescription" for r in reads.readers)
            or not any(p.startswith("items.") for r in reads.readers for p in r.dropped_fields)
        ):
            return reads
        data = retriever.media_store.get(
            retriever.scope, normalized_blob_ref, DRAFT_SCRIBE_POLICY.max_photo_bytes
        )
        crop = instruction_column(data)
        crop_ref = retriever.media_store.put(retriever.scope, crop.data, "image/jpeg")
        return reads.model_copy(
            update={
                "instruction_crop": DocumentCrop(
                    blob_ref=crop_ref,
                    box=crop.box,
                    layout=crop.layout,
                    normalized_blob_ref=normalized_blob_ref,
                )
            }
        )

    def danger_facts(self, reads: DocumentRead) -> dict[str, JsonValue]:
        if not two_readers(reads):
            return {}
        rows: list[JsonValue] = []
        unreliable = unreadable_read(reads)
        # For H fallback, only a jointly readable critical row can raise danger.
        # On an editable document either reader still adds danger as in 09b.
        for reader in reads.readers:
            for row in reader.items:
                if unreliable and not all(
                    any(
                        (other.item.name or "").strip().casefold()
                        == (row.item.name or "").strip().casefold()
                        and other.item.value == row.item.value
                        and other.item.unit == row.item.unit
                        and row.item.value is not None
                        and row.item.unit
                        and row.item.name
                        and "[unreadable]" not in row.item.name
                        for other in reading.items
                    )
                    for reading in reads.readers
                ):
                    continue
                if row.lab_verdict and row.lab_verdict.level == "critical":
                    rows.append(
                        {
                            "item": row.item.model_dump(mode="json"),
                            "verdict": row.lab_verdict.model_dump(mode="json"),
                            "model_id": reader.provenance.model_id,
                        }
                    )
        return (
            {"rows": rows, "source_receipt_id": reads.first.provenance.source_observation_id}
            if rows
            else {}
        )

    def unreadable_card(
        self,
        receipt: InboundReceipt,
        actor: Principal,
        claim: Claim,
        doctor: Doctor,
        draft: IntakeDraft,
        selected: str | None,
    ) -> RouteResult:
        now = self.repo.clock()
        lease = None
        models: tuple[BaseModel, ...] = (revise(draft, now),)
        if selected:
            from datetime import timedelta

            scope = PatientScope(doctor_id=doctor.id, patient_id=selected)
            lease = self.repo.store.acquire_patient(
                scope, "photo-unreadable", now, timedelta(minutes=5)
            )
            if lease is None:
                return RouteResult(route="busy", status="patient_busy")
            models = (
                revise(
                    draft, now, selected_patient_id=selected, state="associated", work_clock=None
                ),
            )
            for media_id in draft.media_work_ids:
                media_scope = keys.IntakeScope(doctor_id=doctor.id, intake_id=draft.id)
                work = self.repo.load(media_scope, "media_work", media_id, MediaWork)
                assert work is not None and work.mime
                models += (
                    PatientMedia(
                        id=media_id,
                        media_work_id=media_id,
                        media_scope=media_scope,
                        source_receipt_id=work.receipt_id,
                        kind=draft.kind,
                        mime=work.mime,
                        scope=scope,
                        created_at=now,
                        updated_at=now,
                    ),
                )
        try:
            result = self.repo.commit(
                actor,
                "PhotoUnreadable",
                "photo-unreadable:" + receipt.id,
                models,
                (
                    self.repo.intent(
                        doctor,
                        "doctor_photo_unreadable",
                        {"text": unreadable_reply(draft.reads)},
                        receipt.id,
                    ),
                ),
                claim=claim,
                fence=lease,
                reads=(read_of(draft),),
            )
            return RouteResult(
                route="doctor", status=result.status, template_id="doctor_photo_unreadable"
            )
        finally:
            if lease:
                self.repo.store.release_patient(lease)

    def raise_patient(self, reads: DocumentRead, patient_id: str, actor: Principal) -> bool:
        facts = self.danger_facts(reads)
        if not facts:
            return False
        if (
            self.turn.claims.doctor(actor) is None
            or self.turn.claims.patient(actor.doctor_id or "", patient_id) is None
        ):
            raise ValueError("photo_patient_authority")
        scope = PatientScope(doctor_id=actor.doctor_id or "", patient_id=patient_id)
        from sanad.store.keys import IntakeScope

        intake_id = keys.digest(reads.first.provenance.source_observation_id)
        concern = self.repo.load(
            IntakeScope(doctor_id=scope.doctor_id, intake_id=intake_id),
            "intake_concern",
            keys.digest("intake-lab:" + intake_id),
            IntakeConcern,
        )
        self.raise_rows(facts, scope, concern.prior_delivery_refs if concern else ())
        self.turn.checkpoint("photo_danger_persisted")
        return True

    def raise_rows(
        self, facts: dict[str, JsonValue], scope: PatientScope, prior: tuple[str, ...] = ()
    ) -> None:
        from sanad.domain import ObservationRef
        from sanad.safety import to_incident_facts
        from sanad.safety.models import LabVerdict

        rows = facts.get("rows")
        assert isinstance(rows, list)
        for row in rows:
            assert isinstance(row, dict) and isinstance(row["item"], dict)
            verdict = LabVerdict.model_validate(row["verdict"])
            if verdict.level != "critical":
                continue
            value, severity = to_incident_facts(
                verdict,
                source=ObservationRef(observation_id=str(facts["source_receipt_id"])),
                policy=self.turn.runtime.safety_policy,
            )
            key = (
                value.unique_source_key
                + ":"
                + keys.digest(
                    json.dumps(
                        {k: row["item"].get(k) for k in ("name", "value", "unit")}, sort_keys=True
                    )
                )
            )
            self.turn.runtime.urgent.raise_incident(
                scope,
                key,
                value.as_payload(),
                severity,
                self.repo.clock(),
                prior_delivery_refs=prior,
            )

    def screen_correction(
        self,
        candidate: DictationCandidate,
        photo: PhotoReview,
        receipt: InboundReceipt,
        actor: Principal,
        patient_id: str | None,
    ) -> PhotoReview:
        rows: list[JsonValue] = []
        for fact in candidate.facts:
            if fact.lab and fact.lab.verdict and fact.lab.verdict.level == "critical":
                rows.append(
                    {
                        "item": {
                            "name": fact.lab.analyte,
                            "value": fact.lab.value,
                            "unit": fact.lab.unit,
                        },
                        "verdict": fact.lab.verdict.model_dump(mode="json"),
                        "model_id": "doctor_correction",
                    }
                )
        if not rows:
            return photo
        facts: dict[str, JsonValue] = {"rows": rows, "source_receipt_id": receipt.id}
        if patient_id:
            if (
                self.turn.claims.doctor(actor) is None
                or self.turn.claims.patient(actor.doctor_id or "", patient_id) is None
            ):
                raise ValueError("photo_patient_authority")
            self.raise_rows(
                facts, PatientScope(doctor_id=actor.doctor_id or "", patient_id=patient_id)
            )
        else:
            draft = self.repo.load(
                TenantScope(doctor_id=actor.doctor_id or ""),
                "intake_draft",
                photo.intake_id,
                IntakeDraft,
            )
            assert draft is not None
            self.intake.concern(draft, actor, facts, source_key=receipt.id)
        self.turn.checkpoint("photo_danger_persisted")
        return photo.model_copy(update={"danger_raised": True})

    def finish_association(self, work: PhotoAssociationWork) -> None:
        if work.state != "pending":
            return
        doctor_row = self.repo.store.get(work.scope, "doctor", work.scope.doctor_id)
        draft = self.repo.load(work.scope, "intake_draft", work.intake_id, IntakeDraft)
        if doctor_row is None or draft is None:
            return
        doctor = from_record(doctor_row, Doctor)
        actor = self.repo.store.authorize(doctor.telegram_bot_id, doctor.telegram_user_id).principal
        if self.turn.claims.doctor(actor) is None:
            return
        self.raise_patient(draft.reads, work.patient_id, actor)
        from sanad.store.keys import IntakeScope

        for row in records(
            self.repo.store, IntakeScope(doctor_id=doctor.id, intake_id=draft.id), "intake_concern"
        ):
            concern = from_record(row, IntakeConcern)
            if concern.state == "open":
                self.raise_rows(
                    concern.facts,
                    PatientScope(doctor_id=doctor.id, patient_id=work.patient_id),
                    concern.prior_delivery_refs,
                )
        now = self.repo.clock()
        models: tuple[BaseModel, ...] = (
            revise(work, now, state="completed", work_clock=None),
            revise(draft, now, selected_patient_id=work.patient_id),
            *self.intake.associated_concern(draft, work.patient_id),
        )
        self.repo.commit(
            actor,
            "PhotoAssociate",
            "photo-associated:" + work.id,
            models,
            reads=(read_of(work), read_of(draft)),
        )

    def pending(
        self,
        receipt: InboundReceipt,
        actor: Principal,
        claim: Claim,
        doctor: Doctor,
        draft: IntakeDraft,
    ) -> RouteResult:
        if unreadable_read(draft.reads):
            return self.unreadable_card(receipt, actor, claim, doctor, draft, None)
        now = self.repo.clock()
        changed = revise(draft, now)
        tokens, markup = self.intake.buttons(changed, doctor, actor)
        body = wording.render("scribe_intake_pending")
        if draft.safety_epoch:
            body += "\n⚠️ تم تنبيهك"
        models: tuple[BaseModel, ...] = (changed, *tokens)
        previous, state = self.repo.pending(doctor.scope), self.repo.state(doctor.scope)
        if previous and previous.status == "pending":
            models += (revise(previous, now, status="superseded", work_clock=None),)
        if state and state.pending_proposal_id:
            models += (revise(state, now, pending_proposal_id=None),)
        intent = self.repo.intent(
            doctor,
            "scribe_intake_pending",
            {"text": body, "reply_markup": markup},
            "intake-pending:" + receipt.id,
        )
        result = self.repo.commit(
            actor,
            "IntakeAction",
            "intake-pending:" + receipt.id,
            models,
            (intent,),
            claim=claim,
            reads=(read_of(draft),),
        )
        return RouteResult(
            route="doctor", status=result.status, template_id="scribe_intake_pending"
        )

    def propose(
        self,
        receipt: InboundReceipt,
        actor: Principal,
        claim: Claim,
        doctor: Doctor,
        draft: IntakeDraft,
        patient: PatientCandidate,
        selected: str | None,
        *,
        token: IntakeCallback | None = None,
        new: bool = False,
        intake_fence: Claim | None = None,
    ) -> RouteResult:
        if unreadable_read(draft.reads):
            return self.unreadable_card(receipt, actor, claim, doctor, draft, selected)
        candidate = candidate_from(
            draft.reads, draft.kind, self.turn.runtime.safety_policy
        ).model_copy(update={"patient": patient})
        photo = PhotoReview(
            reads=draft.reads,
            kind=draft.kind,
            intake_id=draft.id,
            media_work_ids=draft.media_work_ids,
            shift_detected=DRAFT_SCRIBE_POLICY.shift_guard_enabled
            and shift_guard(draft.reads.first, draft.reads.second),
            danger_raised=bool(self.danger_facts(draft.reads)),
        )
        now, proposal_id = self.repo.clock(), uuid4().hex
        models: tuple[BaseModel, ...] = (
            revise(
                draft,
                now,
                state="associated",
                proposal_id=proposal_id,
                selected_patient_id=selected,
                work_clock=None,
                processing_claim=None,
            ),
        )
        reads: tuple[IdentityRead, ...] = (read_of(draft),)
        if token:
            models += (revise(token, now, consumed_at=now),)
            reads += (read_of(token),)
        if selected:
            self.raise_patient(draft.reads, selected, actor)
            models += self.intake.associated_concern(draft, selected)
        return self.turn._propose(
            receipt,
            actor,
            claim,
            doctor,
            candidate,
            source_text(draft.reads) + "\n" + (patient.name_as_spoken or ""),
            self.repo.state(doctor.scope),
            self.repo.pending(doctor.scope),
            (draft.reads.first.provenance, draft.reads.second.provenance),
            (),
            (),
            None,
            new,
            False,
            "/new" if new else None,
            photo=photo,
            selected_id=selected,
            extra_models=models,
            extra_reads=reads,
            proposal_id=proposal_id,
            intake_fence=intake_fence,
        )

    def associate(
        self, receipt: InboundReceipt, actor: Principal, token: IntakeCallback
    ) -> RouteResult:
        claimed = self.turn._claim(receipt)
        if claimed is None:
            return RouteResult(route="busy", status="processing")
        receipt, claim = claimed
        doctor = self.turn.claims.doctor(actor)
        draft = self.repo.load(token.scope, "intake_draft", token.intake_id, IntakeDraft)
        now = self.repo.clock()
        if (
            doctor is None
            or draft is None
            or draft.state != "pending"
            or actor.subject != token.actor_subject
            or actor.doctor_id != draft.owner_doctor_id
            or token.consumed_at
            or token.expires_at <= now
            or token.intake_version > draft.version
        ):
            return self.turn._reply(receipt, actor, claim, "scribe_stale", "stale_version")
        if token.action == "later":
            later_result = self.repo.commit(
                actor,
                "IntakeAction",
                "intake-later:" + receipt.id,
                (revise(draft, now), revise(token, now, consumed_at=now)),
                claim=claim,
                payload={"nonce_hash": token.id},
                reads=(read_of(draft), read_of(token)),
            )
            return RouteResult(
                route="callback", status=later_result.status, template_id="scribe_intake_pending"
            )
        intake_fence = self.repo.store.acquire_intake(
            doctor.scope,
            draft.id,
            draft.version,
            actor.subject,
            now,
            self.turn.runtime.accounts.policy.operations.claim_ttl,
        )
        if intake_fence is None:
            return self.turn._reply(receipt, actor, claim, "scribe_stale", "intake_busy")
        current_draft = self.repo.load(doctor.scope, "intake_draft", draft.id, IntakeDraft)
        assert current_draft is not None
        draft = current_draft
        if draft.reader_policy_version != self.turn.runtime.safety_policy.policy_version or any(
            r.provenance.prompt_version != VISION_PROMPT_VERSION
            for r in (draft.reads.first, draft.reads.second)
        ):
            from sanad.store.keys import IntakeScope

            media_scope = IntakeScope(doctor_id=doctor.id, intake_id=draft.id)
            work = self.repo.load(media_scope, "media_work", draft.media_work_ids[0], MediaWork)
            source_row = self.repo.store.get(
                doctor.scope, "inbound_receipt", draft.source_receipt_ids[0]
            )
            if (
                work is None
                or source_row is None
                or not work.normalized_blob_ref
                or not self.turn.media_factory
                or not self.turn.vision_factory
            ):
                return self.turn._reply(
                    receipt,
                    actor,
                    claim,
                    "doctor_photo_unreadable",
                    "reread_required",
                    text=unreadable("unavailable"),
                )
            source_receipt = from_record(source_row, InboundReceipt)
            retriever = self.turn.media_factory(source_receipt, actor)
            data = retriever.media_store.get(
                media_scope, work.normalized_blob_ref, DRAFT_SCRIBE_POLICY.max_photo_bytes
            )
            info = image_info(data)
            fresh = asyncio.run(
                read_document(
                    data,
                    info.format,
                    kind_hint=draft.kind,
                    adapter=self.turn.vision_factory(draft.reads.first.provenance),
                )
            )
            if isinstance(fresh, DocumentFailure):
                return self.turn._reply(
                    receipt,
                    actor,
                    claim,
                    "doctor_photo_unreadable",
                    "reread_required",
                    text=unreadable("unreadable"),
                )
            if unreadable_read(fresh):
                # Keep the surviving reread in the doctor-private draft before
                # rendering fallback; a callback retry must not resurrect old reads.
                revised = revise(
                    draft,
                    self.repo.clock(),
                    reads=fresh,
                    kind=fresh.first.document_type,
                    reader_policy_version=self.turn.runtime.safety_policy.policy_version,
                    processing_claim=None,
                )
                saved = self.repo.commit(
                    actor,
                    "IntakeAction",
                    "photo-reread:" + receipt.id,
                    (revised,),
                    reads=(read_of(draft),),
                )
                if saved.status not in {"accepted", "duplicate"}:
                    return RouteResult(route="busy", status="stale_work")
                draft = revised
            else:
                draft = draft.model_copy(
                    update={
                        "reads": self.with_column(fresh, retriever, work.normalized_blob_ref),
                        "reader_policy_version": self.turn.runtime.safety_policy.policy_version,
                    }
                )
        selected = (
            self.turn.claims.patient(doctor.id, token.patient_id or "")
            if token.action == "select"
            else None
        )
        if token.action == "select" and selected is None:
            return self.turn._reply(receipt, actor, claim, "scribe_stale", "stale_version")
        result = self.propose(
            receipt,
            actor,
            claim,
            doctor,
            draft,
            PatientCandidate(),
            selected.id if selected else None,
            token=token,
            new=token.action == "new",
            intake_fence=intake_fence,
        )
        self.turn.runtime.transport.answer_callback(
            str((receipt.payload or {}).get("callback_query_id", "")), ""
        )
        return result

    @staticmethod
    def confirmable(proposal: Proposal) -> bool:
        if not proposal.photo:
            return True
        return any(
            not proposal.blocked(f"{family}:{i}")
            for family, values in (
                ("order", proposal.candidate.orders),
                ("fact", proposal.candidate.facts),
            )
            for i, _ in enumerate(values)
        )

    @staticmethod
    def edits(text: str) -> tuple[int, dict[str, str | None]] | None:
        match = re.fullmatch(r"(?:صف|row)\s+(\d+)\s*[:：]\s*(.+)", text, flags=re.I | re.S)
        if not match:
            return None
        mapping = {
            "الاسم": "name",
            "القيمة": "value",
            "الوحدة": "unit",
            "الجرعة": "dose",
            "التكرار": "frequency",
            "الطريق": "route",
            "التوقيت": "timing",
            "المدة": "duration",
            "العلامة": "flag",
            "الإجراء": "action",
        }
        fields: dict[str, str | None] = {}
        for entry in re.split("[;؛]", match[2]):
            key, separator, value = entry.partition("=")
            if not separator:
                return None
            key, value = mapping.get(key.strip(), key.strip()), value.strip()
            if key not in {
                "name",
                "value",
                "unit",
                "dose",
                "frequency",
                "route",
                "timing",
                "duration",
                "flag",
                "action",
            }:
                return None
            fields[key] = None if value in {"", "null", "بدون"} else value
        return int(match[1]) - 1, fields

    def manual_edit(self, proposal: Proposal, text: str) -> DictationCandidate | None:
        edit = self.edits(text)
        if edit is None or proposal.photo is None:
            return None
        index, fields = edit
        candidate = proposal.candidate
        for field, value in fields.items():
            candidate = self.set_field(candidate, proposal.photo, index, field, value)
        return candidate

    def corrected_review(
        self, proposal: Proposal, candidate: DictationCandidate, text: str
    ) -> PhotoReview:
        photo = proposal.photo
        assert photo is not None
        resolved, edited = set(photo.resolved_fields), set(photo.edited_rows)
        explicit = self.edits(text)
        if explicit:
            index, fields = explicit
            count = len(candidate.orders) if photo.kind == "prescription" else len(candidate.facts)
            if 0 <= index < count:
                resolved.update(f"items.{index}.{f}" for f in fields)
                if {"name", "dose" if photo.kind == "prescription" else "value"} <= fields.keys():
                    edited.add(index)
        else:
            before, after = (
                (proposal.candidate.orders, candidate.orders)
                if photo.kind == "prescription"
                else (proposal.candidate.facts, candidate.facts)
            )
            for i, (old, new) in enumerate(zip(before, after, strict=False)):
                old_fields = old.model_dump()
                new_fields = new.model_dump()
                if photo.kind == "lab":
                    old_fields, new_fields = (
                        old_fields.get("lab") or {},
                        new_fields.get("lab") or {},
                    )
                mapping = {"drug": "name", "analyte": "name"}
                for field, value in new_fields.items():
                    if (
                        value != old_fields.get(field)
                        and isinstance(value, str)
                        and plain(value) in plain(text)
                    ):
                        resolved.add(f"items.{i}.{mapping.get(field, field)}")
                if (
                    f"items.{i}.name" in resolved
                    and f"items.{i}.{'dose' if photo.kind == 'prescription' else 'value'}"
                    in resolved
                ):
                    edited.add(i)
        return photo.model_copy(
            update={
                "resolved_fields": tuple(sorted(resolved)),
                "edited_rows": tuple(sorted(edited)),
            }
        )

    def buttons(
        self, proposal: Proposal, actor: Principal
    ) -> tuple[tuple[ScribeCallback, ...], list[JsonValue]]:
        if not proposal.photo or unreadable_read(proposal.photo.reads):
            return (), []
        tokens: list[ScribeCallback] = []
        buttons: list[JsonValue] = []
        for d in proposal.photo.reads.disagreements:
            if d.field in proposal.photo.resolved_fields or d.field == "printed_identity_hint":
                continue
            # One disputed field at a time keeps Telegram and the atomic batch bounded.
            for reading, value in enumerate((d.first, d.second)):
                token, now = issue_token(), self.repo.clock()
                tokens.append(
                    ScribeCallback.model_validate(
                        {
                            "id": token.hash,
                            "scope": proposal.scope,
                            "proposal_id": proposal.id,
                            "proposal_version": proposal.version,
                            "actor_subject": actor.subject,
                            "action": "reading",
                            "field": d.field,
                            "reading": reading,
                            "expires_at": proposal.expires_at,
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                )
                buttons.append(
                    [
                        {
                            "text": f"قراءة {reading + 1}: " + plain(value or "غير مقروء")[:60],
                            "callback_data": token.secret.get_secret_value(),
                        }
                    ]
                )
            break
        return tuple(tokens), buttons

    def reading(
        self,
        receipt: InboundReceipt,
        actor: Principal,
        claim: Claim,
        proposal: Proposal,
        token: ScribeCallback,
    ) -> RouteResult:
        photo = proposal.photo
        if photo is None or token.field is None or token.reading is None:
            return self.turn._reply(receipt, actor, claim, "scribe_stale", "stale_version")
        d = next((d for d in photo.reads.disagreements if d.field == token.field), None)
        if d is None:
            return self.turn._reply(receipt, actor, claim, "scribe_stale", "stale_version")
        candidate = proposal.candidate
        if token.field.startswith("items."):
            _, index, field = token.field.split(".")
            selected_value = d.first if token.reading == 0 else d.second
            # An empty reading is not a doctor's replacement for a missing name.
            # Keep its row blocked until a real name is chosen or explicitly edited.
            if field == "name" and not selected_value:
                return self.refresh(receipt, actor, claim, proposal, token, candidate, photo)
            candidate = self.set_field(candidate, photo, int(index), field, selected_value)
        elif token.field == "document_type":
            chosen = photo.reads.first if token.reading == 0 else photo.reads.second
            kind = chosen.document_type
            if kind in {"prescription", "lab", "other"}:
                photo = PhotoReview.model_validate(photo.model_dump() | {"kind": kind})
                candidate = candidate_from(
                    photo.reads, kind, self.turn.runtime.safety_policy
                ).model_copy(update={"patient": candidate.patient})
        photo = photo.model_copy(update={"resolved_fields": (*photo.resolved_fields, token.field)})
        return self.refresh(receipt, actor, claim, proposal, token, candidate, photo)

    def set_field(
        self,
        candidate: DictationCandidate,
        photo: PhotoReview,
        index: int,
        field: str,
        value: str | None,
    ) -> DictationCandidate:
        if photo.kind == "prescription":
            orders = list(candidate.orders)
            mapping = {
                "name": "drug",
                "dose": "dose",
                "unit": "dose",
                "frequency": "frequency",
                "route": "route",
                "timing": "timing",
                "action": "action",
                "duration": "duration",
            }
            if 0 <= index < len(orders) and field in mapping:
                if field == "dose" and value and re.fullmatch(r"[0-9.]+", value):
                    unit = re.sub(r"^[0-9.]+\s*", "", orders[index].dose or "")
                    value = value + (" " + unit if unit else "")
                if field == "unit":
                    dose = re.sub(r"[^0-9.]+.*$", "", orders[index].dose or "")
                    value = " ".join(v for v in (dose, value) if v) or None
                orders[index] = type(orders[index]).model_validate(
                    orders[index].model_dump()
                    | {mapping[field]: value or ("" if field == "name" else None)}
                )
            return candidate.model_copy(update={"orders": tuple(orders)})
        facts = list(candidate.facts)
        if 0 <= index < len(facts) and facts[index].lab:
            row = facts[index].lab
            assert row is not None
            mapping = {"name": "analyte", "value": "value", "unit": "unit", "flag": "flag"}
            if field in mapping:
                row = grade_row(
                    row.model_copy(
                        update={mapping[field]: value or ("" if field == "name" else None)}
                    ),
                    self.turn.runtime.safety_policy,
                )
                facts[index] = facts[index].model_copy(update={"lab": row, "text": lab_text(row)})
        return candidate.model_copy(update={"facts": tuple(facts)})

    def refresh(
        self,
        receipt: InboundReceipt,
        actor: Principal,
        claim: Claim,
        proposal: Proposal,
        token: ScribeCallback,
        candidate: DictationCandidate,
        photo: PhotoReview,
    ) -> RouteResult:
        doctor = self.turn.claims.doctor(actor)
        assert doctor is not None
        target = (
            PatientScope(doctor_id=doctor.id, patient_id=proposal.selected_patient_id)
            if proposal.selected_patient_id
            else None
        )
        candidate, amendments, amendment_issues = amend.prepare(
            self.repo, target, candidate, creating=proposal.creating_patient
        )
        support = "\n".join(amend.instruction_line(a.old) for a in amendments if a.old)
        issues = (
            *candidate_issues(candidate, proposal.source_text + "\n" + support),
            *review_issues(photo, candidate),
            *amendment_issues,
        )
        if not proposal.selected_patient_id and not (
            proposal.creating_patient and candidate.patient.name_as_spoken
        ):
            issues += (ProposalIssue(item="patient", code="patient_missing"),)
        timings, timing_issues = candidate_timings(
            candidate,
            proposal.created_at,
            DRAFT_POLICY_2026_09.model_copy(update={"timezone": doctor.timezone}),
        )
        now, nonce = self.repo.clock(), issue_token()
        changed = revise(
            proposal,
            now,
            candidate=candidate,
            photo=photo,
            amendments=amendments,
            issues=tuple(dict.fromkeys((*issues, *timing_issues))),
            timings=timings,
            confirmation_nonce_hash=nonce.hash,
        )
        tokens, markup = self.turn.buttons(changed, actor, nonce.secret.get_secret_value())
        intents = self.card_intents(changed, doctor, markup)
        result = self.repo.commit(
            actor,
            "ScribeAction",
            "photo-choice:" + receipt.id,
            (changed, revise(token, now, consumed_at=now), *tokens),
            intents,
            claim=claim,
            payload={"proposal_id": proposal.id, "nonce_hash": token.id},
        )
        self.turn.runtime.transport.answer_callback(
            str((receipt.payload or {}).get("callback_query_id", "")), ""
        )
        return RouteResult(route="callback", status=result.status, template_id="scribe_card")

    def card_intents(
        self, proposal: Proposal, doctor: Doctor, markup: JsonValue
    ) -> tuple[OutboundIntent, ...]:
        if proposal.photo and unreadable_read(proposal.photo.reads):
            return (
                self.repo.intent(
                    doctor,
                    "doctor_photo_unreadable",
                    {"text": unreadable_reply(proposal.photo.reads)},
                    f"{proposal.id}:{proposal.version}",
                    proposal=proposal,
                ),
            )
        cards = render_card(proposal)
        intents = tuple(
            self.repo.intent(
                doctor,
                "scribe_card",
                {"text": card, **({"reply_markup": markup} if i == len(cards) - 1 else {})},
                f"{proposal.id}:{proposal.version}",
                proposal=proposal,
                sequence=i,
            )
            for i, card in enumerate(cards)
        )
        crop = proposal.photo.reads.instruction_crop if proposal.photo else None
        if crop:
            intents += (
                self.repo.intent(
                    doctor,
                    "scribe_photo_column",
                    {"text": COLUMN_CAPTION, "photo_blob_ref": crop.blob_ref},
                    f"{proposal.id}:{proposal.version}",
                    proposal=proposal,
                    sequence=len(cards),
                ),
            )
        return intents
