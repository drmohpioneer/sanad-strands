"""Explicit secret lifecycle, webhook registration, tick and sanitized log commands."""

import argparse
import json
import re
import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from deploy.common import (
    PARAMETERS,
    OperationError,
    client,
    command,
    dev_only,
    dev_outputs,
    env_values,
    environment,
    oldest,
    operator_arguments,
    outputs,
    parameter_values,
    parse_instant,
    safe_public_text,
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


def secrets_set(aws: Any, env: str, *, operator_name: str | None = None) -> None:
    if operator_name is not None:
        safe_public_text(operator_name)
        if not operator_name.strip() or len(operator_name) > 80 or "\n" in operator_name:
            raise OperationError("Operator name must be a plain name of 1-80 characters")
    ssm = client(aws, "ssm")
    local = env_values({value[1] for value in PARAMETERS.values() if value[1] is not None})
    current, _ = parameter_values(ssm, env, include_operator=True)
    out = outputs(client(aws, "cloudformation"), env)
    values = {}
    for suffix, (_, name) in PARAMETERS.items():
        if suffix == "operator-name":
            if operator_name is not None:
                values[suffix] = operator_name
            continue
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
        if suffix not in values:
            continue
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
    operator_arguments(groups.add_parser("health-report"))
    for name, actions in {
        "secrets": ("set", "delete", "check"),
        "webhook": ("register", "info"),
        "tick": ("fire",),
        "logs": ("tail",),
    }.items():
        child = groups.add_parser(name)
        sub = child.add_subparsers(dest="action", required=True)
        for action in actions:
            action_parser = sub.add_parser(action)
            environment(action_parser)
            if name == "secrets" and action == "set":
                action_parser.add_argument("--operator-name")
    args = parser.parse_args()
    if args.group == "health-report":
        dev_only(args.env)
        print(json.dumps(health_report(session(), args.env), sort_keys=True))
        return
    aws = session()
    ssm = client(aws, "ssm")
    if args.group == "secrets":
        if args.action == "set":
            secrets_set(aws, args.env, operator_name=args.operator_name)
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
            recent: list[dict[str, Any]] = []
            for event in log_events(aws, group, int((time.time() - 600) * 1000)):
                recent = (recent + [event])[-100:]
            for event in recent:
                print(redact_log(event["message"]))


# Only numerical targets in operations.md; Lambda Errors does not measure latency.
THRESHOLDS = {
    "durable_webhook_ack": "under 2 seconds",
    "readable_text_safety": "under 5 seconds",
    "ordinary_text_response": "around 15 seconds",
    "scheduler_lag": "under 2 minutes",
}
MISSING_ALARMS = [f"{name}: no alarm ({target})" for name, target in THRESHOLDS.items()]


def log_events(aws: Any, group: str, start_ms: int) -> Iterator[dict[str, Any]]:
    pages = (
        client(aws, "logs")
        .get_paginator("filter_log_events")
        .paginate(
            logGroupName=group,
            startTime=start_ms,
        )
    )
    for page in pages:
        yield from page.get("events", [])


def health_report(aws: Any, env: str, *, now: datetime | None = None) -> dict[str, Any]:
    from deploy.cleanup import row_body, scan_rows

    out = dev_outputs(aws, env)
    now = now or datetime.now(UTC)
    rows = list(scan_rows(client(aws, "dynamodb"), out["TableName"], "PK", ""))
    bodies = [row_body(row) for row in rows]
    measures: dict[str, Any] = {}
    source = "DynamoDB consistent scan: " + out["TableName"]

    def measured(name: str, value: Any, origin: str = source) -> None:
        measures[name] = {"value": value, "source": origin, "alarm": "no alarm"}

    def unavailable(name: str, reason: str, origin: str = source) -> None:
        measures[name] = {
            "value": "not measurable in this build",
            "reason": reason,
            "source": origin,
            "alarm": "no alarm",
        }

    for kind, field in (("inbound_receipt", "received_at"), ("media_work", "created_at")):
        selected = [
            b for b in bodies if b.get("entity_type") == kind and b.get("state") != "completed"
        ]
        measured("oldest_unfinished_" + kind, oldest([b.get(field) for b in selected], now))
    app_group = f"/aws/lambda/sanad-{env}-app"
    relay_group = f"/aws/lambda/sanad-{env}-relay"
    from sanad.api.failures import REASONS

    failed_requests = dict.fromkeys(REASONS, 0)
    last_tick: int | None = None
    for group in (app_group, relay_group):
        for event in log_events(aws, group, int((now - timedelta(hours=1)).timestamp() * 1000)):
            if group == app_group and event.get("timestamp", 0) <= now.timestamp() * 1000:
                import re

                failure = re.search(
                    r"request_failed reason=([a-z_]+) route_family=([a-z]+)",
                    event.get("message", ""),
                )
                if failure and failure[1] in failed_requests:
                    failed_requests[failure[1]] += 1
            if group == app_group and "tick accepted nonce=" in event.get("message", ""):
                stamp = event["timestamp"]
                if stamp <= now.timestamp() * 1000:
                    last_tick = max(last_tick or stamp, stamp)
    measured(
        "failed_requests_last_hour", failed_requests, app_group + "; request_failed reason codes"
    )
    unavailable(
        "scheduler_lag", "Tick logs record acceptance, not scheduled/due-to-handled lag.", app_group
    )
    measures["scheduler_lag"]["last_tick_accepted_at"] = (
        datetime.fromtimestamp(last_tick / 1000, UTC).isoformat() if last_tick else None
    )
    expired = 0
    for body in bodies:
        if body.get("lease_expires_at") and parse_instant(body["lease_expires_at"]) <= now:
            expired += 1
        for field in ("processing_claim", "delivery_claim"):
            claim = body.get(field) or {}
            if claim.get("expires_at") and parse_instant(claim["expires_at"]) <= now:
                expired += 1
    measured("currently_expired_claims", expired)
    unavailable(
        "leases_expired_last_hour", "Replaced/released leases have no retained expiry event series."
    )
    unavailable(
        "leases_reclaimed_last_hour",
        "Claim generations retain current state, not a timed reclaim history.",
    )
    for kind, terminals in (
        ("mission", {"fulfilled", "cancelled", "closed_unfulfilled", "superseded"}),
        ("followup", {"fulfilled", "cancelled"}),
        ("review", {"resolved"}),
    ):
        missing = 0
        for row, body in zip(rows, bodies, strict=True):
            if body.get("entity_type") != kind or body.get("state") in terminals:
                continue
            clock = body.get("work_clock") or {}
            if (
                not clock.get("next_action_at")
                or not row.get("due_lane_shard")
                or not row.get("due_sort")
            ):
                missing += 1
        measured(kind + "_without_due_work", missing)
    overdue = [
        b
        for b in bodies
        if b.get("entity_type") in {"mission", "followup"}
        and b.get("state") not in {"fulfilled", "cancelled", "closed_unfulfilled", "superseded"}
        and b.get("due_at")
        and parse_instant(b["due_at"]) <= now
    ]
    measured("oldest_overdue", oldest([b.get("due_at") for b in overdue], now))
    reviews = [
        b for b in bodies if b.get("entity_type") == "review" and b.get("state") != "resolved"
    ]
    measured("oldest_open_review", oldest([b.get("created_at") for b in reviews], now))
    for outcome in ("definite_failure", "uncertain"):
        attempts = [
            b
            for b in bodies
            if b.get("entity_type") == "delivery_attempt"
            and b.get("outcome") == outcome
            and b.get("ended_at")
            and now - timedelta(hours=24) <= parse_instant(b["ended_at"]) <= now
        ]
        measured(outcome + "_deliveries_last_24h", len(attempts))
    unacknowledged = unknown = 0
    for row, body in zip(rows, bodies, strict=True):
        if (
            body.get("entity_type") not in {"incident", "intake_concern"}
            or body.get("state") == "resolved"
        ):
            continue
        linked = [
            b
            for r, b in zip(rows, bodies, strict=True)
            if r["PK"] == row["PK"]
            and b.get("entity_type") == "review"
            and b.get("id") == body.get("review_obligation_id")
        ]
        if not linked:
            unknown += 1
        elif linked[0].get("state") == "open" and not linked[0].get("acknowledged_at"):
            unacknowledged += 1
    measured(
        "unacknowledged_urgent_incidents", {"count": unacknowledged, "missing_review": unknown}
    )
    for name, metric, statistic, operation in (
        ("consumed_read_units_last_hour", "ConsumedReadCapacityUnits", "Sum", None),
        ("consumed_write_units_last_hour", "ConsumedWriteCapacityUnits", "Sum", None),
        ("scan_requests_last_hour", "SuccessfulRequestLatency", "SampleCount", "Scan"),
    ):
        dimensions = [{"Name": "TableName", "Value": out["TableName"]}]
        if operation:
            dimensions.append({"Name": "Operation", "Value": operation})
        result = client(aws, "cloudwatch").get_metric_statistics(
            Namespace="AWS/DynamoDB",
            MetricName=metric,
            Dimensions=dimensions,
            StartTime=now - timedelta(hours=1),
            EndTime=now,
            Period=300,
            Statistics=[statistic],
        )
        measured(
            name,
            sum(p[statistic] for p in result["Datapoints"]),
            "CloudWatch AWS/DynamoDB " + metric + " " + statistic,
        )
    errors = {}
    for role, output_key in (("app", "AppFunctionName"), ("relay", "RelayFunctionName")):
        result = client(aws, "cloudwatch").get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName="Errors",
            Dimensions=[{"Name": "FunctionName", "Value": out[output_key]}],
            StartTime=now - timedelta(hours=1),
            EndTime=now,
            Period=300,
            Statistics=["Sum"],
        )
        errors[role] = sum(p["Sum"] for p in result["Datapoints"]) if result["Datapoints"] else None
    measured("lambda_errors_last_hour", errors, "CloudWatch AWS/Lambda Errors Sum, app and relay")
    measures["lambda_errors_last_hour"]["alarm"] = (
        "sanad-dev-apperrors, sanad-dev-relayerrors (5-minute windows)"
    )
    if None in errors.values():
        measures["lambda_errors_last_hour"]["reason"] = (
            "Missing datapoints are unknown, not measured zero."
        )
    for name, reason in {
        "model_tool_asr_errors": "No complete timed provider/tool error counter is emitted.",
        "clarification_rate": "Clarification counts lack a complete eligible-turn denominator.",
        "durable_webhook_ack": "No receipt-to-HTTP-ACK latency measurement is emitted.",
        "readable_text_safety": "No ingress-to-safety-result latency measurement is emitted.",
        "ordinary_text_response": "No ingress-to-response latency measurement is emitted.",
        "cost": "No complete cross-provider cost meter includes Gemini and all AWS services.",
    }.items():
        unavailable(name, reason, app_group + "; " + relay_group + "; DynamoDB")
    # The complete bill is unavailable, but the same measured gross Lambda
    # estimate used by smoke.cost is answerable without inventing other costs.
    from deploy.smoke import ARM_GB_SECOND_USD, REQUEST_USD

    lambda_cost: dict[str, float | None] = {}
    for output_key, memory_mb in (("AppFunctionName", 3008), ("RelayFunctionName", 128)):
        samples: dict[str, float | None] = {}
        for metric in ("Invocations", "Duration"):
            result = client(aws, "cloudwatch").get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName=metric,
                Dimensions=[{"Name": "FunctionName", "Value": out[output_key]}],
                StartTime=now - timedelta(hours=24),
                EndTime=now,
                Period=3600,
                Statistics=["Sum"],
            )
            samples[metric] = (
                sum(p["Sum"] for p in result["Datapoints"]) if result["Datapoints"] else None
            )
        duration, invocations = samples["Duration"], samples["Invocations"]
        lambda_cost[out[output_key]] = (
            duration / 1000 * memory_mb / 1024 * ARM_GB_SECOND_USD + invocations * REQUEST_USD
            if duration is not None and invocations is not None
            else None
        )
    measures["cost"]["lambda_gross_usd_last_24h"] = lambda_cost
    measures["cost"]["partial_source"] = (
        "CloudWatch AWS/Lambda Duration and Invocations Sum; smoke.cost ARM/request rates; "
        "before credits/free tier, excluding other AWS services and Gemini."
    )
    measured(
        "contact_count",
        sum(
            b.get("entity_type") == "outbound_intent"
            and b.get("audience") == "patient"
            and b.get("notification_purpose") == "routine_prompt"
            and b.get("status") == "provider_accepted"
            for b in bodies
        ),
    )
    measured(
        "opt_outs",
        sum(
            b.get("entity_type") == "patient" and b.get("contact_status") == "opted_out"
            for b in bodies
        ),
    )
    for name, threshold in THRESHOLDS.items():
        measures[name]["target"] = threshold
    return {
        "mode": "read-only",
        "measured_at": now.isoformat(),
        "rows_scanned": len(rows),
        "measures": measures,
        "missing_alarms": MISSING_ALARMS,
        "limitations": "Rows are not a historical census; scans are not atomic. "
        "Contacts count retained accepted routine intents; opt-outs count current patients. "
        "No raw rows or log messages are printed; no alarms or source instrumentation changed.",
    }


if __name__ == "__main__":
    command(main)
