"""Operator IO, redacted errors and reproducible release metadata."""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
PARAMETERS = {
    "gemini_api_key": ("SecureString", "GEMINI_API_KEY"),
    "bot-token": ("SecureString", "TELEGRAM_BOT_TOKEN_SANAD_STRANDS"),
    "webhook-secret": ("SecureString", None),
    "tick-secret": ("SecureString", None),
    "admin-telegram-id": ("SecureString", "SANAD_ADMIN_TELEGRAM_USER_ID"),
    "public-base-url": ("String", None),
    "bot-username": ("String", "SANAD_TELEGRAM_BOT_USERNAME"),
    "budget-email": ("String", "SANAD_BUDGET_EMAIL"),
    "operator-name": ("String", None),
    "clinic-contact": ("String", None),
    "doctor-access-code": ("SecureString", None),
}


class OperationError(RuntimeError):
    """Only deliberate, non-sensitive operator messages are printed."""


def command(main: Callable[[], None]) -> None:
    try:
        main()
    except OperationError as error:
        raise SystemExit(str(error)) from None
    except ClientError as error:
        code = error.response["Error"]["Code"]
        raise SystemExit(f"AWS {error.operation_name}: {code}; operation stopped") from None
    except Exception as error:
        raise SystemExit(
            f"{type(error).__name__}: operation failed; no sensitive details logged"
        ) from None


def environment(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env", choices=("dev", "judge"), default="dev")


def session() -> Any:
    value = boto3.Session()
    if value.region_name != "us-east-1":
        raise OperationError("Expected AWS_DEFAULT_REGION=us-east-1")
    expected = os.environ.get("AWS_ACCOUNT_ID")
    if not expected or value.client("sts").get_caller_identity()["Account"] != expected:
        raise OperationError("AWS identity does not match configured AWS_ACCOUNT_ID")
    return value


def client(aws: Any, service: str) -> Any:
    # A 3-second connect budget failed four deploys on 2026-09-11 from an ordinary home
    # connection while every endpoint answered within seconds once connected.
    return aws.client(
        service,
        config=Config(
            connect_timeout=10, read_timeout=60, retries={"max_attempts": 3, "mode": "standard"}
        ),
    )


def env_values(names: set[str], path: Path = ROOT / ".env") -> dict[str, str]:
    """Read only explicitly named operator inputs; never execute shell syntax."""
    found = {}
    for line in path.read_text().splitlines():
        name, sep, value = line.removeprefix("export ").partition("=")
        name = name.strip()
        if sep and name in names:
            try:
                parts = shlex.split(value, comments=True)
            except ValueError:
                raise OperationError(f"Invalid .env syntax for {name}") from None
            if len(parts) == 1 and parts[0].strip():
                found[name] = parts[0]
    if missing := names - found.keys():
        raise OperationError("Missing .env names: " + ", ".join(sorted(missing)))
    return found


def git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def outputs(cfn: Any, env: str) -> dict[str, str]:
    result = cfn.describe_stacks(StackName=f"sanad-{env}")["Stacks"][0]
    return {x["OutputKey"]: x["OutputValue"] for x in result.get("Outputs", [])}


def parameter_values(
    ssm: Any, env: str, *, include_operator: bool = False
) -> tuple[dict[str, str], dict[str, int]]:
    result = ssm.get_parameters(
        Names=[
            f"/sanad/{env}/{name}"
            for name in PARAMETERS
            if include_operator or name != "operator-name"
        ],
        WithDecryption=True,
    )
    values = {x["Name"].rsplit("/", 1)[1]: x["Value"] for x in result["Parameters"]}
    versions = {x["Name"]: x["Version"] for x in result["Parameters"]}
    return values, versions


def configuration_revision(ssm: Any, env: str) -> str:
    """Metadata fingerprint also detects parameters deleted and recreated at version 1."""
    result = ssm.get_parameters(
        Names=[
            f"/sanad/{env}/{name}"
            for name in PARAMETERS
            if name not in {"public-base-url", "operator-name"}
        ],
        WithDecryption=False,
    )
    metadata = sorted(
        (p["Name"], p["Version"], str(p.get("LastModifiedDate"))) for p in result["Parameters"]
    )
    return hashlib.sha256(json.dumps(metadata).encode()).hexdigest()


def digest(value: str) -> str:
    if re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
        raise argparse.ArgumentTypeError("image must be a sha256 digest (no mutable tag)")
    return value


def revision(template: str, relay: bytes) -> str:
    return hashlib.sha256(template.encode() + relay).hexdigest()


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise OperationError("release history must be an append-only JSON array")
    return data


def record_release(path: Path, record: dict[str, Any]) -> None:
    data = history(path)
    data.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def previous_release(records: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [r for r in records if r.get("status") == "passed" and r.get("image_digest")]
    current = records[-1].get("image_digest") if records else None
    for prior in reversed(successful):
        if prior["image_digest"] != current:
            return prior
    raise OperationError("No previous distinct image with passing smoke checks")


def wait_stack(cfn: Any, name: str, *, sleep: Callable[[float], None] = time.sleep) -> str:
    for _ in range(240):
        state = cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
        if state in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
            return str(state)
        if not state.endswith("_IN_PROGRESS"):
            failures = cfn.describe_stack_events(StackName=name)["StackEvents"]
            for event in failures:
                if event["ResourceStatus"].endswith("FAILED"):
                    # Resource identifiers/statuses only; provider reason may contain input values.
                    reason = event.get("ResourceStatusReason", "")
                    code = " SubscriptionRequired" if "SubscriptionRequired" in reason else ""
                    print(
                        f"{event['LogicalResourceId']} {event['ResourceType']} "
                        f"{event['ResourceStatus']}{code}"
                    )
            raise OperationError(f"Stack {state}; deployment stopped")
        sleep(5)
    raise OperationError("Stack wait timed out; inspect status before retrying")


def dev_only(env: str) -> None:
    if env != "dev":
        raise OperationError("This command requires --env dev")


def dev_outputs(aws: Any, env: str) -> dict[str, str]:
    dev_only(env)
    out = outputs(client(aws, "cloudformation"), env)
    if out.get("TableName") != "sanad-dev-data":
        raise OperationError("Expected stack TableName sanad-dev-data; operation refused")
    return out


def operator_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env", required=True, choices=("dev", "judge"))
    parser.add_argument("--yes", action="store_true", help="execute; default is dry-run")


def parse_instant(value: str) -> datetime:
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError
        return at.astimezone(UTC)
    except ValueError:
        raise OperationError("Expected a timezone-aware ISO instant") from None


def oldest(values: list[str | None], now: datetime) -> dict[str, Any]:
    times = [parse_instant(value) for value in values if value is not None]
    at = min(times) if times else None
    return {
        "count": len(times),
        "oldest_at": at.isoformat() if at else None,
        "age_seconds": max(0.0, (now - at).total_seconds()) if at else None,
    }


def safe_public_text(value: str) -> str:
    """Refuse credentials/contact data, never sanitize them into an apparent pass."""
    phone = re.findall(r"(?<![\w])\+?\d[\d ()-]{7,}\d(?![\w])", value)
    if (
        any(len(re.sub(r"\D", "", match)) >= 9 for match in phone)
        or re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value)
        or re.search(r"\b\d+:[A-Za-z0-9_-]{12,}", value)
        or re.search(r"(?i)\b(?:bearer\s+|sk-|AIza|AKIA|token[=: ]|secret[=: ])\S+", value)
        or re.search(r"(?<![\w])[A-Za-z0-9_-]{28,}(?![\w])", value)
        or any(ord(char) < 32 and char not in "\n\t" for char in value)
    ):
        raise OperationError("Sensitive value detected; public record refused")
    return value
