"""Resolve a private instruction crop only through its current owning proposal."""

from sanad.domain import TenantScope
from sanad.media.limits import MAX_IMAGE_BYTES, image_info
from sanad.media.storage import MediaStore
from sanad.scribe.crosscheck import column_caption
from sanad.scribe.proposal import Proposal
from sanad.store.keys import IntakeScope
from sanad.store.protocol import Store
from sanad.store.records import OutboundIntent, from_record


def load_crop(store: Store, media: MediaStore, intent: OutboundIntent) -> bytes:
    if intent.template_id != "scribe_photo_column" or not isinstance(intent.scope, IntakeScope):
        raise ValueError("photo_scope")
    if (
        len(intent.source_versions) != 1
        or intent.source_versions[0].entity_type != "scribe_proposal"
    ):
        raise ValueError("photo_source")
    ref = intent.source_versions[0]
    scope = TenantScope(doctor_id=intent.scope.doctor_id)
    row = store.get(scope, "scribe_proposal", ref.id)
    if row is None or row.version != ref.version:
        raise ValueError("photo_source")
    proposal = from_record(row, Proposal)
    crop = proposal.photo.reads.instruction_crop if proposal.photo else None
    if (
        not proposal.photo
        or not crop
        # The caption was rendered when queued, possibly before a /lang switch.
        # Accept only the two exact released captions and the same private blob.
        or not any(
            intent.payload
            == {
                "text": column_caption(language),
                "photo_blob_ref": crop.blob_ref,
            }
            for language in ("ar", "en")
        )
    ):
        raise ValueError("photo_reference")
    data = media.get(
        IntakeScope(doctor_id=scope.doctor_id, intake_id=proposal.photo.intake_id),
        crop.blob_ref,
        MAX_IMAGE_BYTES,
    )
    image_info(data)
    return data
