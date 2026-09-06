"""Explicitly configured health application; no external startup dependencies."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import FastAPI

if TYPE_CHECKING:
    from sanad.channels.telegram.settings import TelegramSettings
    from sanad.channels.transport import Transport
    from sanad.store.protocol import Store


def create_app(
    revision: str = "dev",
    *,
    telegram_settings: TelegramSettings | None = None,
    store: Store | None = None,
    clock: Callable[[], datetime] | None = None,
    transport: Transport | None = None,
    process_receipts: bool = True,
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
    app.state.telegram = runtime
    app.include_router(telegram_router(runtime, process_receipts=process_receipts))
    return app
