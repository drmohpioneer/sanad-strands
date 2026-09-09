"""Exchange-path redaction and restrictive browser response headers."""

import logging
import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_PATH = re.compile(r"/(d|pl|p)/[^\s?\#\"'<>]+")
_START = re.compile(r"([?&]start=)[^\s&#\"'<>]+")
HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "style-src 'self'; font-src 'self'; script-src 'self'; img-src 'self' data:; "
        "connect-src 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
}


def redact(text: str) -> str:
    return _START.sub(r"\1<redacted>", _PATH.sub(r"/\1/<redacted>", text))


class ExchangeRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg, record.args = redact(message), ()
        if record.msg != message and record.exc_info:
            record.exc_info, record.exc_text = None, None
        return True


_REDACTOR = ExchangeRedactor()


def install_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error", "httpx", "sanad.web"):
        logger = logging.getLogger(name)
        if _REDACTOR not in logger.filters:
            logger.addFilter(_REDACTOR)


class BrowserSecurity:
    def __init__(self, app: ASGIApp):
        self.app = app
        install_redaction()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        browser = path not in {"/health", "/tg"}

        async def guarded_send(message: Message) -> None:
            if message["type"] == "http.response.start" and browser:
                message["headers"] = [
                    *message.get("headers", []),
                    *((k.lower().encode(), v.encode()) for k, v in HEADERS.items()),
                ]
                logging.getLogger("sanad.web").info(
                    "%s %s %s", scope.get("method"), redact(path), message["status"]
                )
            await send(message)

        await self.app(scope, receive, guarded_send)
