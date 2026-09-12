"""Account-only administrator browser adapter."""

from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from sanad.web import pages
from sanad.web.routes import AnyWebSession, LoginService, clear_cookies, require_session


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$")
    expected_version: int = Field(strict=True, ge=1)
    reason_code: Literal["admin_rejected", "unverified", "coverage"] | None = None


def admin_router(login: LoginService) -> APIRouter:
    router = APIRouter()
    guard = require_session("admin")

    @router.get("/admin")
    async def home(request: Request) -> HTMLResponse:
        try:
            await guard(request)
        except HTTPException as error:
            if error.status_code != 401:
                raise
            response = HTMLResponse(pages.admin_entry_page(), status_code=401)
            clear_cookies(response)
            return response
        return HTMLResponse(pages.admin_home())

    @router.get("/api/admin/applications")
    def applications(session: Annotated[AnyWebSession, Depends(guard)]) -> list[dict[str, object]]:
        rows = login.accounts.admin_applications(session)
        if rows is None:
            raise HTTPException(403)
        return rows

    def action(body: ActionBody, session: AnyWebSession, verb: str, target: str) -> JSONResponse:
        result = login.accounts.browser_action(
            session, verb, target, body.command_id, body.expected_version, body.reason_code
        )
        return JSONResponse(
            result.model_dump(mode="json"),
            status_code=403
            if result.status == "forbidden"
            else 409
            if result.status in {"stale_version", "already_in_state"}
            else 200,
        )

    @router.post("/api/admin/applications/{id}/approve")
    def approve(
        id: str, body: ActionBody, session: Annotated[AnyWebSession, Depends(guard)]
    ) -> JSONResponse:
        return action(body, session, "approve", id)

    @router.post("/api/admin/applications/{id}/reject")
    def reject(
        id: str, body: ActionBody, session: Annotated[AnyWebSession, Depends(guard)]
    ) -> JSONResponse:
        return action(body, session, "reject", id)

    @router.post("/api/admin/doctors/{id}/suspend")
    def suspend(
        id: str, body: ActionBody, session: Annotated[AnyWebSession, Depends(guard)]
    ) -> JSONResponse:
        return action(body, session, "suspend", id)

    @router.post("/api/admin/doctors/{id}/reinstate")
    def reinstate(
        id: str, body: ActionBody, session: Annotated[AnyWebSession, Depends(guard)]
    ) -> JSONResponse:
        return action(body, session, "reinstate", id)

    @router.post("/api/admin/logout")
    def logout(request: Request, session: Annotated[AnyWebSession, Depends(guard)]) -> JSONResponse:
        result = login.revoke_admin(session.subject, uuid4().hex, epoch=session.auth_epoch)
        if result.status != "accepted":
            raise HTTPException(403)
        response = JSONResponse({"status": "accepted"})
        clear_cookies(response)
        return response

    return router
