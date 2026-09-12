"""Explicit secret lifecycle, webhook registration, tick and sanitized log commands."""

import argparse
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from deploy.common import (
    PARAMETERS,
    OperationError,
    client,
    command,
    env_values,
    environment,
    outputs,
    parameter_values,
    session,
)
from sanad.channels.telegram.settings import TelegramSettings
from sanad.channels.telegram.transport import register_webhook


def telegram_settings(values: dict[str, str]) -> TelegramSettings:
    return TelegramSettings(
        bot_id=values["bot-token"].split(":", 1)[0],
        bot_token=SecretStr(values["bot-token"]),
        webhook_secret=SecretStr(values["webhook-secret"]),
        admin_user_id=values["admin-telegram-id"],
    )


def secrets_set(aws: Any, env: str) -> None:
    ssm = client(aws, "ssm")
    local = env_values({value[1] for value in PARAMETERS.values() if value[1] is not None})
    current, _ = parameter_values(ssm, env)
    out = outputs(client(aws, "cloudformation"), env)
    values = {}
    for suffix, (_, name) in PARAMETERS.items():
        if name:
            values[suffix] = local[name]
        elif suffix in {"webhook-secret", "tick-secret"}:
            values[suffix] = current.get(suffix) or secrets.token_hex(32)
        else:
            # SSM refuses empty strings. Readiness works without this value; deploy
            # replaces the explicit sentinel with the stack URL before app smoke.
            values[suffix] = out.get("FunctionUrl", "pending-infrastructure-only").rstrip("/")
    telegram_settings(values)  # validate secret/ID shapes before the first write
    for suffix, (kind, _) in PARAMETERS.items():
        if current.get(suffix) != values[suffix]:
            ssm.put_parameter(
                Name=f"/sanad/{env}/{suffix}", Value=values[suffix], Type=kind, Overwrite=True
            )
        print(f"/sanad/{env}/{suffix} configured")


def redact_log(text: str) -> str:
    text = re.sub(r"bot[0-9]+:[A-Za-z0-9_-]+", "bot<redacted>", text)
    text = re.sub(r"/(?:d|p|pl)/[^\s?\"'<>]+", "/exchange/<redacted>", text)
    return re.sub(
        r"(?i)(?:x-goog-api-key|GEMINI_API_KEY|key|x-sanad-sig|secret_token|authorization|password)[\"']?\s*[=:]\s*[\"']?[^\s,\"'&]+",
        "credential=<redacted>",
        text,
    )


def tick_fire(aws: Any, env: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    out = outputs(client(aws, "cloudformation"), env)
    result = client(aws, "lambda").invoke(
        FunctionName=out["RelayFunctionName"],
        InvocationType="RequestResponse",
        Payload=json.dumps(event or {}).encode(),
    )
    if result.get("FunctionError"):
        raise OperationError("Tick relay failed; inspect sanitized logs")
    return dict(json.loads(result["Payload"].read()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    groups = parser.add_subparsers(dest="group", required=True)
    for name, actions in {
        "secrets": ("set", "delete", "check"),
        "webhook": ("register", "info"),
        "tick": ("fire",),
        "logs": ("tail",),
    }.items():
        child = groups.add_parser(name)
        sub = child.add_subparsers(dest="action", required=True)
        for action in actions:
            environment(sub.add_parser(action))
    args = parser.parse_args()
    aws = session()
    ssm = client(aws, "ssm")
    if args.group == "secrets":
        if args.action == "set":
            secrets_set(aws, args.env)
        elif args.action == "delete":
            ssm.delete_parameters(Names=[f"/sanad/{args.env}/{n}" for n in PARAMETERS])
            for n in PARAMETERS:
                print(f"/sanad/{args.env}/{n} deleted (or already absent)")
        else:
            response = ssm.get_parameters(
                Names=[f"/sanad/{args.env}/{n}" for n in PARAMETERS], WithDecryption=False
            )
            found = {p["Name"] for p in response["Parameters"]}
            for n in PARAMETERS:
                name = f"/sanad/{args.env}/{n}"
                print(name, "exists" if name in found else "missing")
    elif args.group == "webhook":
        values, _ = parameter_values(ssm, args.env)
        settings = telegram_settings(values)
        out = outputs(client(aws, "cloudformation"), args.env)
        with httpx.Client(trust_env=False, timeout=20) as http:
            if args.action == "register":
                if values["public-base-url"] != out["FunctionUrl"].rstrip("/"):
                    raise OperationError("SSM public URL does not match this stack")
                result = register_webhook(
                    settings, out["FunctionUrl"].rstrip("/") + "/tg", http=http
                )
                if result.status != "registered":
                    raise OperationError("Webhook registration " + result.status)
                print("Webhook registered on the stack /tg URL")
            else:
                response = http.post(
                    settings.api_base
                    + "/bot"
                    + settings.bot_token.get_secret_value()
                    + "/getWebhookInfo"
                )
                data = response.json()
                if response.status_code != 200 or not data.get("ok"):
                    raise OperationError("getWebhookInfo failed")
                info = data["result"]
                # Provider error text and arbitrary URL paths may contain secrets.
                safe = {
                    k: info[k]
                    for k in (
                        "pending_update_count",
                        "has_custom_certificate",
                        "max_connections",
                        "allowed_updates",
                        "last_error_date",
                    )
                    if k in info
                }
                safe["url"] = (
                    (
                        urlsplit(info.get("url", "")).scheme
                        + "://"
                        + urlsplit(info.get("url", "")).netloc
                        + "/<redacted-path>"
                    )
                    if info.get("url")
                    else ""
                )
                print(json.dumps(safe, sort_keys=True))
    elif args.group == "tick":
        print(json.dumps(tick_fire(aws, args.env), sort_keys=True))
    else:
        for function in ("app", "relay"):
            group = f"/aws/lambda/sanad-{args.env}-{function}"
            pages = (
                client(aws, "logs")
                .get_paginator("filter_log_events")
                .paginate(
                    logGroupName=group,
                    startTime=int((time.time() - 600) * 1000),
                )
            )
            recent: list[dict[str, Any]] = []
            for page in pages:
                recent = (recent + page["events"])[-100:]
            for event in recent:
                print(redact_log(event["message"]))


if __name__ == "__main__":
    command(main)
