"""AWS composition root and Lambda Web Adapter async event entry.

Health/readiness needs only the revision. Configuration is loaded once on the
first non-health request, after deploy has written the function URL into SSM.
No environment file is read, no webhook is registered and no provider is called
at import. The public event path requires the same stored service replay guard.
"""

import logging
import os
import threading
from time import monotonic

import boto3  # type: ignore[import-untyped]
import httpx
from botocore.config import Config  # type: ignore[import-untyped]
from fastapi import FastAPI
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from sanad.api.app import create_app
from sanad.api.internal import process_event as process_event
from sanad.channels.telegram.settings import TelegramSettings
from sanad.media.audio import FFmpegConverter
from sanad.media.storage import S3MediaStore
from sanad.models.registry import ModelRegistry
from sanad.models.timeouts import (
    PROVIDER_CONNECT_TIMEOUT,
    SPEECH_READ_TIMEOUT,
    TRANSCRIPTION_TIMEOUT,
)
from sanad.ops.nonce_store import NonceStore, TickVerifier
from sanad.ops.sweep import sweep_due
from sanad.ops.worker import AsyncReceiptInvoker
from sanad.store._base import utc_now
from sanad.store.dynamodb import DynamoStore
from sanad.web.settings import WebSettings

logger = logging.getLogger(__name__)
_audio_converter = FFmpegConverter()
_warm_started = monotonic()
_audio_converter.warm()
logger.info("ffmpeg_warm_ms=%d", round((monotonic() - _warm_started) * 1000))


class MetadataOnlyErrors(logging.Filter):
    """Provider/validation exceptions can retain request bodies or credentials."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            record.msg = "application request failed; details withheld from logs"
            record.args = ()
            record.exc_info = record.exc_text = None
        return True


def configure(revision: str) -> FastAPI:
    config = Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})
    prefix = os.environ["SANAD_SSM_PREFIX"]
    ssm = boto3.client("ssm", config=config)
    names = (
        "bot-token",
        "webhook-secret",
        "tick-secret",
        "admin-telegram-id",
        "public-base-url",
        "bot-username",
    )
    response = ssm.get_parameters(Names=[prefix + n for n in names], WithDecryption=True)
    values = {p["Name"].removeprefix(prefix): p["Value"] for p in response["Parameters"]}
    if set(values) != set(names) or any(not v for v in values.values()):
        raise ValueError("SSM configuration incomplete")
    settings = TelegramSettings(
        bot_id=values["bot-token"].split(":", 1)[0],
        bot_token=SecretStr(values["bot-token"]),
        webhook_secret=SecretStr(values["webhook-secret"]),
        admin_user_id=values["admin-telegram-id"],
    )
    store = DynamoStore(boto3.client("dynamodb", config=config), os.environ["SANAD_TABLE"])
    invoker = AsyncReceiptInvoker(
        boto3.client("lambda", config=config),
        os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        values["tick-secret"],
    )
    verifier = TickVerifier(
        values["tick-secret"], NonceStore(store, "tick:" + os.environ["SANAD_ENV"]), utc_now
    )
    app = create_app(
        revision,
        synthetic=os.environ.get("SANAD_ENV") in {"dev", "synthetic", "test", "judge"},
        telegram_settings=settings,
        store=store,
        receipt_submit=invoker,
        web_settings=WebSettings(
            public_base_url=values["public-base-url"], bot_username=values["bot-username"]
        ),
        tick_verifier=verifier,
        tick_sweep=lambda: sweep_due(app.state.telegram, store, app.state.claim_lane),
    )
    app.state.model_registry = ModelRegistry()
    app.state.media_store = S3MediaStore(
        os.environ["SANAD_BUCKET"], boto3.client("s3", config=config)
    )
    from sanad.channels.telegram.transport import TelegramTransport
    from sanad.domain import DRAFT_POLICY_2026_09, Provenance
    from sanad.media.retrieve import MediaRetriever
    from sanad.media.speech import SpeechAdapter
    from sanad.media.telegram import TelegramFileClient
    from sanad.media.vision import VisionAdapter
    from sanad.models.io import CALL_TIMEOUT, BedrockCaller
    from sanad.scribe.turn import ScribeTurn
    from sanad.steward.types import StewardPolicy
    from sanad.store.keys import IntakeScope, digest

    runtime = app.state.telegram
    runtime.dispatcher.media_store = app.state.media_store
    scribe: ScribeTurn = app.state.scribe
    scribe.rxnorm_client = httpx.Client(timeout=3, follow_redirects=False, trust_env=False)

    def speech(
        source: Provenance,
        read_timeout: float = SPEECH_READ_TIMEOUT,
        timeout: float = TRANSCRIPTION_TIMEOUT,
    ) -> SpeechAdapter:
        caller = BedrockCaller(
            boto3.client(
                "bedrock-runtime",
                region_name=app.state.model_registry.region,
                config=Config(
                    connect_timeout=PROVIDER_CONNECT_TIMEOUT,
                    read_timeout=read_timeout,
                    retries={"total_max_attempts": 1},
                ),
            ),
            runtime.safety_policy.policy_version,
            timeout=timeout,
        )
        return SpeechAdapter(caller, _audio_converter, source, app.state.model_registry)

    scribe.vision_factory = lambda source: VisionAdapter(
        speech(source, read_timeout=22, timeout=CALL_TIMEOUT).caller,
        source,
        runtime.safety_policy,
        app.state.model_registry,
    )
    scribe.speech_factory = speech
    app.state.concierge.speech_factory = speech
    app.state.concierge.vision_factory = scribe.vision_factory
    if isinstance(runtime.transport, TelegramTransport):
        scribe.media_factory = lambda receipt, principal: MediaRetriever(
            runtime.steward,
            app.state.media_store,
            TelegramFileClient(runtime.transport),
            _audio_converter,
            IntakeScope(doctor_id=principal.doctor_id or "", intake_id=digest(receipt.id)),
            principal,
            lambda: app.state.claims.doctor(principal) is not None,
            StewardPolicy(DRAFT_POLICY_2026_09),
        )
        from sanad.domain import PatientScope

        app.state.concierge.media_factory = lambda receipt, principal: MediaRetriever(
            runtime.steward,
            app.state.media_store,
            TelegramFileClient(runtime.transport),
            _audio_converter,
            PatientScope(
                doctor_id=principal.doctor_id or "", patient_id=principal.patient_id or ""
            ),
            principal,
            lambda: bool(
                (auth := store.authorize(settings.bot_id, principal.subject)).binding
                and app.state.concierge.valid(principal, auth.binding)
            ),
            StewardPolicy(DRAFT_POLICY_2026_09),
        )
    return app


def create_runtime_app() -> ASGIApp:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("uvicorn.error").addFilter(MetadataOnlyErrors())
    # HTTPX's default request logger contains single-use login URLs in send bodies only
    # at DEBUG; the accepted transport also redacts Telegram token-bearing URLs.
    for name in ("httpx", "httpcore", "boto3", "botocore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    revision = os.environ["SANAD_REVISION"]
    health = create_app(revision)
    configured: FastAPI | None = None
    lock = threading.Lock()

    def load() -> FastAPI:
        nonlocal configured
        with lock:
            if configured is None:
                configured = configure(revision)
            return configured

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/health":
            await health(scope, receive, send)
            return
        try:
            app = await run_in_threadpool(load)
        except Exception:
            logger.error("runtime configuration unavailable")
            await Response(status_code=503)(scope, receive, send)
            return
        await app(scope, receive, send)

    return application
