"""Plain-text Telegram IO. Request-start uncertainty is never success or proven failure."""

import logging
import re
from typing import Literal

import httpx
from pydantic import JsonValue

from sanad.channels.telegram.settings import TelegramSettings
from sanad.channels.transport import CallbackOutcome, ProvablyUnsent, SendOutcome
from sanad.domain.boundaries import _BoundaryValue


class _LoggedURL(httpx.URL):
    """Keep wire components intact while making URL string/repr logging safe."""

    def __str__(self) -> str:
        return re.sub(r"/bot[^/\s]+/", "/bot<redacted>/", super().__str__())

    def __repr__(self) -> str:
        return f"URL({str(self)!r})"


class _Redact(logging.Filter):
    def __init__(self, settings: TelegramSettings):
        super().__init__()
        self.secrets = (
            settings.bot_token.get_secret_value(),
            settings.webhook_secret.get_secret_value(),
        )

    def clean(self, text: str) -> str:
        text = re.sub(r"/bot[^/\s]+/", "/bot<redacted>/", text)
        for secret in self.secrets:
            text = text.replace(secret, "<redacted>")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = self.clean(record.getMessage()), ()
        if record.exc_info:
            # Provider exception objects can retain credential-bearing request URLs.
            record.exc_info, record.exc_text = None, None
        return True


class TelegramTransport:
    def __init__(self, settings: TelegramSettings, http: httpx.Client):
        self.settings, self.http = settings, http
        self._redactor = _Redact(settings)
        self._install_redaction()
        self.http.event_hooks["request"].insert(0, self._request_hook)

    def __repr__(self) -> str:
        return "TelegramTransport(credentials=<redacted>)"

    def _install_redaction(self) -> None:
        # httpx logs the URL before its response hooks. Install at construction and
        # in the request hook, without changing the actual credential-bearing URL.
        for name in (
            "httpx",
            "httpcore",
            "httpcore.connection",
            "httpcore.http11",
            "httpcore.http2",
            "httpcore.proxy",
        ):
            logger = logging.getLogger(name)
            if self._redactor not in logger.filters:
                logger.addFilter(self._redactor)

    def _request_hook(self, request: httpx.Request) -> None:
        self._install_redaction()
        request.url = _LoggedURL(request.url)
        request.extensions["redacted_url"] = self._redactor.clean(str(request.url))

    def _call(
        self,
        method: str,
        data: dict[str, JsonValue],
        *,
        result_id: str | None = None,
        photo: bytes | None = None,
    ) -> SendOutcome:
        url = (
            f"{self.settings.api_base.rstrip('/')}/bot"
            f"{self.settings.bot_token.get_secret_value()}/{method}"
        )
        try:
            response = (
                self.http.post(url, json=data, follow_redirects=False)
                if photo is None
                else self.http.post(
                    url,
                    data={k: str(v) for k, v in data.items()},
                    files={"photo": ("invitation.png", photo, "image/png")},
                    follow_redirects=False,
                )
            )
        except httpx.ConnectError:
            raise ProvablyUnsent("telegram_connect_before_write") from None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError):
            return SendOutcome(status="uncertain", code="telegram_response_unknown")
        except Exception:
            # Includes client hooks/decoders: without a before-write guarantee, fail uncertain.
            return SendOutcome(status="uncertain", code="telegram_client_error")
        try:
            body = response.json()
        except (ValueError, UnicodeError):
            body = None
        if 200 <= response.status_code < 300:
            if isinstance(body, dict) and body.get("ok") is True:
                result = body.get("result")
                if result_id is not None and result is True:
                    return SendOutcome(status="accepted", provider_message_id=result_id)
                if isinstance(result, dict) and type(result.get("message_id")) is int:
                    return SendOutcome(
                        status="accepted", provider_message_id=str(result["message_id"])
                    )
            return SendOutcome(status="uncertain", code="telegram_unparseable_success")
        if response.status_code == 429:
            retry = None
            if isinstance(body, dict) and isinstance(body.get("parameters"), dict):
                value = body["parameters"].get("retry_after")
                if type(value) is int and value >= 0:
                    retry = value
            return SendOutcome(
                status="failed",
                retryable=True,
                code="telegram_rate_limited",
                retry_after_seconds=retry,
            )
        if response.status_code >= 500:
            return SendOutcome(status="failed", retryable=True, code="telegram_server_error")
        code = f"telegram_http_{response.status_code}"
        # Fixed categories exclude provider-echoed payloads and credentials entirely.
        if isinstance(body, dict) and isinstance(body.get("description"), str):
            description = body["description"].lower()
            for phrase, label in (
                ("chat not found", "chat_not_found"),
                ("bot was blocked by the user", "blocked"),
                ("user is deactivated", "deactivated"),
            ):
                if phrase in description:
                    code = "telegram_" + label
                    break
        return SendOutcome(status="failed", code=code)

    def send(self, recipient_ref: str, payload: dict[str, JsonValue]) -> SendOutcome:
        if isinstance(payload.get("qr_payload"), str):
            from sanad.scribe.qr import render_png

            link, caption = str(payload["qr_payload"]), payload.get("text")
            if not isinstance(caption, str) or len(caption) > 1024:
                raise ProvablyUnsent("telegram_photo_caption_required")
            try:
                photo = render_png(link)
            except Exception:
                raise ProvablyUnsent("qr_render_failed") from None
            return self._call(
                "sendPhoto", {"chat_id": recipient_ref, "caption": caption}, photo=photo
            )
        text = payload.get("text")
        if not isinstance(text, str) or not text or len(text) > 4096:
            raise ProvablyUnsent("telegram_plain_text_required")
        data: dict[str, JsonValue] = {"chat_id": recipient_ref, "text": text}
        if "reply_markup" in payload:
            data["reply_markup"] = payload["reply_markup"]
        return self._call("sendMessage", data)

    def answer_callback(self, callback_query_id: str, text: str) -> CallbackOutcome:
        try:
            outcome = self._call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": text,
                },
                result_id=callback_query_id,
            )
        except ProvablyUnsent:
            return CallbackOutcome(status="failed", code="telegram_connect_before_write")
        return CallbackOutcome(status=outcome.status, code=outcome.code)


class WebhookRegistration(_BoundaryValue):
    status: Literal["registered", "failed", "uncertain"]
    code: str | None = None


def register_webhook(
    settings: TelegramSettings,
    public_url: str,
    *,
    http: httpx.Client | None = None,
) -> WebhookRegistration:
    """Explicit ops-only action; never called by startup, tests or Make targets."""
    url = httpx.URL(public_url)
    if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
        raise ValueError("webhook requires a clean public HTTPS URL")
    own_client = http is None
    client = http if http is not None else httpx.Client(trust_env=False, timeout=15)
    try:
        adapter = TelegramTransport(settings, client)
        try:
            outcome = adapter._call(
                "setWebhook",
                {
                    "url": public_url,
                    "secret_token": settings.webhook_secret.get_secret_value(),
                    "allowed_updates": ["message", "callback_query"],
                    "drop_pending_updates": False,
                },
                result_id="webhook",
            )
        except ProvablyUnsent:
            return WebhookRegistration(status="failed", code="telegram_connect_before_write")
        return WebhookRegistration(
            status="registered" if outcome.status == "accepted" else outcome.status,
            code=outcome.code,
        )
    finally:
        if own_client:
            client.close()
