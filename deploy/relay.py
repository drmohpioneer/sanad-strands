"""Python 3.12 zip relay; tick_signing is copied from the app source unchanged."""

import json
import os
import secrets
import socket
import time
import urllib.error
import urllib.request
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

from sanad.ops.tick_signing import signed_headers

_secret: str | None = None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _secret
    if _secret is None:
        _secret = boto3.client(
            "ssm", config=Config(connect_timeout=2, read_timeout=3)
        ).get_parameter(Name=os.environ["TICK_PARAMETER"], WithDecryption=True)["Parameter"][
            "Value"
        ]
    assert _secret is not None
    # Explicit signed nonce/time can be supplied by the operator for replay smoke.
    # The relay is private and callable only through IAM, never the public URL.
    ts = str(event.get("timestamp", int(time.time())))
    nonce = str(event.get("nonce", secrets.token_hex(32)))
    body = b"{}"
    request = urllib.request.Request(
        os.environ["APP_URL"].rstrip("/") + "/internal/tick",
        data=body,
        headers=signed_headers(_secret, ts, nonce, body),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
            return {"status": response.status, "nonce": nonce, "timestamp": ts, "result": result}
    except urllib.error.HTTPError as error:
        if error.code == 429:
            print("tick skipped: app throttled")
            return {"skipped": "app_throttled"}
        # No URL, headers or HTTP response body in an exception/trace.
        raise RuntimeError(f"tick HTTP {error.code}") from None
    except TimeoutError:
        print("tick skipped: app timeout")
        return {"skipped": "app_timeout"}
    except urllib.error.URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            print("tick skipped: app connect timeout")
            return {"skipped": "app_timeout"}
        raise RuntimeError("tick connection failed") from None
