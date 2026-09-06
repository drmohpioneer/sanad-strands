"""Authenticated internal HTTP boundaries, explicitly wired by the app factory."""

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from sanad.channels.telegram.router import TelegramRuntime, route_receipt
from sanad.ops.nonce_store import TickVerifier
from sanad.ops.worker import worker_body
from sanad.store.keys import ScopedKey

logger = logging.getLogger(__name__)


def process_event(runtime: TelegramRuntime, event: dict[str, Any]) -> dict[str, str]:
    if event.get("type") != "process_receipt":
        raise ValueError("unsupported worker event")
    key = ScopedKey.model_validate(event["receipt"])
    result = route_receipt(runtime, key, owner="lambda-worker")
    runtime.count("route_" + result.route)
    logger.info("receipt worker completed status=%s", result.status)
    return {"route": result.route, "status": result.status}


def internal_router(
    verifier: TickVerifier,
    sweep: Callable[[], dict[str, Any]],
    runtime: TelegramRuntime,
) -> APIRouter:
    router = APIRouter()

    @router.post("/internal/tick")
    async def tick(request: Request) -> Any:
        body = await request.body()
        if len(body) > 4096:
            return Response(status_code=401)
        status = await run_in_threadpool(verifier.verify, request.headers, body)
        if status != 200:
            return Response(status_code=status)
        nonce = request.headers["x-sanad-nonce"]
        logger.info("tick accepted nonce=%s", nonce)
        return {"accepted": True, "nonce": nonce, "sweep": await run_in_threadpool(sweep)}

    @router.post("/events")
    async def worker(request: Request) -> Any:
        try:
            body = await request.body()
            if len(body) > 16384:
                return Response(status_code=401)
            import json

            event = json.loads(body)
            payload = worker_body(event)
            status = await run_in_threadpool(verifier.verify, event["headers"], payload)
            if status != 200:
                return Response(status_code=status)
            return await run_in_threadpool(process_event, runtime, event)
        except (ValueError, KeyError, TypeError, AttributeError):
            return Response(status_code=400)

    return router
