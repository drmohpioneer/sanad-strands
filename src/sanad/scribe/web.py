"""Minimal session-authorized doctor record reads."""

from typing import Annotated

from fastapi import APIRouter, Depends

from sanad.auth.claim import ClaimService
from sanad.domain import TenantScope
from sanad.scribe.patients import panel
from sanad.store.records import WebSession
from sanad.web.routes import require_session


def scribe_router(claims: ClaimService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/patients")
    def patients(session: Annotated[WebSession, Depends(require_session("doctor"))]) -> object:
        return [
            {
                "patient_id": p.id,
                "display_name": p.display_name,
                "contact_status": p.contact_status,
                "record_version": p.record_version,
                "updated_at": p.updated_at.isoformat(),
            }
            for p in panel(claims.store, TenantScope(doctor_id=session.doctor_id))
        ]

    from sanad.web.api_record import record_router

    router.include_router(record_router(claims))
    return router
