"""Browser adapter staging and recovery; evidence is processed by existing workers."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

from sanad.auth.login import LoginService
from sanad.channels.telegram.router import TelegramRuntime
from sanad.domain import ObservationRef, PatientScope, Principal
from sanad.media.images import normalize_document, source_image_mime
from sanad.media.limits import MAX_IMAGE_BYTES, MediaInvalid
from sanad.media.source import FileBytes, MediaFailure, MediaSource
from sanad.media.storage import UploadStorage
from sanad.safety import to_incident_facts
from sanad.safety.models import ScreenVerdict
from sanad.store import keys
from sanad.store.keys import ScopedKey
from sanad.store.protocol import Store
from sanad.store.records import (
    InboundReceipt,
    OperationalClock,
    StoredRecord,
    UploadStage,
    WebSession,
    from_record,
    to_record,
)

STAGING_TTL = timedelta(minutes=10)


@dataclass
class StagedUpload:
    store: Store
    storage: UploadStorage
    scope: PatientScope
    subject: str
    receipt_id: str

    def fetch(self, handle: str) -> FileBytes | MediaFailure:
        if not handle.startswith("upload:"):
            return MediaFailure(reason="invalid_handle", request_resend=False)
        row = self.store.get(self.scope, "upload_stage", handle.removeprefix("upload:"))
        if row is None:
            return MediaFailure(reason="upload_missing", request_resend=False)
        stage = from_record(row, UploadStage)
        if (
            stage.state != "attached"
            or stage.subject != self.subject
            or stage.receipt.id != self.receipt_id
            or stage.receipt.provider_media_handle != handle
            or stage.object_ref
            != self.storage.upload_reference(self.scope, stage.id, stage.content_digest)
        ):
            return MediaFailure(reason="upload_scope_mismatch", request_resend=False)
        try:
            data = self.storage.get_upload(
                self.scope, stage.id, stage.content_digest, MAX_IMAGE_BYTES
            )
        except Exception:
            return MediaFailure(reason="storage_unavailable", request_resend=False)
        if (
            data is None
            or len(data) != stage.size
            or sha256(data).hexdigest() != stage.content_digest
        ):
            return MediaFailure(reason="invalid_upload_blob", request_resend=False)
        return FileBytes(data=data)


def patient_source(
    receipt: InboundReceipt,
    actor: Principal,
    store: Store,
    storage: UploadStorage,
    telegram: MediaSource,
) -> MediaSource:
    """The adapter selection ends here, before MediaRetriever or domain processing."""
    if receipt.channel == "browser":
        return StagedUpload(
            store,
            storage,
            PatientScope(doctor_id=actor.doctor_id or "", patient_id=actor.patient_id or ""),
            actor.subject,
            receipt.id,
        )
    return telegram


@dataclass
class UploadIngress:
    runtime: TelegramRuntime
    login: LoginService
    storage: UploadStorage
    submit: Callable[[ScopedKey], None] | None = None
    checkpoint: Callable[[str], None] = lambda name: None

    def receipt(
        self, session: WebSession, caption: str, verdict: ScreenVerdict, upload_id: str
    ) -> InboundReceipt:
        auth = self.runtime.store.authorize(session.scope.bot_id, session.subject)
        if auth.principal.actor_kind != "patient" or auth.binding is None:
            raise PermissionError("upload_authority_changed")
        scope = PatientScope(doctor_id=session.doctor_id, patient_id=session.patient_id or "")
        if (
            auth.principal.doctor_id != scope.doctor_id
            or auth.principal.patient_id != scope.patient_id
        ):
            raise PermissionError("upload_scope_changed")
        now = self.runtime.clock()
        transport_key = session.scope.bot_id + ":" + upload_id
        return InboundReceipt(
            id=keys.inbound("browser", keys.digest(transport_key)).pk,
            scope=scope,
            transport="browser",
            transport_key=transport_key,
            source_subject=session.subject,
            source_chat=auth.private_chat_id or "",
            channel="browser",
            principal=auth.principal,
            kind="photo",
            payload={"kind": "photo", "text": caption, "media_handle": "upload:" + upload_id},
            provider_media_handle="upload:" + upload_id,
            received_at=now,
            created_at=now,
            updated_at=now,
            work_clock=OperationalClock(next_action_at=now, work_lane="ingress"),
            safety_screen_state="screened",
            safety_policy_version=verdict.policy_version,
            safety_result=verdict.model_dump(mode="json"),
        )

    def rejected_caption(self, receipt: InboundReceipt, verdict: ScreenVerdict) -> None:
        """A rejected/disconnected image cannot erase its permitted dangerous caption."""
        if verdict.level != "danger":
            return
        rejected = InboundReceipt.model_validate(
            receipt.model_dump()
            | {
                "kind": "text",
                "provider_media_handle": None,
                "payload": {"kind": "text", "text": (receipt.payload or {}).get("text", "")},
            }
        )
        accepted = self.runtime.store.accept_inbound(
            rejected.transport_key, to_record(rejected, rejected.scope)
        )
        if accepted.record is None or accepted.status not in {"created", "existing"}:
            raise RuntimeError("caption_receipt_unavailable")
        saved = from_record(accepted.record, InboundReceipt)
        self.danger(saved, verdict)
        self.handoff(accepted.record)

    def danger(self, receipt: InboundReceipt, verdict: ScreenVerdict) -> None:
        if verdict.level != "danger":
            return
        assert isinstance(receipt.scope, PatientScope)
        facts, severity = to_incident_facts(
            verdict,
            source=ObservationRef(observation_id=receipt.id),
            policy=self.runtime.safety_policy,
        )
        self.runtime.urgent.raise_incident(
            receipt.scope,
            facts.unique_source_key,
            facts.as_payload(),
            severity,
            self.runtime.clock(),
        )

    def handoff(self, row: StoredRecord) -> None:
        if self.submit is None:
            return
        receipt = from_record(row, InboundReceipt)
        try:
            self.submit(row.scoped_key(receipt.scope))
        except Exception:
            self.runtime.count("upload_handoff_deferred")

    def stage(
        self, session: WebSession, receipt: InboundReceipt, data: bytes, declared: str
    ) -> UploadStage:
        actual = source_image_mime(data)
        if actual != declared:
            raise MediaInvalid("content_type_mismatch")
        # Decode bounded pixels now so corrupt/truncated bodies never reach S3.
        # The downstream normalization and exact reader input remain unchanged.
        normalize_document(data)
        assert isinstance(receipt.scope, PatientScope)
        assert session.binding_id and session.binding_epoch is not None and session.consent_version
        id = (receipt.provider_media_handle or "").removeprefix("upload:")
        digest, now = sha256(data).hexdigest(), self.runtime.clock()
        stage = UploadStage(
            id=id,
            scope=receipt.scope,
            subject=session.subject,
            session_scope=session.scope,
            session_id=session.id,
            binding_id=session.binding_id,
            binding_epoch=session.binding_epoch,
            consent_version=session.consent_version,
            auth_epoch=session.auth_epoch,
            content_digest=digest,
            content_type=actual,
            size=len(data),
            object_ref=self.storage.upload_reference(receipt.scope, id, digest),
            receipt=receipt,
            recovery_deadline=now + STAGING_TTL,
            work_clock=OperationalClock(next_action_at=now + STAGING_TTL, work_lane="operational"),
            created_at=now,
            updated_at=now,
        )
        if not self.runtime.store.reserve_upload(stage):
            raise PermissionError("upload_authority_changed")
        self.checkpoint("reserved")
        if not self.runtime.store.upload_authorized(stage):
            raise PermissionError("upload_authority_changed")
        self.storage.put_upload(stage.scope, id, digest, data, actual)
        self.checkpoint("stored")
        return stage

    def accept(self, stage: UploadStage) -> StoredRecord:
        accepted = self.runtime.store.accept_inbound(
            stage.receipt.transport_key,
            to_record(stage.receipt, stage.scope),
            upload_id=stage.id,
        )
        if accepted.record is None or accepted.status not in {"created", "existing"}:
            raise PermissionError("upload_attachment_deferred")
        self.checkpoint("accepted")
        return accepted.record

    def recover(self, row: StoredRecord) -> None:
        stage = from_record(row, UploadStage)
        store, now = self.runtime.store, self.runtime.clock()
        fresh = store.get(stage.scope, "upload_stage", stage.id)
        if fresh is None:
            return
        stage = from_record(fresh, UploadStage)
        if stage.work_clock is None or stage.work_clock.next_action_at > now:
            return
        if stage.state == "reserved":
            try:
                data = self.storage.get_upload(
                    stage.scope, stage.id, stage.content_digest, MAX_IMAGE_BYTES
                )
            except ValueError:
                data = None
            if data is not None and len(data) == stage.size:
                try:
                    saved = self.accept(stage)
                except PermissionError:
                    pass
                else:
                    self.handoff(saved)
                    return
        if stage.state == "reserved":
            self.rejected_caption(
                stage.receipt, ScreenVerdict.model_validate(stage.receipt.safety_result)
            )
        discarded = store.discard_upload(stage.scope, stage.id, stage.version)
        if discarded is not None:
            self.storage.delete_upload(stage.scope, stage.id, stage.content_digest)


def new_upload_id() -> str:
    return uuid4().hex
