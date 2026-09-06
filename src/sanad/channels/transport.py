from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal, Protocol, Self

from pydantic import JsonValue, StrictBool, model_validator

from sanad.domain import NonnegativeInt
from sanad.domain.boundaries import NonblankStr, _BoundaryValue


class SendOutcome(_BoundaryValue):
    status: Literal["accepted", "uncertain", "failed"]
    provider_message_id: NonblankStr | None = None
    retryable: StrictBool = False
    code: NonblankStr | None = None
    retry_after_seconds: NonnegativeInt | None = None

    @model_validator(mode="after")
    def evidence(self) -> Self:
        if (self.status == "accepted") != (self.provider_message_id is not None):
            raise ValueError("only acceptance carries a provider message ID")
        if self.status == "failed" and self.code is None:
            raise ValueError("failure requires a redacted code")
        return self


class ProvablyUnsent(Exception):
    """Adapter guarantee: the attempt failed before transmitting any bytes."""


class CallbackOutcome(_BoundaryValue):
    status: Literal["accepted", "uncertain", "failed"]
    code: NonblankStr | None = None


class CapturedCallback(_BoundaryValue):
    callback_query_id: NonblankStr
    text: str


class Transport(Protocol):
    def send(self, recipient_ref: str, payload: dict[str, JsonValue]) -> SendOutcome: ...
    def answer_callback(self, callback_query_id: str, text: str) -> CallbackOutcome: ...


@dataclass(frozen=True)
class CapturedSend:
    recipient_ref: str
    payload: dict[str, JsonValue] = field(repr=False)


class CapturedTransport:
    def __init__(self, script: tuple[SendOutcome | Exception, ...] = ()):
        self.script = deque(script)
        self.callback_calls: list[CapturedCallback] = []
        self.calls: list[CapturedSend] = []
        self.possibly_sent: list[CapturedSend] = []

    def send(self, recipient_ref: str, payload: dict[str, JsonValue]) -> SendOutcome:
        call = CapturedSend(recipient_ref, deepcopy(payload))
        self.calls.append(call)
        outcome = (
            self.script.popleft()
            if self.script
            else SendOutcome(status="accepted", provider_message_id=f"captured-{len(self.calls)}")
        )
        if not isinstance(outcome, ProvablyUnsent):
            self.possibly_sent.append(call)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def answer_callback(self, callback_query_id: str, text: str) -> CallbackOutcome:
        self.callback_calls.append(CapturedCallback(callback_query_id=callback_query_id, text=text))
        return CallbackOutcome(status="accepted")
