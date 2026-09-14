"""Recoverable patient evidence extraction; no model call holds a patient lease."""

import asyncio
import re
from datetime import date, datetime
from typing import TYPE_CHECKING

from sanad.concierge.records import PatientAction
from sanad.domain import Mission, PatientScope, Principal, Provenance
from sanad.evidence import associate, templates
from sanad.evidence.classify import classify, document_identity, kind_hint, multiple_documents
from sanad.evidence.grading import grade as grade_row
from sanad.evidence.grading import meaningful_disagreements
from sanad.evidence.screen import read_values, screen_values
from sanad.media.limits import MAX_IMAGE_BYTES, image_info
from sanad.media.telegram import MediaFailure
from sanad.media.vision import DocumentFailure, DocumentRead, read_document
from sanad.scribe.crosscheck import shift_guard
from sanad.steward.service import system_command
from sanad.steward.types import CommandResult, records
from sanad.store import keys
from sanad.store.records import (
    CommandEnvelope,
    Evidence,
    EvidenceHash,
    EvidenceHead,
    InboundReceipt,
    Lease,
    MediaWork,
    Patient,
    from_record,
    to_record,
)

if TYPE_CHECKING:
    from sanad.channels.telegram.router import RouteResult
    from sanad.concierge.turn import ConciergeTurn
    from sanad.media.retrieve import MediaRetriever
    from sanad.scribe.extract import LabRowCandidate


def printed_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    return None


class EvidenceTurn:
    def __init__(self, concierge: "ConciergeTurn"):
        self.concierge = concierge
        self.runtime, self.store = concierge.runtime, concierge.store

    def command(self, scope: PatientScope, id: str, kind: str, **payload: object) -> CommandResult:
        command = system_command(scope, id, {}, self.runtime.clock(), lane="media")
        command = CommandEnvelope.model_validate(
            command.model_dump() | {"payload": {"type": kind, "executor": "evidence-v1", **payload}}
        )
        return self.runtime.steward.handle(command)

    def run(self, receipt: InboundReceipt, actor: Principal) -> str:
        scope = receipt.scope
        if (
            not isinstance(scope, PatientScope)
            or not self.concierge.media_factory
            or not self.concierge.vision_factory
        ):
            return "media_unavailable"
        retriever = self.concierge.media_factory(receipt, actor)
        patient_row = self.store.get(scope, "patient", scope.patient_id)
        assert patient_row
        patient = from_record(patient_row, Patient)
        retriever.failure_template = "patient_evidence_unreadable"
        retriever.failure_text = templates.render("patient_evidence_unreadable", patient.language)
        retriever.failure_renderer = lambda reason: (
            templates.render(
                "patient_" + reason,
                patient.language,
                **({"max_pages": "10"} if reason == "document_too_many_pages" else {}),
            )
            if "patient_" + reason in templates.TEXT
            else retriever.failure_text
        )
        media = retriever.fetch_telegram_file(
            receipt.provider_media_handle or "", receipt_id=receipt.id
        )
        if isinstance(media, MediaFailure):
            return media.reason
        work_row = self.store.get(scope, "media_work", media.work_id)
        assert work_row
        work = from_record(work_row, MediaWork)
        if work.state == "completed":
            return "completed"
        missions = tuple(from_record(r, Mission) for r in records(self.store, scope, "mission"))
        caption = str((receipt.payload or {}).get("text", ""))
        duplicate_row = self.store.get(scope, "evidence_hash", media.byte_hash)
        duplicate: Evidence | None = None
        if duplicate_row:
            marker = from_record(duplicate_row, EvidenceHash)
            first = self.store.get(scope, "evidence", marker.evidence_id + ":1")
            assert first
            duplicate = from_record(first, Evidence)
        cached = None
        if work.transcript_ref:
            try:
                cached = DocumentRead.model_validate_json(
                    retriever.media_store.get(scope, work.transcript_ref, MAX_IMAGE_BYTES)
                )
            except Exception:
                retriever.extraction_result(receipt.id, failure="read_checkpoint_unavailable")
                return "read_checkpoint_unavailable"
        if not cached and not duplicate:
            saved = retriever.media_store.get_read_checkpoint(scope, receipt.id, MAX_IMAGE_BYTES)
            if saved:
                cached = DocumentRead.model_validate_json(saved)
                if (
                    cached.first.provenance.source_observation_id != receipt.id
                    or cached.second.provenance.source_observation_id != receipt.id
                ):
                    return "read_checkpoint_source"
        if cached and media.mime != "application/pdf":
            result: DocumentRead | DocumentFailure = cached
        elif duplicate and media.mime != "application/pdf":
            result = DocumentRead(
                first=duplicate.readers[0],
                second=duplicate.readers[1],
                disagreements=duplicate.disagreements,
            )
        elif media.mime == "application/pdf":
            from sanad.media.documents import DocumentPipeline
            from sanad.media.limits import MediaInvalid

            source = Provenance(
                source_observation_id=receipt.id,
                actor_kind="patient",
                actor_id=actor.subject,
                source_kind="document_observation",
                received_at=receipt.received_at,
            )

            def screen_page(read: DocumentRead, index: int) -> None:
                from sanad.evidence.photos import corroborated_values

                screen_values(
                    self.runtime.urgent,
                    scope,
                    corroborated_values(read.model_copy(update={"blocked_pages": (index,)})),
                    duplicate.observation_id if duplicate else receipt.id,
                    duplicate.provenance.received_at if duplicate else receipt.received_at,
                    self.runtime.safety_policy,
                )
                self.concierge.checkpoint("document_page_danger_persisted")

            try:
                result, manifests = DocumentPipeline(
                    retriever,
                    receipt,
                    source,
                    self.concierge.vision_factory,
                    kind_hint=kind_hint(caption, associate.open_missions(missions)),
                    on_page=screen_page,
                ).run(work)
            except MediaInvalid as error:
                return str(error)
            if isinstance(result, DocumentFailure):
                from sanad.media.documents import failed_document_read

                result = failed_document_read(result.reason, source, manifests)
            reference = retriever.media_store.put(
                scope, result.model_dump_json().encode(), "application/json"
            )
            if not retriever.extraction_result(
                receipt.id, transcript_ref=reference, document_pages=manifests
            ):
                return "stale_work"
            work_row = self.store.get(scope, "media_work", media.work_id)
            assert work_row
            work = from_record(work_row, MediaWork)
        else:
            claim = self.store.claim_work(
                to_record(work, scope).scoped_key(scope),
                work.version,
                "evidence-reader",
                self.runtime.clock(),
                retriever.policy.operations.claim_ttl,
                start_extraction=True,
            )
            if claim is None:
                return "work_busy"
            try:
                image = retriever.media_store.get(scope, media.normalized_blob_ref, MAX_IMAGE_BYTES)
                info = image_info(image)
            except Exception:
                return "storage_unavailable"
            source = Provenance(
                source_observation_id=receipt.id,
                actor_kind="patient",
                actor_id=actor.subject,
                source_kind="document_observation",
                received_at=receipt.received_at,
            )
            from sanad.evidence.context_names import active_drug_names
            from sanad.scribe.memory import NameVocabulary

            doctor = self.runtime.accounts.doctor(scope.doctor_id)
            vocabulary = (
                tuple(NameVocabulary(self.store, doctor).hint().split(", ")) if doctor else ()
            )
            names = (*active_drug_names(self.store, scope, 200), *vocabulary)
            result = asyncio.run(
                read_document(
                    image,
                    info.format,
                    kind_hint=kind_hint(caption, associate.open_missions(missions)),
                    adapter=self.concierge.vision_factory(source),
                    context_names=tuple(dict.fromkeys(names))[:200],
                )
            )
            if isinstance(result, DocumentFailure):
                retriever.extraction_result(receipt.id, failure=result.reason, claim=claim)
                return result.reason
            saved = retriever.media_store.put_read_checkpoint(
                scope, receipt.id, result.model_dump_json().encode()
            )
            self.concierge.checkpoint("evidence_read_blob_written")
            result = DocumentRead.model_validate_json(saved)
            reference = retriever.media_store.put(
                scope, result.model_dump_json().encode(), "application/json"
            )
            if not retriever.extraction_result(
                receipt.id, transcript_ref=reference, claim=claim, resume_immediately=True
            ):
                return "stale_work"
            self.concierge.checkpoint("evidence_read_checkpoint")
        if not work.transcript_ref and (duplicate is not None or cached):
            reference = retriever.media_store.put(
                scope, result.model_dump_json().encode(), "application/json"
            )
            if not retriever.extraction_result(
                receipt.id, transcript_ref=reference, resume_immediately=True
            ):
                return "stale_work"
        assert isinstance(result, DocumentRead)
        ids, hits = screen_values(
            self.runtime.urgent,
            scope,
            self.pdf_values(result) if media.mime == "application/pdf" else read_values(result),
            duplicate.observation_id if duplicate else receipt.id,
            duplicate.provenance.received_at if duplicate else receipt.received_at,
            self.runtime.safety_policy,
        )
        self.concierge.checkpoint("evidence_danger_persisted")
        if duplicate and duplicate.observation_id != receipt.id:
            final_fields = self.pdf_finalization(retriever, work)
            if media.mime == "application/pdf":
                final_fields["duplicate_evidence_id"] = duplicate.evidence_id
            outcome = self.command(
                scope,
                "evidence-duplicate:" + receipt.id,
                "_EvidenceTurn",
                action="duplicate",
                source_receipt_id=receipt.id,
                **final_fields,
            )
            if outcome.status == "accepted" and media.mime != "application/pdf":
                retriever.extraction_result(
                    receipt.id, association_ref="duplicate:" + duplicate.evidence_id
                )
            if outcome.status != "accepted" and media.mime == "application/pdf":
                self.pdf_conflict(retriever, work, final_fields)
            return outcome.status
        id = keys.digest("evidence:" + receipt.id)
        if not self.store.get(scope, "evidence_head", id):
            patient_row = self.store.get(scope, "patient", scope.patient_id)
            assert patient_row
            patient = from_record(patient_row, Patient)
            flags = []
            identity = document_identity(result, patient.display_name)
            if identity != "match":
                flags.append("identity_" + identity)
            if multiple_documents(result, caption):
                flags.append("one_document_per_photo")
            if any(
                re.search(r"ignore|instruction|تجاهل|تجاهَل|بدل.*الرقم", note, re.I)
                for r in (result.first, result.second)
                for note in r.notes
            ):
                flags.append("document_instructions")
            flags.extend(h.reason for h in hits if h.status == "cannot_judge")
            category = classify(result, caption, associate.open_missions(missions))
            now = self.runtime.clock()
            from sanad.media.documents import row_page_indices

            row_pages = row_page_indices(self.store, work, result) if work.document_pages else ()
            candidate = Evidence(
                id=id + ":1",
                evidence_id=id,
                scope=scope,
                created_at=now,
                updated_at=now,
                observation_id=receipt.id,
                media_id=media.work_id,
                content_hash=media.byte_hash,
                category=category,
                printed_identity=result.first.printed_identity_hint.text,
                printed_date=printed_date(result.first.printed_date),
                association_state="candidate",
                extracted_values=tuple(
                    grade_row(r, self.runtime.safety_policy)
                    for r in read_values(
                        DocumentRead(first=result.first, second=result.first, disagreements=())
                    )[: len(result.first.items)]
                )
                if category == "lab_result"
                else tuple(r.item for r in result.first.items),
                readers=(result.first, result.second),
                document_pages=work.document_pages,
                row_pages=row_pages,
                blocked_pages=result.blocked_pages,
                rejection_reason=result.first.failure_reason if work.document_pages else None,
                disagreements=meaningful_disagreements(result.disagreements),
                shift_guard_fired=shift_guard(result.first, result.second),
                provenance=result.first.provenance,
                flags=tuple(dict.fromkeys(flags)),
                incident_ids=ids,
            )
            if row_pages:
                candidate = candidate.model_copy(
                    update={
                        "extracted_values": tuple(
                            value.model_copy(update={"page_indices": row_pages[i]})
                            if hasattr(value, "page_indices") and i < len(row_pages)
                            else value
                            for i, value in enumerate(candidate.extracted_values)
                        )
                    }
                )
            target, match, plausible = associate.choose(missions, candidate, caption)
            candidate = Evidence.model_validate(
                candidate.model_dump()
                | {
                    "mission_id": target.id if target else None,
                    "patient_match_provenance": match,
                    "candidate_mission_ids": tuple(m.id for m in plausible),
                }
            )
            outcome = self.command(
                scope,
                "evidence-record:" + id,
                "_EvidenceTurn",
                action="record",
                candidate=candidate.model_dump(mode="json"),
            )
            if outcome.status != "accepted":
                return outcome.status
            self.concierge.checkpoint("evidence_candidate_persisted")
        head_row = self.store.get(scope, "evidence_head", id)
        assert head_row
        head = from_record(head_row, EvidenceHead)
        # A stored second version is a completed association decision, including a
        # pending patient/doctor choice. Its clock/review survives MediaWork completion.
        if head.current_version == 1:
            final_fields = self.pdf_finalization(retriever, work)
            outcome = self.command(
                scope,
                f"evidence:{id}:2",
                "RecordObjectiveFulfilled",
                action="evaluate",
                evidence_id=id,
                evidence_version=1,
                **final_fields,
            )
            if outcome.status != "accepted":
                if media.mime == "application/pdf":
                    self.pdf_conflict(retriever, work, final_fields)
                return outcome.status
            self.concierge.checkpoint("evidence_acceptance_persisted")
        if media.mime != "application/pdf":
            retriever.extraction_result(receipt.id, association_ref="evidence:" + id)
        return "accepted"

    @staticmethod
    def pdf_values(read: DocumentRead) -> tuple["LabRowCandidate", ...]:
        from sanad.evidence.photos import corroborated_values

        return corroborated_values(read.model_copy(update={"blocked_pages": (1,)}))

    @staticmethod
    def pdf_conflict(
        retriever: "MediaRetriever", work: MediaWork, fields: dict[str, object]
    ) -> None:
        from sanad.store.records import Claim

        claim = (
            Claim.model_validate(fields["document_claim"]) if fields.get("document_claim") else None
        )
        retriever._conflict(work.id, "associate", claim.version if claim else work.version, claim)

    def pdf_finalization(self, retriever: "MediaRetriever", work: MediaWork) -> dict[str, object]:
        if work.mime != "application/pdf":
            return {}
        fresh = self.store.get(work.scope, "media_work", work.id)
        assert fresh
        claim = self.store.claim_work(
            fresh.scoped_key(work.scope),
            fresh.version,
            "pdf-finalize",
            self.runtime.clock(),
            retriever.policy.operations.claim_ttl,
            count_attempt=False,
            start_extraction=True,
        )
        if claim is None:
            return {"document_claim": None}
        return {"document_claim": claim.model_dump(mode="json")}

    def patient_action(
        self, receipt: InboundReceipt, actor: Principal, token: PatientAction
    ) -> "RouteResult":
        from sanad.channels.telegram.router import RouteResult

        assert isinstance(receipt.scope, PatientScope)
        lease = self.store.acquire_patient(
            receipt.scope,
            "evidence-choice:" + receipt.id,
            self.runtime.clock(),
            self.runtime.steward.policy_provider(receipt.scope).operations.lease_ttl,
        )
        if lease is None:
            return RouteResult(route="busy", status="patient_busy")
        try:
            return self._patient_action(receipt, actor, token, lease)
        finally:
            self.store.release_patient(lease)

    def _patient_action(
        self, receipt: InboundReceipt, actor: Principal, token: PatientAction, lease: Lease
    ) -> "RouteResult":
        from sanad.channels.telegram.router import RouteResult

        claimed = self.concierge._claim(receipt)
        if not claimed:
            return RouteResult(route="busy", status="processing")
        receipt, claim = claimed
        action = {
            "evidence_choose": "patient_choose",
            "evidence_yes": "patient_yes",
            "evidence_no": "patient_no",
            "evidence_other": "patient_other",
        }.get(token.action)
        command = CommandEnvelope.model_validate(
            {
                "command_id": "evidence-choice:" + receipt.id,
                "fence": lease,
                "scope": receipt.scope,
                "principal": actor,
                "requested_at": self.runtime.clock(),
                "work_claim": claim,
                "payload": {
                    "type": "_EvidenceTurn",
                    "executor": "evidence-v1",
                    "action": action,
                    "evidence_id": token.evidence_id,
                    "evidence_version": token.evidence_version,
                    "mission_id": token.target_ref.id if token.target_ref else None,
                    "token_hash": token.id,
                },
            }
        )
        outcome = self.runtime.steward.handle(command)
        if outcome.status == "stale_version":
            self.store.defer_inbound(claim, self.runtime.clock())
        elif outcome.status != "accepted":
            stale = CommandEnvelope.model_validate(
                command.model_dump()
                | {
                    "command_id": "evidence-stale:" + receipt.id,
                    # Steward released the first command's lease; acquire a fresh fence.
                    "fence": None,
                    "payload": {
                        "type": "_EvidenceTurn",
                        "executor": "evidence-v1",
                        "action": "patient_stale",
                    },
                }
            )
            self.runtime.steward.handle(stale)
            patient_row = self.store.get(receipt.scope, "patient", actor.patient_id or "")
            assert patient_row
            self.runtime.transport.answer_callback(
                str((receipt.payload or {}).get("callback_query_id", "")),
                templates.render(
                    "patient_evidence_stale",
                    from_record(patient_row, Patient).language,
                ),
            )
        elif outcome.status == "accepted":
            self.runtime.transport.answer_callback(
                str((receipt.payload or {}).get("callback_query_id", "")), ""
            )
        return RouteResult(
            route="patient", status=outcome.status, template_id="patient_evidence_kept"
        )
