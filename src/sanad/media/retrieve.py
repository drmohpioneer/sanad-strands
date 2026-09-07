"""Claimed fetch/normalize checkpoints, with atomic timed failure and resend intent.

Nothing is wired into inbound turns in 08. Later extractors consume the pending
extract stage and own explicit association; a fetched blob is never evidence acceptance.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from sanad.domain import (
    CreateReview,
    PatientScope,
    Principal,
    ReviewKind,
    TenantScope,
    create_review,
)
from sanad.domain.boundaries import _BoundaryValue
from sanad.domain.operations import transition_operational_clock
from sanad.media.audio import AudioConverter, ConversionFailure
from sanad.media.limits import MAX_AUDIO_BYTES, MediaInvalid, image_info, sniff
from sanad.media.storage import MediaScope, MediaStore
from sanad.media.telegram import MediaFailure, TelegramFiles
from sanad.steward.apply import make_intent
from sanad.steward.service import Steward
from sanad.steward.types import StewardPolicy
from sanad.store import keys
from sanad.store.records import (
    AuditEvent,
    Claim,
    CommandEnvelope,
    CommitRequest,
    DoctorAuthority,
    InboundReceipt,
    MediaWork,
    OperationalClock,
    OutboundIntent,
    WorkerCapability,
    from_record,
    to_record,
)

RESEND_TEXT = "مش قادر أقرا الملف. ابعته تاني أو اكتب المحتوى في رسالة."


class _StorageUnavailable(Exception):
    pass


class StoredMedia(_BoundaryValue):
    work_id: str
    source_blob_ref: str
    normalized_blob_ref: str
    byte_hash: str
    mime: str
    size: int
    duration: float | None
    stage: Literal["extract", "associate"]


@dataclass
class MediaRetriever:
    steward: Steward
    media_store: MediaStore
    telegram: TelegramFiles
    converter: AudioConverter
    scope: MediaScope
    principal: Principal
    authority_check: Callable[[], bool] = field(repr=False)
    policy: StewardPolicy
    checkpoint: Callable[[str], None] = lambda name: None
    failure_template: str = "media:resend-v1"
    failure_text: str = RESEND_TEXT
    failure_renderer: Callable[[str], str] | None = None

    def _put_blob(self, data: bytes, mime: str) -> str:
        try:
            return self.media_store.put(self.scope, data, mime)
        except Exception:
            raise _StorageUnavailable() from None

    def _get_blob(self, reference: str) -> bytes:
        try:
            return self.media_store.get(self.scope, reference, MAX_AUDIO_BYTES)
        except Exception:
            raise _StorageUnavailable() from None

    def _valid(self) -> bool:
        actor = self.principal
        return (
            actor.doctor_id == self.scope.doctor_id
            and actor.actor_kind in {"doctor", "patient"}
            and actor.actor_kind in actor.verified_roles
            and (
                actor.actor_kind != "patient"
                or actor.patient_id == getattr(self.scope, "patient_id", None)
            )
            and self.authority_check()
        )

    def _get(self, id: str) -> MediaWork | None:
        row = self.steward.store.get(self.scope, "media_work", id)
        return from_record(row, MediaWork) if row else None

    def _commit(
        self,
        work: MediaWork,
        claim: Claim | None = None,
        *,
        failure: str | None = None,
    ) -> bool:
        if not self._valid():
            return False
        now, store = self.steward.clock(), self.steward.store
        # Only operational timing is used for intake; no patient is synthesized.
        policy = self.policy
        lease = (
            store.acquire_patient(
                self.scope, "media:" + uuid4().hex, now, policy.operations.lease_ttl
            )
            if isinstance(self.scope, PatientScope)
            else None
        )
        if isinstance(self.scope, PatientScope) and lease is None:
            return False
        try:
            id = f"media:{work.id}:{work.version}"
            command = CommandEnvelope(
                command_id=id,
                principal=self.principal,
                scope=self.scope,
                payload={"media_work_id": work.id, "version": work.version},
                requested_at=now,
                fence=lease,
                work_claim=claim,
            )
            extras = []
            intents = []
            if failure:
                if self.failure_renderer:
                    self.failure_text = self.failure_renderer(failure)
                review = create_review(
                    CreateReview(
                        event_id=id,
                        source_type="media_work"
                        if isinstance(self.scope, PatientScope)
                        else "intake",
                        source_id=work.id
                        if isinstance(self.scope, PatientScope)
                        else self.scope.intake_id,
                        source_version=work.version,
                        review_kind=ReviewKind.media_failure,
                        owner_doctor_id=self.scope.doctor_id,
                        patient_id=self.scope.patient_id
                        if isinstance(self.scope, PatientScope)
                        else None,
                        review_at=now + policy.timing.result_review_interval,
                    ),
                    now,
                    policy.timing,
                ).aggregate
                doctor_row = store.get(self.scope, "doctor_authority", self.scope.doctor_id)
                if doctor_row is None:
                    return False
                doctor = from_record(doctor_row, DoctorAuthority)
                if not doctor.approved:
                    return False
                if isinstance(self.scope, PatientScope):
                    profile = store.get_patient_profile(self.scope)
                    if profile is None:
                        return False
                    intent = make_intent(
                        self.scope,
                        id,
                        (),
                        "solicited_reply",
                        self.failure_template,
                        now,
                        policy,
                        doctor,
                        profile,
                        audience="patient" if self.principal.actor_kind == "patient" else "doctor",
                        template_id=self.failure_template,
                    )
                    intent = OutboundIntent.model_validate(
                        intent.model_dump() | {"payload": {"text": self.failure_text}}
                    )
                else:
                    logical = keys.digest(id + ":resend")
                    intent = OutboundIntent(
                        id=logical,
                        scope=self.scope,
                        scope_kind="intake",
                        audience="doctor",
                        logical_key=logical,
                        source_event_ids=(id,),
                        source_versions=(),
                        recipient_ref=doctor.recipient_ref,
                        notification_purpose="solicited_reply",
                        eligibility_class="routine",
                        payload_ref=self.failure_template,
                        payload_digest=keys.digest(self.failure_text),
                        payload={"text": self.failure_text},
                        conversation_sequence=0,
                        expires_at=now + policy.timing.overdue_review_interval,
                        status="queued",
                        delivery_lease_seconds=int(policy.operations.lease_ttl.total_seconds()),
                        work_clock=OperationalClock(next_action_at=now, work_lane="delivery"),
                        created_at=now,
                        updated_at=now,
                        doctor_auth_epoch_seen=doctor.auth_epoch,
                        recipient_auth_epoch_seen=doctor.auth_epoch,
                        template_id=self.failure_template,
                    )
                work = MediaWork.model_validate(
                    work.model_dump()
                    | {
                        "state": "needs_attention",
                        "last_error": failure,
                        "review_obligation_id": review.id,
                        "resend_intent_id": intent.id,
                    }
                )
                extras.append(to_record(review, self.scope))
                intents.append(to_record(intent, self.scope))
            record = to_record(work, self.scope)
            event = to_record(
                AuditEvent(
                    id=id,
                    event_id=id,
                    command_id=id,
                    scope=self.scope,
                    actor=self.principal,
                    event_type="media_" + ("needs_attention" if failure else work.stage),
                    accepted_at=now,
                    created_at=now,
                    updated_at=now,
                    aggregate_refs=(record.ref,),
                ),
                self.scope,
            )
            all_records = (record, *extras, event, *intents)
            result = store.commit(
                CommitRequest(
                    command=command,
                    puts=(record, *extras),
                    events=(event,),
                    intents=tuple(intents),
                    expected=tuple(r.ref for r in all_records),
                )
            )
            return result.status in {"accepted", "duplicate"}
        finally:
            if lease:
                store.release_patient(lease)

    def _failure(self, work: MediaWork, claim: Claim, reason: str) -> MediaFailure:
        now = self.steward.clock()
        policy = self.policy
        assert work.work_clock is not None
        revised = MediaWork.model_validate(
            work.model_dump()
            | {
                "version": work.version + 1,
                "updated_at": now,
                "processing_claim": None,
                "work_clock": transition_operational_clock(
                    work.work_clock, now + policy.timing.result_review_interval, error=reason
                ),
            }
        )
        if not self._commit(revised, claim, failure=reason):
            return MediaFailure(reason="stale_work", request_resend=False)
        saved = self._get(work.id)
        assert saved is not None
        return MediaFailure(
            reason=reason,
            durable=True,
            review_obligation_id=saved.review_obligation_id,
            resend_intent_id=saved.resend_intent_id,
        )

    def extraction_result(
        self,
        receipt_id: str,
        *,
        transcript_ref: str | None = None,
        association_ref: str | None = None,
        failure: str | None = None,
    ) -> bool | MediaFailure:
        """Checkpoint the extractor's private transcript and explicit operational association."""
        work = self._get(keys.digest(receipt_id))
        if work is None or work.stage not in {"extract", "associate"}:
            return False
        if work.state == "completed":
            return True
        if association_ref and not (transcript_ref or work.transcript_ref):
            return False
        if work.state == "needs_attention":
            return MediaFailure(
                reason=work.last_error or "needs_attention",
                durable=True,
                review_obligation_id=work.review_obligation_id,
                resend_intent_id=work.resend_intent_id,
            )
        now = self.steward.clock()
        claim = self.steward.store.claim_work(
            to_record(work, self.scope).scoped_key(self.scope),
            work.version,
            "scribe-media",
            now,
            self.policy.operations.claim_ttl,
            start_extraction=True,
        )
        if claim is None:
            return False
        work = self._get(work.id)
        assert work is not None
        if failure:
            return self._failure(work, claim, failure)
        changed = MediaWork.model_validate(
            work.model_dump()
            | {
                "version": work.version + 1,
                "updated_at": now,
                "processing_claim": None,
                "transcript_ref": transcript_ref or work.transcript_ref,
                "association_ref": association_ref,
                "state": "completed" if association_ref else "pending",
                "stage": "associate" if association_ref else "extract",
                "work_clock": None
                if association_ref
                else OperationalClock(
                    next_action_at=now + self.policy.timing.result_review_interval,
                    work_lane="media",
                ),
            }
        )
        return self._commit(changed, claim)

    def fetch_telegram_file(self, handle: str, *, receipt_id: str) -> StoredMedia | MediaFailure:
        if not self._valid():
            return MediaFailure(reason="scope_unavailable", request_resend=False)
        store, now = self.steward.store, self.steward.clock()
        receipt_row = store.get(self.scope, "inbound_receipt", receipt_id)
        if receipt_row is None and self.principal.actor_kind == "doctor":
            receipt_row = store.get(
                TenantScope(doctor_id=self.scope.doctor_id), "inbound_receipt", receipt_id
            )
        if receipt_row is None:
            return MediaFailure(reason="receipt_missing", request_resend=False)
        receipt = from_record(receipt_row, InboundReceipt)
        if receipt.source_subject != self.principal.subject:
            return MediaFailure(reason="scope_unavailable", request_resend=False)
        if receipt.provider_media_handle != handle:
            return MediaFailure(reason="handle_mismatch", request_resend=False)
        id = keys.digest(receipt.id)
        work = self._get(id)
        if work is None:
            work = MediaWork(
                id=id,
                scope=self.scope,
                receipt_id=receipt.id,
                provider_handle_ref=handle,
                created_at=now,
                updated_at=now,
                work_clock=OperationalClock(next_action_at=now, work_lane="media"),
            )
            if not self._commit(work):
                return MediaFailure(reason="stale_work", request_resend=False)
        self.checkpoint("enqueued")
        for _ in range(2):
            work = self._get(id)
            assert work is not None
            if work.state == "needs_attention":
                return MediaFailure(
                    reason=work.last_error or "needs_attention",
                    durable=True,
                    review_obligation_id=work.review_obligation_id,
                    resend_intent_id=work.resend_intent_id,
                )
            if work.stage in {"extract", "associate"}:
                break
            now = self.steward.clock()
            policy = self.policy
            row = to_record(work, self.scope)
            claim = store.claim_work(
                row.scoped_key(self.scope),
                work.version,
                "media-worker",
                now,
                policy.operations.claim_ttl,
            )
            if claim is None:
                return MediaFailure(reason="work_busy", request_resend=False)
            work = self._get(id)
            assert work is not None and work.work_clock is not None
            self.checkpoint("claimed_" + work.stage)
            if work.work_clock.attempt_count > policy.operations.max_inbound_attempts:
                return self._failure(work, claim, "attempts_exhausted")
            try:
                if work.stage == "fetch":
                    download = self.telegram.fetch(handle)
                    if isinstance(download, MediaFailure):
                        return self._failure(work, claim, download.reason)
                    self.checkpoint("downloaded")
                    actual = sniff(download.data)
                    if actual in {"png", "jpeg"}:
                        mime = image_info(download.data).mime
                    elif len(download.data) <= MAX_AUDIO_BYTES:
                        mime = {
                            "ogg": "audio/ogg",
                            "wav": "audio/wav",
                            "m4a": "audio/mp4",
                            "mp3": "audio/mpeg",
                        }[actual]
                    else:
                        raise MediaInvalid("too_large")
                    reference = self._put_blob(download.data, mime)
                    self.checkpoint("source_stored")
                    changed = {
                        "stage": "normalize",
                        "source_blob_ref": reference,
                        "byte_hash": sha256(download.data).hexdigest(),
                        "size": len(download.data),
                        "mime": mime,
                    }
                else:
                    assert work.source_blob_ref is not None
                    data = self._get_blob(work.source_blob_ref)
                    actual = sniff(data)
                    self.checkpoint("normalization_loaded")
                    if actual in {"png", "jpeg"}:
                        image_info(data)
                        normalized, duration = work.source_blob_ref, None
                    else:
                        converted = self.converter.convert(data, actual)
                        if isinstance(converted, ConversionFailure):
                            return self._failure(work, claim, converted.reason)
                        normalized = self._put_blob(converted.data, "audio/mpeg")
                        duration = converted.duration
                    self.checkpoint("normalized_stored")
                    changed = {
                        "stage": "extract",
                        "normalized_blob_ref": normalized,
                        "duration": duration,
                    }
            except MediaInvalid as error:
                return self._failure(work, claim, str(error))
            except _StorageUnavailable:
                return MediaFailure(reason="storage_unavailable", request_resend=False)
            except Exception:
                # Leave the claim and clock intact for crash/outage recovery; no false completion.
                raise
            revised = MediaWork.model_validate(
                work.model_dump()
                | changed
                | {
                    "version": work.version + 1,
                    "updated_at": self.steward.clock(),
                    "state": "pending",
                    "processing_claim": None,
                    "work_clock": transition_operational_clock(
                        work.work_clock,
                        self.steward.clock() + policy.timing.result_review_interval
                        if changed["stage"] == "extract"
                        else self.steward.clock(),
                    ),
                }
            )
            if not self._commit(revised, claim):
                return MediaFailure(reason="stale_work", request_resend=False)
            self.checkpoint("committed_" + str(changed["stage"]))
        saved = self._get(id)
        assert saved is not None
        return StoredMedia.model_validate(
            saved.model_dump(
                include={
                    "source_blob_ref",
                    "normalized_blob_ref",
                    "byte_hash",
                    "mime",
                    "size",
                    "duration",
                    "stage",
                }
            )
            | {"work_id": saved.id}
        )

    def sweep(self, *, limit: int = 20) -> int:
        """Scoped bounded recovery; callers own the global due-index dispatch."""
        store, now = self.steward.store, self.steward.clock()
        cap = WorkerCapability(
            service_subject="media-sweep",
            permitted_lanes=frozenset({"media"}),
            resolved_scope=self.scope,
            auth_expiry=now + self.policy.operations.claim_ttl,
            invocation_id=uuid4().hex,
        )
        due, _ = store.query_due("media", "0", now, limit=limit, capability=cap)
        handled = 0
        for item in due:
            work = self._get(item.id)
            if work is None or work.work_clock is None or work.work_clock.next_action_at > now:
                continue
            if work.stage in {"fetch", "normalize"} and work.state != "needs_attention":
                self.fetch_telegram_file(work.provider_handle_ref, receipt_id=work.receipt_id)
            else:
                policy = self.policy
                claim = store.claim_work(
                    item.record_key,
                    work.version,
                    "media-sweep",
                    now,
                    policy.operations.claim_ttl,
                    count_attempt=work.state != "needs_attention",
                )
                if claim is None:
                    continue
                current = self._get(work.id)
                assert current is not None and current.work_clock is not None
                if current.state != "needs_attention":
                    self._failure(current, claim, "extraction_unresolved")
                else:
                    revised = MediaWork.model_validate(
                        current.model_dump()
                        | {
                            "version": current.version + 1,
                            "updated_at": now,
                            "processing_claim": None,
                            "work_clock": transition_operational_clock(
                                current.work_clock, now + policy.timing.overdue_review_interval
                            ),
                        }
                    )
                    self._commit(revised, claim)
            handled += 1
        return handled


def fetch_telegram_file(
    handle: str, *, retriever: MediaRetriever, receipt_id: str
) -> StoredMedia | MediaFailure:
    return retriever.fetch_telegram_file(handle, receipt_id=receipt_id)
