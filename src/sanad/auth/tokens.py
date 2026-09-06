"""The single credential generator. Callers retain plaintext only for immediate delivery."""

import re
import secrets
from typing import Literal

from pydantic import SecretStr

from sanad.domain.boundaries import _BoundaryValue
from sanad.store import keys
from sanad.store.protocol import Store
from sanad.store.records import CommitRequest, CommitResult, Forbidden

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
type Purpose = Literal["doctor_login", "patient_login", "invitation"]


class Token(_BoundaryValue):
    secret: SecretStr
    hash: str


def issue_token() -> Token:
    raw = secrets.token_urlsafe(32)
    return Token(secret=SecretStr(raw), hash=keys.digest(raw))


def token_hash(raw: str) -> str | None:
    return keys.digest(raw) if TOKEN_PATTERN.fullmatch(raw) else None


def consume_token(store: Store, request: CommitRequest) -> CommitResult:
    """Consume through the store's actor/state/version/clock-guarded transaction."""
    if request.command.payload.get("type") != "ExchangeLogin":
        return Forbidden()
    return store.commit_account(request)
