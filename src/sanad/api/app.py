"""Explicitly configured health application; no external startup dependencies."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

if TYPE_CHECKING:
    from sanad.auth.commands import ConsentPolicy
    from sanad.channels.telegram.settings import TelegramSettings
    from sanad.channels.transport import Transport
    from sanad.media.storage import UploadStorage
    from sanad.ops.nonce_store import TickVerifier
    from sanad.store.keys import ScopedKey
    from sanad.store.protocol import Store
    from sanad.web.settings import WebSettings


def create_app(
    revision: str = "dev",
    *,
    telegram_settings: TelegramSettings | None = None,
    store: Store | None = None,
    clock: Callable[[], datetime] | None = None,
    transport: Transport | None = None,
    process_receipts: bool = True,
    synthetic: bool = False,
    web_settings: WebSettings | None = None,
    consent_policy: Callable[[str], ConsentPolicy | None] | None = None,
    receipt_submit: Callable[[ScopedKey], None] | None = None,
    upload_storage: UploadStorage | None = None,
    tick_verifier: TickVerifier | None = None,
    tick_sweep: Callable[[], dict[str, Any]] | None = None,
) -> FastAPI:
    """Build an independent application without reading runtime configuration."""
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, bool | str]:
        return {"ok": True, "service": "sanad", "revision": revision}

    from sanad.channels.telegram.webhook import telegram_router

    runtime = None
    if telegram_settings is not None and store is not None:
        from sanad.channels.telegram.router import TelegramRuntime
        from sanad.channels.telegram.transport import TelegramTransport
        from sanad.store._base import utc_now

        # The transport is explicitly injected. Startup never contacts Telegram.
        if transport is None:
            from collections.abc import AsyncIterator
            from contextlib import asynccontextmanager

            import httpx

            client = httpx.Client(trust_env=False, timeout=15)
            transport = TelegramTransport(telegram_settings, client)

            @asynccontextmanager
            async def lifespan(app: FastAPI) -> AsyncIterator[None]:
                try:
                    yield
                finally:
                    client.close()

            app.router.lifespan_context = lifespan
        runtime = TelegramRuntime(telegram_settings, store, clock or utc_now, transport)
    if web_settings is not None and runtime is not None:
        from fastapi import HTTPException, Request
        from fastapi.responses import HTMLResponse

        from sanad.auth.claim import ClaimService
        from sanad.auth.integration import claim_lane, credential_freshness
        from sanad.auth.login import LoginService
        from sanad.auth.telegram import IdentityRouting
        from sanad.web import pages
        from sanad.web.routes import clear_cookies, web_router
        from sanad.web.security import BrowserSecurity, install_redaction

        login = LoginService(runtime.accounts, web_settings.public_base_url)
        claims = ClaimService(runtime.accounts, web_settings.public_base_url)
        if consent_policy is not None:
            claims.consent_policy = consent_policy
        runtime.identity_route = IdentityRouting(runtime, login, claims)
        runtime.dispatcher.extra_freshness = lambda intent, now: credential_freshness(
            login, intent, now
        )
        app.state.login, app.state.claims, app.state.web_settings = login, claims, web_settings
        from sanad.concierge.turn import ConciergeTurn
        from sanad.scribe.turn import ScribeTurn

        app.state.concierge = ConciergeTurn(runtime, synthetic=synthetic)
        runtime.concierge_route = app.state.concierge
        app.state.scribe = ScribeTurn(runtime, claims)
        runtime.scribe_route = app.state.scribe
        from sanad.scribe.web import scribe_router

        app.include_router(scribe_router(claims))
        app.state.claim_lane = lambda row: claim_lane(claims, row)
        app.include_router(web_router(login, claims, web_settings))
        if upload_storage is not None:
            from sanad.media.upload import UploadIngress
            from sanad.web.routes import upload_router

            app.state.uploads = UploadIngress(
                runtime, login, upload_storage, receipt_submit if process_receipts else None
            )
            app.include_router(upload_router(app.state.uploads, web_settings))
        install_redaction()
        app.add_middleware(BrowserSecurity)

        @app.exception_handler(HTTPException)
        async def browser_error(request: Request, error: HTTPException) -> HTMLResponse:
            response = HTMLResponse(pages.refused_page(), status_code=error.status_code)
            if error.status_code in {401, 403}:
                clear_cookies(response)
            return response

    app.state.telegram = runtime
    app.include_router(
        telegram_router(runtime, process_receipts=process_receipts, receipt_submit=receipt_submit)
    )
    if tick_verifier is not None and tick_sweep is not None and runtime is not None:
        from sanad.api.internal import internal_router

        app.include_router(internal_router(tick_verifier, tick_sweep, runtime))
    return app
