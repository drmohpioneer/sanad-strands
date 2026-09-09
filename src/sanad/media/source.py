"""Channel-neutral byte source; composition binds opaque handles to owner scope."""

from typing import Literal, Protocol

from pydantic import Field

from sanad.domain.boundaries import _BoundaryValue


class FileBytes(_BoundaryValue):
    data: bytes = Field(repr=False)


class MediaFailure(_BoundaryValue):
    reason: str
    route: Literal["media_failure"] = "media_failure"
    request_resend: bool = True
    durable: bool = False
    review_obligation_id: str | None = None
    resend_intent_id: str | None = None


class MediaSource(Protocol):
    def fetch(self, handle: str) -> FileBytes | MediaFailure: ...
