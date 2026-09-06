"""The small SDK surface used by Sanad; no SDK-managed persistence or body traces."""

from collections.abc import AsyncGenerator, AsyncIterable, Callable
from time import monotonic
from typing import Any, Protocol

from pydantic import BaseModel
from strands.models import Model
from strands.types.content import Messages
from strands.types.streaming import StreamEvent

from sanad.models.io import CallMetadata


class UsageMetrics(Protocol):
    accumulated_usage: dict[str, int]


class SDKResult(Protocol):
    message: dict[str, Any]
    stop_reason: str


class MeasuredModel(Model):
    def __init__(
        self,
        delegate: Model,
        model_id: str,
        policy_version: str,
        observe: Callable[[CallMetadata], None],
    ):
        self.delegate, self.model_id, self.policy_version = delegate, model_id, policy_version
        self.observe = observe
        self.calls: list[CallMetadata] = []

    def update_config(self, **model_config: Any) -> None:
        self.delegate.update_config(**model_config)

    def get_config(self) -> Any:
        return self.delegate.get_config()

    async def structured_output(
        self,
        output_model: type[BaseModel],
        prompt: Messages,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        # Required abstract SDK method; Sanad validates plain JSON after the guarded loop.
        raise RuntimeError("tool-forced structured output is disabled")
        yield {}

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterable[StreamEvent]:
        started = monotonic()
        usage: dict[str, Any] = {}
        status: str = "unavailable"
        try:
            async for event in self.delegate.stream(*args, **kwargs):
                if "metadata" in event:
                    usage = dict(event["metadata"].get("usage", {}))
                yield event
            status = "ok"
        finally:
            meta = CallMetadata.model_validate(
                {
                    "model_id": self.model_id,
                    "policy_version": self.policy_version,
                    "latency_ms": (monotonic() - started) * 1000,
                    "input_tokens": usage.get("inputTokens", 0),
                    "output_tokens": usage.get("outputTokens", 0),
                    "usage_known": "inputTokens" in usage and "outputTokens" in usage,
                    "status": status,
                }
            )
            self.calls.append(meta)
            self.observe(meta)
