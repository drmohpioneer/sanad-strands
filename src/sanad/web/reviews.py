"""Read-only ReviewObligation projection over the accepted doctor review index.

Contract 18 binding addendum 1 releases this list independently of slice 17's verbs.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import JsonValue

from sanad.auth.claim import ClaimService
from sanad.domain import TenantScope
from sanad.steward.types import records
from sanad.store.keys import IntakeScope
from sanad.store.records import WebSession
from sanad.web.routes import require_session


def review_router(claims: ClaimService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/browser/reviews")
    def reviews(
        session: Annotated[WebSession, Depends(require_session("doctor"))], history: bool = False
    ) -> object:
        scope = TenantScope(doctor_id=session.doctor_id)
        if history:
            # Resolved reviews leave GSI_REVIEW. Reuse the accepted owned-intake
            # listing and scoped REVIEW reads, never a global scan or new index.
            owned = list(records(claims.store, scope, "review"))
            for draft in records(claims.store, scope, "intake_draft"):
                if draft.body.get("owner_doctor_id") != scope.doctor_id:
                    continue
                intake = IntakeScope(doctor_id=scope.doctor_id, intake_id=draft.id)
                owned.extend(records(claims.store, intake, "review"))
            return [
                row.body
                for row in owned
                if row.body.get("owner_doctor_id") == scope.doctor_id
                and row.body.get("patient_id") is None
                and row.body.get("state") == "resolved"
            ]
        cursor = None
        values: list[dict[str, JsonValue]] = []
        while True:
            page, cursor = claims.store.list_reviews(scope, cursor, limit=100)
            values.extend(
                row.body for row in page if row.body.get("owner_doctor_id") == scope.doctor_id
            )
            if cursor is None:
                return values

    return router
