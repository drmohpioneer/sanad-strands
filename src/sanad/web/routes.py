"""Minimal authenticated pages and same-origin credential exchanges."""

import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import Field, ValidationError

from sanad.api.failures import RequestFailure, store_busy
from sanad.auth.claim import ClaimService
from sanad.auth.commands import ConfirmPatientClaim, ExchangeRefused
from sanad.auth.login import LoginService as LoginService
from sanad.auth.login import SessionIssued
from sanad.auth.tokens import token_hash
from sanad.domain import VersionRef
from sanad.domain.boundaries import _BoundaryValue
from sanad.store import keys
from sanad.store.records import AnyWebSession as AnyWebSession
from sanad.store.records import InboundReceipt, Patient, WebSession
from sanad.web import pages
from sanad.web.security import SessionRefused
from sanad.web.settings import WebSettings

if TYPE_CHECKING:
    from sanad.media.upload import UploadIngress


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
    role: Literal["doctor", "patient", "admin"],
) -> Callable[[Request], Awaitable[AnyWebSession]]:
    async def guard(request: Request) -> AnyWebSession:
        login: LoginService = request.app.state.login
        settings: WebSettings = request.app.state.web_settings
        from sanad.store.records import AuthorizationUnavailable

        try:
            session = login.require(
                request.cookies.get(SESSION_COOKIE, ""), role, path=request.url.path
            )
        except AuthorizationUnavailable:
            raise RequestFailure("authorization_unavailable") from None
        if session is None:
            if role == "admin":
                raise HTTPException(401)
            previous = login.session(request.cookies.get(SESSION_COOKIE, ""))
            changed_elsewhere = (
                previous is not None
                and previous.revocation_reason == "binding_consent_or_authority"
            )
            reason = (
                "patient_access_changed"
                if role == "patient" and changed_elsewhere
                else "patient_sign_in"
                if role == "patient"
                else "signed_out"
            )
            raise SessionRefused(reason)
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
                login.revoke(session, reason="csrf", path=request.url.path)
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
            raise RequestFailure("authorization_unavailable")
        response = HTMLResponse(pages.continue_page(request.url.path, pre.csrf.get_secret_value()))
        set_cookie(
            response,
            PRE_COOKIE,
            pre.cookie.get_secret_value(),
            int(login.policy.login_ttl.total_seconds()),
        )
        return response

    async def exchange(
        request: Request, token: str, role: Literal["doctor", "patient", "admin"]
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
            else ExchangeRefused(status="origin")
        )
        if not isinstance(result, SessionIssued):
            logging.getLogger(__name__).info("login_refused role=%s reason=%s", role, result.status)
            response: Response = HTMLResponse(pages.refused_page(result.status), status_code=403)
            prior = login.session(request.cookies.get(SESSION_COOKIE, ""))
            if prior:
                login.revoke(prior, reason="exchange_refused", path="auth/exchange")
            clear_cookies(response)
            return response
        response = RedirectResponse(
            (
                result.destination + "#remove"
                if result.destination
                else "/admin"
                if role == "admin"
                else "/a"
                if role == "doctor"
                else "/pp"
            ),
            status_code=303,
        )
        seconds = int(login.policy.absolute_ttl.total_seconds())
        set_cookie(response, SESSION_COOKIE, result.cookie.get_secret_value(), seconds)
        set_cookie(response, CSRF_COOKIE, result.csrf.get_secret_value(), seconds, httponly=False)
        response.delete_cookie(PRE_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    router.add_api_route("/d/{token}", show_continue, methods=["GET"], response_class=HTMLResponse)
    router.add_api_route("/pl/{token}", show_continue, methods=["GET"], response_class=HTMLResponse)

    router.add_api_route("/ad/{token}", show_continue, methods=["GET"], response_class=HTMLResponse)

    @router.post("/ad/{token}")
    async def admin_exchange(request: Request, token: str) -> Response:
        return await exchange(request, token, "admin")

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
        from sanad.web.browser import surface

        return HTMLResponse(surface(doctor.name, doctor.language))

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
        from sanad.web.browser import surface

        return HTMLResponse(
            surface(
                patient.display_name,
                patient.language,
                audience="patient",
                patient_plan=patient_plan_data(session),
            )
        )

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

    # Contract 18: additive route registration, independent of inbox/correction verbs.
    from sanad.web.api_admin import admin_router
    from sanad.web.browser import browser_router
    from sanad.web.preferences import preference_router
    from sanad.web.reviews import review_router

    router.include_router(admin_router(login))
    router.include_router(browser_router(login, claims))
    router.include_router(preference_router(login))
    from sanad.web.api_patient import patient_router

    router.include_router(patient_router(login))
    router.include_router(review_router(claims))
    from sanad.web.api_questions import question_router

    router.include_router(question_router(claims))
    from sanad.web.api_actions import action_router

    router.include_router(action_router(claims))
    from sanad.web.api_removal import removal_router

    router.include_router(removal_router(claims))
    return router


def upload_router(ingress: "UploadIngress", settings: WebSettings) -> APIRouter:
    """Raw image bodies avoid multipart spooling before authorization."""
    import asyncio
    import base64
    import binascii

    from starlette.concurrency import run_in_threadpool
    from starlette.requests import ClientDisconnect

    from sanad.media.limits import MAX_DOCUMENT_BYTES, MAX_IMAGE_BYTES, MediaInvalid
    from sanad.media.upload import new_upload_id
    from sanad.safety import screen_text

    router = APIRouter()

    @router.post("/api/patient/uploads")
    async def upload(request: Request) -> Response:
        cookie = request.cookies.get(SESSION_COOKIE, "")
        from sanad.store.records import AuthorizationUnavailable

        try:
            session = await run_in_threadpool(
                ingress.login.require, cookie, "patient", path=request.url.path
            )
        except AuthorizationUnavailable:
            raise RequestFailure("authorization_unavailable") from None
        if session is None:
            previous = ingress.login.session(cookie)
            raise SessionRefused(
                "patient_access_changed"
                if previous and previous.revocation_reason == "binding_consent_or_authority"
                else "patient_sign_in"
            )
        csrf, csrf_cookie = (
            request.headers.get("x-csrf-token", ""),
            request.cookies.get(CSRF_COOKIE, ""),
        )
        if (
            not same_origin(request, settings)
            or not csrf
            or not csrf_cookie
            or not hmac.compare_digest(csrf.encode(), csrf_cookie.encode())
            or not hmac.compare_digest(keys.digest(csrf), session.csrf_secret_ref)
        ):
            raise HTTPException(403)
        # UTF-8 captions travel as base64 in a header, never a URL/access log.
        encoded = request.headers.get("x-upload-caption", "")
        if len(encoded) > 8192:
            raise HTTPException(400)
        try:
            caption = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error):
            raise HTTPException(400) from None
        if len(caption) > 4096:
            raise HTTPException(400)
        verdict = screen_text(caption, policy=ingress.runtime.safety_policy)
        receipt = await run_in_threadpool(
            ingress.receipt, session, caption, verdict, new_upload_id()
        )
        body = bytearray()
        try:
            # No browser-provided scope, kind, subject, receipt id or staged handle is accepted.
            if request.query_params:
                raise MediaInvalid("unsupported_parameter")
            declared = request.headers.get("content-type", "").split(";", 1)[0].lower()
            if declared != "application/pdf" and not declared.startswith("image/"):
                raise MediaInvalid("unsupported_type")
            async with asyncio.timeout(30):
                async for part in request.stream():
                    cap = MAX_DOCUMENT_BYTES if declared == "application/pdf" else MAX_IMAGE_BYTES
                    if len(body) + len(part) > cap:
                        raise MediaInvalid(
                            "document_too_large" if declared == "application/pdf" else "too_large"
                        )
                    body.extend(part)
            # Receipt timing belongs to the complete image, not its first HTTP byte.
            received_at = ingress.runtime.clock()
            assert receipt.work_clock is not None
            receipt = InboundReceipt.model_validate(
                receipt.model_dump()
                | {
                    "received_at": received_at,
                    "created_at": received_at,
                    "updated_at": received_at,
                    "work_clock": receipt.work_clock.model_copy(
                        update={"next_action_at": received_at}
                    ),
                }
            )
            current = await run_in_threadpool(ingress.login.require, cookie, "patient")
            if current is None or current.id != session.id:
                raise PermissionError("upload_session_changed")
            stage = await run_in_threadpool(ingress.stage, current, receipt, bytes(body), declared)
            saved = await run_in_threadpool(ingress.accept, stage)
        except (MediaInvalid, ClientDisconnect, TimeoutError, PermissionError) as error:
            await run_in_threadpool(ingress.rejected_caption, receipt, verdict)
            status = (
                403
                if isinstance(error, PermissionError)
                else 413
                if str(error) in {"too_large", "document_too_large"}
                else 400
            )
            from sanad.media.upload import rejection_category

            return JSONResponse(
                {"status": "not_received", "category": rejection_category(str(error))},
                status_code=status,
            )
        except AuthorizationUnavailable:
            raise RequestFailure("authorization_unavailable") from None
        except Exception as error:
            if store_busy(error):
                raise RequestFailure("store_busy") from None
            # Reserved stages retain their clock; outages never receive a saved ACK.
            raise RequestFailure("media_storage_unavailable") from None
        await run_in_threadpool(ingress.danger, receipt, verdict)
        await run_in_threadpool(ingress.handoff, saved)
        return JSONResponse(
            {
                "status": "received",
                "handle": stage.receipt.provider_media_handle,
                "receipt_id": receipt.id,
            },
            status_code=202,
        )

    return router
