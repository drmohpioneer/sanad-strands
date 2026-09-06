"""One byte-level signing scheme, also packaged unchanged into the tick relay."""

import hashlib
import hmac
import re
from collections.abc import Mapping


def sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    return hmac.new(
        secret.encode(), f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256
    ).hexdigest()


def signed_headers(secret: str, timestamp: str, nonce: str, body: bytes) -> dict[str, str]:
    return {
        "x-sanad-ts": timestamp,
        "x-sanad-nonce": nonce,
        "x-sanad-sig": sign(secret, timestamp, nonce, body),
        "content-type": "application/json",
    }


def authentic(secret: str, headers: Mapping[str, str], body: bytes, now: float) -> bool:
    ts, nonce, signature = (
        headers.get(k, "") for k in ("x-sanad-ts", "x-sanad-nonce", "x-sanad-sig")
    )
    if (
        not secret
        or re.fullmatch(r"[0-9]{1,12}", ts) is None
        or abs(int(ts) - now) > 300
        or re.fullmatch(r"[a-f0-9]{32,128}", nonce) is None
        or re.fullmatch(r"[a-f0-9]{64}", signature) is None
    ):
        return False
    return hmac.compare_digest(sign(secret, ts, nonce, body), signature)
