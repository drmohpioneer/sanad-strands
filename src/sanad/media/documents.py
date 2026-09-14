"""Bounded, isolated PDF rasterization. The application never opens PDF objects."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from sanad.media.limits import (
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_PAGES,
    MAX_IMAGE_BYTES,
    MediaInvalid,
    image_info,
)

if TYPE_CHECKING:
    from sanad.domain import Principal, Provenance
    from sanad.media.retrieve import MediaRetriever
    from sanad.media.vision import DocumentFailure, DocumentRead, ItemRead, VisionAdapter
    from sanad.scribe.repository import ScribeRepository
    from sanad.steward.apply import CommitBuilder
    from sanad.store._base import Check, StoreBase
    from sanad.store.protocol import Store
    from sanad.store.records import (
        Claim,
        CommitRequest,
        CommitResult,
        DocumentPageManifest,
        DocumentPageWork,
        InboundReceipt,
        IntakeDraft,
        MediaWork,
        StoredRecord,
    )

RENDERER_NAME = "pypdfium2"
RENDERER_VERSION = "5.13.0:png-v1"
VALIDATION_SECONDS = 5
RENDER_SECONDS = 10
ADDRESS_SPACE_BYTES = 1_500_000_000

# An isolated interpreter loads only the rasterizer. No forms/JavaScript environment,
# attachments, text API, URLs or user-controlled file names enter this process.
_CHILD = r"""
import io, json, math, resource, socket, sys
if sys.platform == "darwin":
    sys.stderr.write("pdf_address_space_limit_unavailable_on_development_host\n")
else:
    resource.setrlimit(resource.RLIMIT_AS, (1500000000, 1500000000))
def deny(*args, **kwargs):
    raise OSError("network_disabled")
socket.socket = deny
socket.create_connection = deny
try:
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    data = sys.stdin.buffer.read(20000001)
    if len(data) > 20000000:
        raise ValueError("document_too_large")
    if not data.startswith(b"%PDF-") or not data.rstrip().endswith(b"%%EOF"):
        raise ValueError("document_invalid")
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        raise ValueError(
            "document_encrypted" if error.err_code == raw.FPDF_ERR_PASSWORD else "document_invalid"
        )
    with pdf:
        if raw.FPDF_GetSecurityHandlerRevision(pdf) != -1:
            raise ValueError("document_encrypted")
        count = len(pdf)
        if count < 1:
            raise ValueError("document_invalid")
        if count > 10:
            raise ValueError("document_too_many_pages")
        if sys.argv[1] == "validate":
            sys.stdout.buffer.write(json.dumps({"pages": count}).encode())
        else:
            index = int(sys.argv[1])
            if not 1 <= index <= count:
                raise ValueError("document_unreadable")
            page = pdf[index - 1]
            try:
                width, height = page.get_size()
                if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                    raise ValueError("document_unreadable")
                longest = 2000
                while True:
                    bitmap = page.render(scale=longest / max(width, height), may_draw_forms=False)
                    try:
                        image = bitmap.to_pil().convert("RGB")
                        if max(image.size) > 2000 or image.width * image.height > 20000000:
                            raise ValueError("document_unreadable")
                        blank = all(low >= 250 for low, high in image.getextrema())
                        output = io.BytesIO()
                        image.save(output, format="PNG")
                        image.close()
                        encoded = output.getvalue()
                    finally:
                        bitmap.close()
                    if len(encoded) <= 8 * 1024 * 1024:
                        sys.stdout.buffer.write((b"blank\n" if blank else b"page\n") + encoded)
                        break
                    if longest == 1000:
                        raise ValueError("document_unreadable")
                    longest = max(1000, int(longest * 0.8))
            finally:
                page.close()
except BaseException as error:
    reason = str(error)
    if reason not in {
        "document_invalid", "document_encrypted", "document_too_large",
        "document_too_many_pages", "document_unreadable",
    }:
        reason = "document_invalid" if sys.argv[1] == "validate" else "document_unreadable"
    sys.stdout.buffer.write(reason.encode())
    sys.exit(1)
"""


@dataclass(frozen=True)
class DocumentInfo:
    pages: int
    renderer_name: str = RENDERER_NAME
    renderer_version: str = RENDERER_VERSION


@dataclass(frozen=True)
class RenderedPage:
    data: bytes
    blank: bool

    @property
    def byte_hash(self) -> str:
        return sha256(self.data).hexdigest()


def _isolated(data: bytes, operation: str, timeout: float) -> bytes:
    if len(data) > MAX_DOCUMENT_BYTES:
        raise MediaInvalid("document_too_large")
    fallback = "document_invalid" if operation == "validate" else "document_unreadable"
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _CHILD, operation],
            input=data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        raise MediaInvalid(fallback) from None
    if b"pdf_address_space_limit_unavailable_on_development_host" in result.stderr:
        logging.getLogger(__name__).info(
            "PDF address-space limit is unavailable on this development host"
        )
    if result.returncode:
        reason = result.stdout.decode(errors="replace")
        raise MediaInvalid(
            reason
            if reason in {"document_encrypted", "document_too_many_pages", "document_too_large"}
            else fallback
        )
    return result.stdout


def validate_document(data: bytes) -> DocumentInfo:
    result = _isolated(data, "validate", VALIDATION_SECONDS)
    try:
        pages = json.loads(result)["pages"]
        if type(pages) is not int or not 1 <= pages <= MAX_DOCUMENT_PAGES:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise MediaInvalid("document_invalid") from None
    return DocumentInfo(pages=pages)


def render_page(
    data: bytes, page_index: int, *, renderer_version: str = RENDERER_VERSION
) -> RenderedPage:
    if renderer_version != RENDERER_VERSION or not 1 <= page_index <= MAX_DOCUMENT_PAGES:
        raise MediaInvalid("document_unreadable")
    result = _isolated(data, str(page_index), RENDER_SECONDS)
    marker, _, image = result.partition(b"\n")
    if marker not in {b"page", b"blank"} or len(image) > MAX_IMAGE_BYTES:
        raise MediaInvalid("document_unreadable")
    info = image_info(image)
    if max(info.width, info.height) > 2000:
        raise MediaInvalid("document_unreadable")
    return RenderedPage(data=image, blank=marker == b"blank")


# Complete serialized records, including their storage envelope, stay below DynamoDB's cap.
AGGREGATE_BYTES = 300 * 1024
ITEM_BYTES = 350 * 1024


def page_guards(
    store: StoreBase, request: CommitRequest, page: DocumentPageWork, current: StoredRecord | None
) -> list[Check] | None:
    """Reconstruct the child's immutable parent binding and forward-only fenced stages."""
    from sanad.store._base import Check
    from sanad.store.records import DocumentPageWork, canonical_json, record_item, to_record

    parent = store.get(page.scope, "media_work", page.parent_id)
    if (
        not parent
        or parent.body.get("mime") != "application/pdf"
        or parent.body.get("byte_hash") != page.source_hash
        or parent.body.get("receipt_id") != page.receipt_id
        or (parent.body.get("state") == "completed" and not page.refresh_of)
        or len(canonical_json(record_item(to_record(page, page.scope)))) + 128 > ITEM_BYTES
    ):
        return None
    manifests = parent.body.get("document_pages", [])
    assert isinstance(manifests, list)
    if not any(
        isinstance(m, dict)
        and m["work_id"] == (page.refresh_of or page.id)
        and m["page_index"] == page.manifest.page_index
        and m["renderer_version"] == page.manifest.renderer_version
        for m in manifests
    ):
        return None
    if page.refresh_of:
        from sanad.store.keys import IntakeScope

        original = store.get(page.scope, "document_page_work", page.refresh_of)
        if not isinstance(page.scope, IntakeScope) or not original:
            return None
        original_page = DocumentPageWork.model_validate(original.body)
        if original_page.manifest.model_copy(update={"work_id": page.id}) != page.manifest:
            return None
    if page.reads and any(
        r.provenance.source_observation_id != page.receipt_id
        or r.provenance.source_region is None
        or r.provenance.source_region.asset_ref != page.manifest.blob_ref
        for r in (page.reads.first, page.reads.second)
    ):
        return None
    if current is None:
        if (
            page.version != 1
            or page.stage != ("read" if page.refresh_of else "render")
            or page.state != "pending"
        ):
            return None
    else:
        old = DocumentPageWork.model_validate(current.body)
        invalidated = old.model_copy(
            update={
                "version": page.version,
                "updated_at": page.updated_at,
                "state": "unreadable",
                "last_error": "document_unreadable",
            }
        )
        if old.stage == "commit" and page == invalidated:
            return [Check(parent.key, parent.version)]
        claim = request.command.work_claim
        if not claim or claim.record_key.key != current.key:
            return None
        if (
            old.refresh_of != page.refresh_of
            or (old.parent_id, old.receipt_id, old.source_hash, old.scope)
            != (
                page.parent_id,
                page.receipt_id,
                page.source_hash,
                page.scope,
            )
            or old.stage == "commit"
        ):
            return None
        if old.manifest.byte_hash and old.manifest != page.manifest:
            return None
        if (old.manifest.work_id, old.manifest.page_index, old.manifest.renderer_version) != (
            page.manifest.work_id,
            page.manifest.page_index,
            page.manifest.renderer_version,
        ):
            return None
        stages = ("render", "read", "commit")
        if stages.index(page.stage) < stages.index(old.stage):
            return None
    return [Check(parent.key, parent.version)]


class DocumentPipeline:
    """Bounded page-at-a-time reads; committed pages are replayed without provider calls."""

    def __init__(
        self,
        retriever: MediaRetriever,
        receipt: InboundReceipt,
        source: Provenance,
        adapter: Callable[[Provenance], VisionAdapter],
        *,
        kind_hint: str,
        context_names: tuple[str, ...] = (),
        on_page: Callable[[DocumentRead, int], None] | None = None,
    ) -> None:
        self.retriever, self.receipt, self.source = retriever, receipt, source
        self.adapter, self.kind_hint, self.context_names = adapter, kind_hint, context_names
        self.on_page = on_page or (lambda read, index: None)
        self.store, self.scope = retriever.steward.store, retriever.scope

    def _page(self, id: str) -> DocumentPageWork | None:
        from sanad.store.records import DocumentPageWork, from_record

        row = self.store.get(self.scope, "document_page_work", id)
        return from_record(row, DocumentPageWork) if row else None

    def _save(self, page: DocumentPageWork, claim: Claim | None = None) -> bool:
        from sanad.store.records import AuditEvent, CommandEnvelope, CommitRequest, to_record

        r, now = self.retriever, self.retriever.steward.clock()
        if not r._valid():
            return False
        row = to_record(page, self.scope)
        id = f"document-page:{page.id}:{page.version}"
        command = CommandEnvelope(
            command_id=id,
            principal=r.principal,
            scope=self.scope,
            payload={"document_page_id": page.id},
            requested_at=now,
            fence=None,
            work_claim=claim,
        )
        event = to_record(
            AuditEvent(
                id=id,
                event_id=id,
                command_id=id,
                scope=self.scope,
                actor=r.principal,
                event_type="document_page_" + page.stage,
                accepted_at=now,
                created_at=now,
                updated_at=now,
                aggregate_refs=(row.ref,),
            ),
            self.scope,
        )
        outcome = self.store.commit(
            CommitRequest(
                command=command, puts=(row,), events=(event,), expected=(row.ref, event.ref)
            )
        )
        return outcome.status in {"accepted", "duplicate"}

    def _advance(self, page: DocumentPageWork, claim: Claim, **changes: object) -> DocumentPageWork:
        from sanad.store.records import DocumentPageWork

        revised = DocumentPageWork.model_validate(
            page.model_dump()
            | changes
            | {
                "version": page.version + 1,
                "updated_at": self.retriever.steward.clock(),
                "processing_claim": None,
            }
        )
        from sanad.store.records import to_record

        try:
            check_item_size(to_record(revised, revised.scope))
        except MediaInvalid:
            if revised.reads_ref:
                revised = revised.model_copy(
                    update={"reads": None, "last_error": "document_too_detailed"}
                )
            else:
                raise
        if not self._save(revised, claim):
            raise MediaInvalid("stale_work")
        return revised

    def _image(self, data: bytes, page: DocumentPageWork) -> bytes:
        from botocore.exceptions import ClientError  # type: ignore[import-untyped]

        from sanad.media.storage import S3MediaStore

        assert page.manifest.blob_ref
        try:
            image = self.retriever.media_store.get(
                self.scope, page.manifest.blob_ref, MAX_IMAGE_BYTES
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"NoSuchKey", "404"}:
                raise
            rendered = render_page(
                data, page.manifest.page_index, renderer_version=page.manifest.renderer_version
            )
            image = rendered.data
            if sha256(image).hexdigest() != page.manifest.byte_hash:
                raise MediaInvalid("document_unreadable") from None
            storage = self.retriever.media_store
            if isinstance(storage, S3MediaStore):
                storage.put_page(
                    self.scope,
                    page.receipt_id,
                    page.manifest.page_index,
                    page.manifest.renderer_version,
                    image,
                )
            else:
                storage.put(self.scope, image, "image/png")
        except ValueError:
            raise MediaInvalid("document_unreadable") from None
        if sha256(image).hexdigest() != page.manifest.byte_hash:
            raise MediaInvalid("document_unreadable")
        return image

    def run(
        self, work: MediaWork, *, refresh_key: str | None = None
    ) -> tuple[DocumentRead | DocumentFailure, tuple[DocumentPageManifest, ...]]:
        import asyncio
        from datetime import timedelta

        from sanad.domain import ImageRegion
        from sanad.media.storage import S3MediaStore
        from sanad.media.vision import DocumentFailure, read_document
        from sanad.store.records import DocumentPageWork, OperationalClock, to_record

        r, now = self.retriever, self.retriever.steward.clock
        assert work.source_blob_ref and work.byte_hash
        data = r.media_store.get(self.scope, work.source_blob_ref, MAX_DOCUMENT_BYTES)
        if sha256(data).hexdigest() != work.byte_hash:
            raise MediaInvalid("document_invalid")
        pages: list[DocumentPageWork] = []
        for manifest in work.document_pages:
            refresh_of = None
            if refresh_key:
                from sanad.store.keys import digest

                original = self._page(manifest.work_id)
                if not original or original.state != "committed":
                    if original:
                        pages.append(original)
                    continue
                refresh_of = manifest.work_id
                manifest = original.manifest.model_copy(
                    update={"work_id": digest(f"{refresh_of}:refresh:{refresh_key}")}
                )
            page = self._page(manifest.work_id)
            if page is None:
                page = DocumentPageWork(
                    id=manifest.work_id,
                    scope=self.scope,
                    parent_id=work.id,
                    receipt_id=work.receipt_id,
                    source_hash=work.byte_hash,
                    manifest=manifest,
                    refresh_of=refresh_of,
                    stage="read" if refresh_of else "render",
                    work_clock=OperationalClock(next_action_at=now(), work_lane="media"),
                    created_at=now(),
                    updated_at=now(),
                )
                if not self._save(page):
                    raise MediaInvalid("stale_work")
            if page.stage == "commit" and page.manifest.blob_ref:
                try:
                    self._image(data, page)
                except MediaInvalid:
                    page = page.model_copy(
                        update={
                            "version": page.version + 1,
                            "updated_at": now(),
                            "state": "unreadable",
                            "last_error": "document_unreadable",
                        }
                    )
                    if not self._save(page):
                        raise MediaInvalid("stale_work") from None
            for _ in range(2):
                if page.stage == "commit":
                    break
                claim = self.store.claim_work(
                    to_record(page, self.scope).scoped_key(self.scope),
                    page.version,
                    "pdf-page",
                    now(),
                    r.policy.operations.claim_ttl,
                )
                if claim is None:
                    raise MediaInvalid("work_busy")
                page = self._page(page.id)
                assert page and page.work_clock
                r.checkpoint(f"document_page_{manifest.page_index}_claimed_{page.stage}")
                clock = OperationalClock(
                    next_action_at=now(),
                    work_lane="media",
                    attempt_count=page.work_clock.attempt_count,
                )
                if page.stage == "render":
                    try:
                        rendered = render_page(
                            data, manifest.page_index, renderer_version=manifest.renderer_version
                        )
                    except MediaInvalid:
                        page = self._advance(
                            page,
                            claim,
                            stage="commit",
                            state="unreadable",
                            work_clock=None,
                            last_error="document_unreadable",
                        )
                        continue
                    reference = (
                        r.media_store.put_page(
                            self.scope,
                            work.receipt_id,
                            manifest.page_index,
                            manifest.renderer_version,
                            rendered.data,
                        )
                        if isinstance(r.media_store, S3MediaStore)
                        else r.media_store.put(self.scope, rendered.data, "image/png")
                    )
                    next_manifest = manifest.model_copy(
                        update={"byte_hash": rendered.byte_hash, "blob_ref": reference}
                    )
                    duplicate = next(
                        (
                            p
                            for p in pages
                            if p.manifest.byte_hash == rendered.byte_hash
                            and p.state in {"committed", "duplicate"}
                        ),
                        None,
                    )
                    page = self._advance(
                        page,
                        claim,
                        manifest=next_manifest,
                        stage="commit" if rendered.blank or duplicate else "read",
                        state="blank"
                        if rendered.blank
                        else "duplicate"
                        if duplicate
                        else "pending",
                        duplicate_of=duplicate.id if duplicate else None,
                        work_clock=None
                        if rendered.blank or duplicate
                        else clock.model_copy(update={"attempt_count": 0}),
                    )
                    r.checkpoint(f"document_page_{manifest.page_index}_rendered")
                    continue
                try:
                    image = self._image(data, page)
                    source = self.source.model_copy(
                        update={
                            "source_region": ImageRegion(
                                asset_ref=page.manifest.blob_ref or "", x=0, y=0, width=1, height=1
                            )
                        }
                    )
                    reads = asyncio.run(
                        read_document(
                            image,
                            "png",
                            kind_hint=self.kind_hint,
                            adapter=self.adapter(source),
                            context_names=self.context_names,
                        )
                    )
                except MediaInvalid:
                    reads = DocumentFailure(reason="document_unreadable")
                r.checkpoint(f"document_page_{manifest.page_index}_reader_returned")
                if isinstance(reads, DocumentFailure):
                    assert page.work_clock
                    attempts = page.work_clock.attempt_count
                    if attempts < 4:
                        page = self._advance(
                            page,
                            claim,
                            state="pending",
                            last_error=reads.reason,
                            work_clock=clock.model_copy(
                                update={
                                    "next_action_at": now()
                                    + timedelta(minutes=(1, 5, 15)[min(attempts - 1, 2)])
                                }
                            ),
                        )
                        raise MediaInvalid("document_retry_pending")
                    page = self._advance(
                        page,
                        claim,
                        stage="commit",
                        state="unreadable",
                        last_error="document_unreadable",
                        work_clock=None,
                    )
                else:
                    reference = r.media_store.put(
                        self.scope, reads.model_dump_json().encode(), "application/json"
                    )
                    page = self._advance(
                        page,
                        claim,
                        stage="commit",
                        state="committed",
                        reads=reads
                        if len(reads.model_dump_json().encode()) < AGGREGATE_BYTES
                        else None,
                        reads_ref=reference,
                        work_clock=None,
                        last_error="document_too_detailed"
                        if len(reads.model_dump_json().encode()) >= AGGREGATE_BYTES
                        else None,
                    )
                r.checkpoint(f"document_page_{manifest.page_index}_committed")
            if page.reads:
                self.on_page(page.reads, manifest.page_index)
            elif page.reads_ref:
                from sanad.media.vision import DocumentRead

                retained = DocumentRead.model_validate_json(
                    r.media_store.get(self.scope, page.reads_ref, MAX_IMAGE_BYTES)
                )
                self.on_page(retained, manifest.page_index)
            pages.append(page)
        r.checkpoint("document_pages_complete")
        return aggregate_pages(
            pages, caption=str((self.receipt.payload or {}).get("text", ""))
        ), tuple(p.manifest for p in pages)


def aggregate_pages(
    pages: list[DocumentPageWork], *, caption: str = ""
) -> DocumentRead | DocumentFailure:
    """Preserve each independent reader's order; page failures block the whole report."""
    from sanad.media.vision import DocumentFailure, DocumentRead
    from sanad.scribe.crosscheck import shift_guard, unreadable_read

    usable = [p for p in pages if p.state == "committed" and p.reads]
    if any(p.last_error == "document_too_detailed" for p in pages):
        return DocumentFailure(reason="document_too_detailed")
    if not usable:
        return DocumentFailure(
            reason="document_blank"
            if all(p.state == "blank" for p in pages)
            else "document_unreadable"
        )
    if all(p.reads and unreadable_read(p.reads) for p in usable):
        return DocumentFailure(reason="document_unreadable")
    blocked = [p.manifest.page_index for p in pages if p.state == "unreadable"]
    for p in usable:
        assert p.reads
        if (
            unreadable_read(p.reads)
            or any(r.unreadable or r.status != "ok" for r in (p.reads.first, p.reads.second))
            or p.reads.disagreements
            or shift_guard(p.reads.first, p.reads.second)
        ):
            blocked.append(p.manifest.page_index)
    first = usable[0].reads
    assert first
    for p in usable:
        assert p.reads
        for field in ("document_type", "printed_date", "printed_identity_hint"):
            if getattr(p.reads.first, field) != getattr(first.first, field):
                blocked.append(p.manifest.page_index)
    from sanad.evidence.classify import classify

    lab_report = classify(first, caption, ()) == "lab_result"
    for p in usable:
        assert p.reads
        if classify(p.reads, caption, ()) != classify(first, caption, ()):
            blocked.append(p.manifest.page_index)
    if lab_report:
        from sanad.safety.aliases import analyte

        seen_values: dict[tuple[str, str | None], tuple[str | None, str | None]] = {}
        for p in usable:
            assert p.reads
            for item in p.reads.first.items:
                key = (analyte(item.item.name or ""), p.reads.first.printed_date)
                value = (item.item.value, item.item.unit)
                if key in seen_values and seen_values[key] != value:
                    blocked.append(p.manifest.page_index)
                seen_values[key] = value
    readers = []
    for slot in ("first", "second"):
        reader = getattr(first, slot)
        readers.append(
            reader.model_copy(
                update={
                    "items": deduplicated_lab_rows(usable, slot)
                    if lab_report
                    else tuple(row for p in usable for row in getattr(p.reads, slot).items),
                    "notes": tuple(note for p in usable for note in getattr(p.reads, slot).notes),
                }
            )
        )
    disagreements = []
    offset = 0
    for p in usable:
        assert p.reads
        for d in p.reads.disagreements:
            parts = d.field.split(".")
            if len(parts) >= 3 and parts[0] == "items":
                parts[1] = str(int(parts[1]) + offset)
            disagreements.append(d.model_copy(update={"field": ".".join(parts)}))
        offset += max(len(p.reads.first.items), len(p.reads.second.items))
    result = DocumentRead(
        first=readers[0],
        second=readers[1],
        disagreements=tuple(disagreements),
        blocked_pages=tuple(sorted(set(blocked))),
    )
    if len(result.model_dump_json().encode()) > AGGREGATE_BYTES:
        return DocumentFailure(reason="document_too_detailed")
    return result


def deduplicated_lab_rows(pages: list[DocumentPageWork], slot: str) -> tuple[ItemRead, ...]:
    from sanad.safety.aliases import analyte

    seen = set()
    rows = []
    for page in pages:
        assert page.reads
        reader = getattr(page.reads, slot)
        for row in reader.items:
            key = (analyte(row.item.name or ""), row.item.value, row.item.unit, reader.printed_date)
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return tuple(rows)


def finalize_pdf(builder: CommitBuilder, association: str) -> None:
    """Join completion to the evidence transaction using the parent's exact claim."""
    from sanad.steward.apply import EffectsRejected
    from sanad.store.records import Claim, MediaWork, from_record, to_record

    if "document_claim" not in builder.command.payload:
        return
    raw = builder.command.payload["document_claim"]
    if not raw:
        raise EffectsRejected("document_claim_unavailable")
    claim = Claim.model_validate(raw)
    row = builder.store.get(
        claim.record_key.scope, "media_work", claim.record_key.sk.removeprefix("MEDIA#")
    )
    if not row or row.version != claim.version:
        raise EffectsRejected("document_claim_stale")
    work = from_record(row, MediaWork)
    if work.mime != "application/pdf" or not work.transcript_ref or work.scope != builder.scope:
        raise EffectsRejected("document_source")
    if not terminal_pages(builder.store, work):
        raise EffectsRejected("document_pages_unfinished")
    completed = MediaWork.model_validate(
        work.model_dump()
        | {
            "version": work.version + 1,
            "updated_at": builder.now,
            "state": "completed",
            "stage": "associate",
            "work_clock": None,
            "processing_claim": None,
            "association_ref": association,
            "last_error": next(
                (
                    r.body.get("rejection_reason")
                    for r in builder.puts.values()
                    if r.entity_type == "evidence"
                    and str(r.body.get("rejection_reason", "")).startswith("document_")
                ),
                None,
            ),
        }
    )
    builder.command = builder.command.model_copy(update={"work_claim": claim})
    builder.put(to_record(completed, work.scope))


def commit_pdf_intake(
    repo: ScribeRepository,
    actor: Principal,
    draft: IntakeDraft,
    retriever: MediaRetriever,
    existing: bool,
) -> CommitResult:
    from sanad.store.records import (
        AuditEvent,
        CommandEnvelope,
        CommitRequest,
        MediaWork,
        from_record,
        to_record,
    )

    row = repo.store.get(retriever.scope, "media_work", draft.id)
    assert row
    if row.body.get("state") == "completed":
        from sanad.store.records import Accepted

        return Accepted(event_ids=(), resulting_versions=())
    claim = repo.store.claim_work(
        row.scoped_key(retriever.scope),
        row.version,
        "pdf-intake",
        repo.clock(),
        retriever.policy.operations.claim_ttl,
        count_attempt=False,
        start_extraction=True,
    )
    if claim is None:
        raise MediaInvalid("work_busy")
    row = repo.store.get(retriever.scope, "media_work", draft.id)
    assert row
    parent = from_record(row, MediaWork)
    if not terminal_pages(repo.store, parent):
        retriever._conflict(parent.id, "associate", parent.version, claim)
        raise MediaInvalid("document_pages_unfinished")
    complete = parent.model_copy(
        update={
            "version": parent.version + 1,
            "updated_at": repo.clock(),
            "stage": "associate",
            "state": "completed",
            "processing_claim": None,
            "work_clock": None,
            "association_ref": "intake:" + draft.id,
            "last_error": draft.reads.first.failure_reason,
        }
    )
    id = "pdf-intake:" + draft.id
    rows = (to_record(draft, draft.scope), to_record(complete, complete.scope))
    command = CommandEnvelope(
        command_id=id,
        scope=draft.scope,
        principal=actor,
        requested_at=repo.clock(),
        work_claim=claim,
        payload={"type": "IntakeAction" if existing else "IntakeCreate", "document_final": True},
    )
    event = to_record(
        AuditEvent(
            id=id,
            event_id=id,
            command_id=id,
            scope=draft.scope,
            actor=actor,
            event_type="IntakeAction" if existing else "IntakeCreate",
            accepted_at=repo.clock(),
            created_at=repo.clock(),
            updated_at=repo.clock(),
            aggregate_refs=tuple(r.ref for r in rows),
        ),
        draft.scope,
    )
    outcome = repo.store.commit(
        CommitRequest(
            command=command,
            puts=rows,
            events=(event,),
            expected=tuple(r.ref for r in (*rows, event)),
        )
    )
    if outcome.status not in {"accepted", "duplicate"}:
        retriever._conflict(parent.id, "associate", parent.version, claim)
    return outcome


def terminal_pages(store: Store, work: MediaWork) -> bool:
    """Completion cannot conceal a missing, stale or still-running page."""
    from sanad.store.records import DocumentPageWork, from_record

    if not work.document_pages:
        return False
    for manifest in work.document_pages:
        row = store.get(work.scope, "document_page_work", manifest.work_id)
        if not row:
            return False
        page = from_record(row, DocumentPageWork)
        if (
            page.stage != "commit"
            or page.parent_id != work.id
            or page.source_hash != work.byte_hash
            or page.manifest != manifest
        ):
            return False
    return True


def final_page_checks(store: StoreBase, work: MediaWork) -> list[Check] | None:
    from sanad.store._base import Check
    from sanad.store.records import DocumentPageWork, from_record

    checks = []
    if not work.document_pages:
        return None
    for manifest in work.document_pages:
        row = store.get(work.scope, "document_page_work", manifest.work_id)
        if not row:
            return None
        page = from_record(row, DocumentPageWork)
        if (
            page.stage != "commit"
            or page.parent_id != work.id
            or page.receipt_id != work.receipt_id
            or page.manifest != manifest
            or page.source_hash != work.byte_hash
        ):
            return None
        checks.append(Check(row.key, row.version))
    return checks


def row_page_indices(
    store: Store, work: MediaWork, read: DocumentRead
) -> tuple[tuple[int, ...], ...]:
    from sanad.safety.aliases import analyte
    from sanad.store.records import DocumentPageWork, from_record

    def same_row(left: ItemRead, right: ItemRead) -> bool:
        return (
            (analyte(left.item.name or ""), left.item.value, left.item.unit)
            == (analyte(right.item.name or ""), right.item.value, right.item.unit)
            if read.first.document_type == "lab"
            else left.item == right.item
        )

    pages = []
    for manifest in work.document_pages:
        row = store.get(work.scope, "document_page_work", manifest.work_id)
        if row:
            page = from_record(row, DocumentPageWork)
            if page.reads:
                pages.append(page)
    return tuple(
        tuple(
            p.manifest.page_index
            for p in pages
            if p.reads and any(same_row(item, candidate) for item in p.reads.first.items)
        )
        for candidate in read.first.items
    )


def check_item_size(record: StoredRecord) -> None:
    from sanad.store.records import canonical_json, record_item

    if len(canonical_json(record_item(record))) + 128 > ITEM_BYTES:
        raise MediaInvalid("document_too_detailed")


def failed_document_read(
    reason: str, source: Provenance, pages: tuple[DocumentPageManifest, ...]
) -> DocumentRead:
    """An explicit failed slot has no extracted content or claimed successful model call."""
    from sanad.media.vision import DocumentRead, PrintedIdentityHint, ReaderResult
    from sanad.models.io import CallMetadata

    first = ReaderResult(
        document_type="other",
        printed_identity_hint=PrintedIdentityHint(text=None),
        printed_date=None,
        items=(),
        unreadable=True,
        notes=(),
        provenance=source,
        metadata=CallMetadata(
            model_id="document-not-read",
            policy_version="document-v1",
            latency_ms=0,
            usage_known=False,
            status="unavailable",
        ),
        status="failed",
        failure_reason=reason,
    )
    return DocumentRead(
        first=first,
        second=first,
        disagreements=(),
        blocked_pages=tuple(p.page_index for p in pages),
    )
