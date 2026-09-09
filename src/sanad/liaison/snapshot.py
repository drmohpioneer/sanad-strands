"""Strong review/source reads; scope comes from the record, never its kind name."""

from sanad.domain import ReviewObligation
from sanad.liaison.records import ReviewSnapshot
from sanad.store.protocol import Store
from sanad.store.records import StoredRecord, model_scope, to_record


def source_row(store: Store, review: ReviewObligation) -> StoredRecord | None:
    kind = "intake_draft" if review.source_type == "intake" else review.source_type
    if kind == "slot" and review.source_mission_id:
        return store.get(model_scope(review), "mission", review.source_mission_id)
    scope = model_scope(review)
    # Evidence versions have a composite lookup ID; other sources use their exact ID.
    id = f"{review.source_id}:{review.source_version}" if kind == "evidence" else review.source_id
    return store.get(scope, kind, id)


def snapshot(store: Store, review: ReviewObligation) -> ReviewSnapshot:
    source = source_row(store, review)
    scope = model_scope(review)
    from sanad.domain import TenantScope

    assert isinstance(scope, TenantScope)
    return ReviewSnapshot(
        scope=scope,
        review_ref=to_record(review, model_scope(review)).ref,
        source_type=review.source_type,
        source_id=review.source_id,
        source_version=review.source_version,
        material_version=review.last_material_change_version,
        source_ref=source.ref if source else None,
    )
