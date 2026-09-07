"""Bounded provider calls and an explicit metadata-only observation boundary."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal, Protocol

from pydantic import Field

from sanad.domain.boundaries import NonnegativeInt, _BoundaryValue
from sanad.models.timeouts import TRANSCRIPTION_TIMEOUT

CALL_TIMEOUT = 25.0


class CallMetadata(_BoundaryValue):
    model_id: str
    policy_version: str
    latency_ms: float = Field(ge=0)
    input_tokens: NonnegativeInt = 0
    output_tokens: NonnegativeInt = 0
    usage_known: bool = True
    status: Literal["ok", "unavailable", "timeout", "invalid"] = "ok"


class ModelUnavailable(_BoundaryValue):
    reason: Literal["unavailable", "timeout", "budget_exhausted"]
    route: Literal["media_failure"] = "media_failure"
    metadata: tuple[CallMetadata, ...] = ()


class ModelReply(_BoundaryValue):
    text: str = Field(repr=False)
    metadata: CallMetadata


class ConverseClient(Protocol):
    def converse(self, **kwargs: Any) -> dict[str, Any]: ...


class ModelCaller(Protocol):
    async def call(
        self, model_id: str, content: list[dict[str, Any]], *, max_tokens: int = 2048
    ) -> ModelReply | ModelUnavailable: ...


class _PrivateProviderLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # SDK debug/error messages can contain prompts, tool arguments and AWS URLs.
        # Application metadata is recorded through CallMetadata, never provider logs.
        return False


def private_provider_logs() -> None:
    names = ("strands", "boto3", "botocore", "urllib3")
    for name in tuple(logging.Logger.manager.loggerDict):
        if name.startswith(names):
            logger = logging.getLogger(name)
            if not any(isinstance(f, _PrivateProviderLogs) for f in logger.filters):
                logger.addFilter(_PrivateProviderLogs())


@dataclass
class BedrockCaller:
    client: ConverseClient = field(repr=False)
    policy_version: str
    observe: Callable[[CallMetadata], None] = lambda metadata: None
    timeout: float = CALL_TIMEOUT

    def __post_init__(self) -> None:
        if not 0 < self.timeout <= TRANSCRIPTION_TIMEOUT:
            raise ValueError("provider timeout must be within 30 seconds")
        private_provider_logs()

    async def call(
        self, model_id: str, content: list[dict[str, Any]], *, max_tokens: int = 2048
    ) -> ModelReply | ModelUnavailable:
        started = monotonic()
        status: Literal["ok", "unavailable", "timeout", "invalid"] = "ok"
        response: dict[str, Any] = {}
        text = ""
        try:
            async with asyncio.timeout(self.timeout):
                response = await asyncio.to_thread(
                    self.client.converse,
                    modelId=model_id,
                    messages=[{"role": "user", "content": content}],
                    inferenceConfig={"temperature": 0, "maxTokens": max_tokens},
                )
            text = "\n".join(
                b["text"] for b in response["output"]["message"]["content"] if "text" in b
            )
        except TimeoutError:
            status = "timeout"
        except Exception:
            status = "unavailable"
        usage = response.get("usage", {})
        meta = CallMetadata(
            model_id=model_id,
            policy_version=self.policy_version,
            latency_ms=(monotonic() - started) * 1000,
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
            usage_known="inputTokens" in usage and "outputTokens" in usage,
            status=status,
        )
        self.observe(meta)
        if status != "ok":
            return ModelUnavailable(
                reason="timeout" if status == "timeout" else "unavailable", metadata=(meta,)
            )
        return ModelReply(text=text, metadata=meta)
