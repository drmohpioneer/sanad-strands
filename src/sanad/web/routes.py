"""Minimal authenticated pages and same-origin credential exchanges."""

import hmac
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import Field, ValidationError

from sanad.auth.claim import ClaimService
from sanad.auth.commands import ConfirmPatientClaim
from sanad.auth.login import LoginService, SessionIssued
from sanad.auth.tokens import token_hash
from sanad.domain import VersionRef
from sanad.domain.boundaries import _BoundaryValue
from sanad.store import keys
from sanad.store.records import Patient, WebSession
from sanad.web import pages
from sanad.web.settings import WebSettings

SESSION_COOKIE = "sanad_session"
PRE_COOKIE = "sanad_pre"
CSRF_COOKIE = "sanad_csrf"


async def form_fields(request: Request) -> dict[str, str]:
    if (
        request.headers.get("content-type", "").split(";", 1)[0]
        != "application/x-www-form-urlencoded"
    ):
        return {}
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > 8192:
            return {}
    try:
        parsed = parse_qs(body.decode("utf-8"), strict_parsing=True, max_num_fields=20)
    except (ValueError, UnicodeError):
        return {}
    return {k: v[0] for k, v in parsed.items() if len(v) == 1}


def same_origin(request: Request, settings: WebSettings) -> bool:
    # The exchange/write must originate from the configured HTTPS site.
    return (
        str(request.base_url).rstrip("/") == settings.public_base_url
        and request.headers.get("origin") == settings.public_base_url
        and request.headers.get("sec-fetch-site", "same-origin") in {"same-origin", "none"}
    )


def set_cookie(
    response: Response, name: str, value: str, seconds: int, *, httponly: bool = True
) -> None:
    response.set_cookie(
        name, value, max_age=seconds, secure=True, httponly=httponly, samesite="lax", path="/"
    )


def clear_cookies(response: Response) -> None:
    for name in (SESSION_COOKIE, PRE_COOKIE, CSRF_COOKIE):
        response.delete_cookie(
            name, path="/", secure=True, httponly=name != CSRF_COOKIE, samesite="lax"
        )


def require_session(
    role: Literal["doctor", "patient"],
) -> Callable[[Request], Awaitable[WebSession]]:
    async def guard(request: Request) -> WebSession:
        login: LoginService = request.app.state.login
        settings: WebSettings = request.app.state.web_settings
        session = login.require(request.cookies.get(SESSION_COOKIE, ""), role)
        if session is None:
            raise HTTPException(401)
        if request.method != "GET":
            fields = await form_fields(request)
            supplied = request.headers.get("x-csrf-token") or fields.get("csrf", "")
            cookie = request.cookies.get(CSRF_COOKIE, "")
            if (
                not same_origin(request, settings)
                or not supplied
                or not cookie
                or not hmac.compare_digest(supplied.encode(), cookie.encode())
                or not hmac.compare_digest(keys.digest(supplied), session.csrf_secret_ref)
            ):
                login.revoke(session)
                raise HTTPException(403)
        return session

    return guard


class ConfirmationBody(_BoundaryValue):
    command_id: Annotated[str, Field(min_length=1, max_length=160)]
    expected_versions: tuple[VersionRef, ...]


def web_router(login: LoginService, claims: ClaimService, settings: WebSettings) -> APIRouter:
    router = APIRouter()

    async def show_continue(request: Request, token: str) -> Response:
        pre = login.pre_session()
        if pre is None:
            return HTMLResponse(pages.refused_page(), status_code=503)
        response = HTMLResponse(pages.continue_page(request.url.path, pre.csrf.get_secret_value()))
        set_cookie(
            response,
            PRE_COOKIE,
            pre.cookie.get_secret_value(),
            int(login.policy.login_ttl.total_seconds()),
        )
        return response

    async def exchange(
        request: Request, token: str, role: Literal["doctor", "patient"]
    ) -> Response:
        fields = await form_fields(request)
        result = (
            login.exchange(
                role,
                token,
                request.cookies.get(PRE_COOKIE, ""),
                fields.get("csrf", ""),
                previous_cookie=request.cookies.get(SESSION_COOKIE, ""),
            )
            if same_origin(request, settings)
            else None
        )
        if not isinstance(result, SessionIssued):
            response: Response = HTMLResponse(pages.refused_page(), status_code=403)
            prior = login.session(request.cookies.get(SESSION_COOKIE, ""))
            if prior:
                login.revoke(prior)
            clear_cookies(response)
            return response
        response = RedirectResponse("/a" if role == "doctor" else "/pp", status_code=303)
        seconds = int(login.policy.absolute_ttl.total_seconds())
        set_cookie(response, SESSION_COOKIE, result.cookie.get_secret_value(), seconds)
        set_cookie(response, CSRF_COOKIE, result.csrf.get_secret_value(), seconds, httponly=False)
        response.delete_cookie(PRE_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    router.add_api_route("/d/{token}", show_continue, methods=["GET"], response_class=HTMLResponse)
    router.add_api_route("/pl/{token}", show_continue, methods=["GET"], response_class=HTMLResponse)

    @router.post("/d/{token}")
    async def doctor_exchange(request: Request, token: str) -> Response:
        return await exchange(request, token, "doctor")

    @router.post("/pl/{token}")
    async def patient_exchange(request: Request, token: str) -> Response:
        return await exchange(request, token, "patient")

    @router.get("/p/{token}")
    async def invitation(token: str) -> Response:
        digest = token_hash(token)
        inv = claims.invitation(digest) if digest else None
        doctor = claims.accounts.doctor(inv.doctor_id) if inv else None
        patient = claims.patient(inv.doctor_id, inv.patient_id) if inv else None
        if (
            inv is None
            or inv.state not in {"issued", "claimed"}
            or inv.expires_at <= login.clock()
            or doctor is None
            or doctor.status != "approved"
            or patient is None
            or patient.invitation_id != inv.id
        ):
            return HTMLResponse(pages.refused_page())
        return HTMLResponse(
            pages.invitation_page(
                doctor.name, f"https://t.me/{settings.bot_username}?start={token}"
            )
        )

    @router.get("/a")
    async def doctor_home(
        session: Annotated[WebSession, Depends(require_session("doctor"))],
    ) -> Response:
        doctor = login.accounts.doctor(session.doctor_id)
        if doctor is None:
            raise HTTPException(401)
        return HTMLResponse(pages.doctor_home(doctor.id, doctor.name))

    @router.get("/api/me")
    async def doctor_me(
        session: Annotated[WebSession, Depends(require_session("doctor"))],
    ) -> Response:
        doctor = login.accounts.doctor(session.doctor_id)
        if doctor is None:
            raise HTTPException(401)
        return JSONResponse({"doctor_id": doctor.id, "name": doctor.name, "status": doctor.status})

    def own_patient(session: WebSession) -> Patient:
        patient = claims.patient(session.doctor_id, session.patient_id or "")
        if patient is None:
            raise HTTPException(401)
        return patient

    @router.get("/pp")
    async def patient_home(
        session: Annotated[WebSession, Depends(require_session("patient"))],
    ) -> Response:
        patient = own_patient(session)
        from sanad.concierge.web import patient_page

        return HTMLResponse(patient_page(patient.display_name, patient_plan_data(session)))

    def patient_plan_data(session: WebSession) -> dict[str, object]:
        from sanad.concierge.plan import load, projection

        patient = own_patient(session)
        snapshot = load(claims.store, patient.scope, claims.clock())
        if snapshot is None:
            raise HTTPException(401)
        return dict(projection(snapshot))

    @router.get("/api/patient/plan")
    async def patient_plan(
        session: Annotated[WebSession, Depends(require_session("patient"))],
    ) -> Response:
        return JSONResponse(patient_plan_data(session))

    @router.get("/api/patient/me")
    async def patient_me(
        session: Annotated[WebSession, Depends(require_session("patient"))],
    ) -> Response:
        patient = own_patient(session)
        return JSONResponse(
            {
                "display_name": patient.display_name,
                "consent_version": session.consent_version,
                "plan": patient_plan_data(session),
            }
        )

    @router.post("/api/claims/{claim_id}/confirm")
    async def confirm_claim(
        request: Request,
        claim_id: str,
        session: Annotated[WebSession, Depends(require_session("doctor"))],
    ) -> Response:
        try:
            payload = await request.json()
            body = ConfirmationBody.model_validate(payload)
        except (ValueError, ValidationError):
            return HTMLResponse(pages.refused_page(), status_code=403)
        actor = login.store.authorize(login.scope.bot_id, session.subject).principal
        result = claims.confirm(
            ConfirmPatientClaim(
                command_id=body.command_id,
                actor=actor,
                claim_id=claim_id,
                expected_versions=body.expected_versions,
            ),
            session=session,
        )
        if result.status != "accepted":
            return HTMLResponse(pages.refused_page(), status_code=403)
        return JSONResponse({"status": "accepted"})

    return router
