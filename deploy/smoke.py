"""Real, synthetic development checks. No real doctor or patient is enrolled."""

import argparse
import asyncio
import json
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from deploy.common import (
    OperationError,
    client,
    command,
    environment,
    outputs,
    parameter_values,
    session,
)
from deploy.ops import tick_fire
from sanad.accounts.commands import ApplyAsDoctor, ApproveDoctor
from sanad.accounts.service import AccountService
from sanad.auth.claim import ClaimService, consent_policy
from sanad.auth.commands import CreatePatientStub, IssuedInvitation, IssueInvitation
from sanad.channels.telegram import wording
from sanad.domain import PatientScope, TenantScope
from sanad.ops.tick_signing import signed_headers
from sanad.store import keys
from sanad.store._base import utc_now
from sanad.store.dynamodb import DynamoStore
from sanad.store.records import IdentityConfig
from sanad.web.security import HEADERS

ARM_GB_SECOND_USD = 0.0000133334
REQUEST_USD = 0.0000002


def require(ok: bool, message: str) -> None:
    if not ok:
        raise OperationError(message)


def health(http: httpx.Client, url: str, revision: str) -> dict[str, Any]:
    times = []
    for _ in range(2):
        start = time.monotonic()
        response = http.get(url + "/health")
        times.append(time.monotonic() - start)
        require(
            response.status_code == 200 and response.json().get("revision") == revision,
            "health revision",
        )
    require(times[1] < 1, "warm health exceeds 1 second")
    return {
        "first_request_seconds": round(times[0], 4),
        "warm_seconds": round(times[1], 4),
        "revision": revision,
    }


def tick(aws: Any, env: str, http: httpx.Client, url: str, secret: str) -> dict[str, Any]:
    timestamp, nonce = str(int(time.time())), secrets.token_hex(32)
    result = tick_fire(aws, env, {"timestamp": timestamp, "nonce": nonce})
    require(
        result.get("status") == 200 and result.get("result", {}).get("accepted") is True,
        "relay tick acceptance",
    )
    headers = signed_headers(secret, timestamp, nonce, b"{}")
    require(
        http.post(url + "/internal/tick", content=b"{}", headers=headers).status_code == 409,
        "tick replay",
    )
    forged = dict(headers, **{"x-sanad-nonce": secrets.token_hex(32), "x-sanad-sig": "0" * 64})
    require(
        http.post(url + "/internal/tick", content=b"{}", headers=forged).status_code == 401,
        "tick forgery",
    )
    logs_found = False
    for _ in range(12):
        logs = client(aws, "logs").filter_log_events(
            logGroupName=f"/aws/lambda/sanad-{env}-app",
            startTime=(int(timestamp) - 5) * 1000,
            filterPattern='"tick accepted"',
            limit=100,
        )
        if any(nonce in e["message"] for e in logs["events"]):
            logs_found = True
            break
        time.sleep(5)
    require(logs_found, "tick accepted log not observed")
    return {
        "relay": 200,
        "replay": 409,
        "forged": 401,
        "accepted_log": True,
        "nonce": nonce,
        "sweep": result["result"]["sweep"],
    }


def webhook(
    aws: Any, http: httpx.Client, url: str, table: str, values: dict[str, str]
) -> dict[str, Any]:
    update_id = secrets.randbelow(2**31)
    # Outside Telegram's 52-bit user range: this cannot address a real account.
    subject = "999999999999999907"
    update = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "from": {"id": subject, "is_bot": False},
            "chat": {"id": subject, "type": "private"},
            "document": {
                "file_id": "synthetic-deploy-file",
                "file_unique_id": "synthetic-deploy-file",
            },
            "caption": "Synthetic deployment fixture",
        },
    }
    headers = {"X-Telegram-Bot-Api-Secret-Token": values["webhook-secret"]}
    require(
        http.post(url + "/tg", json=update, headers=headers).status_code == 200,
        "webhook acceptance",
    )
    key = keys.inbound(
        "telegram", keys.digest(values["bot-token"].split(":", 1)[0] + ":" + str(update_id))
    )
    ddb = client(aws, "dynamodb")

    def count() -> int:
        rows = ddb.query(
            TableName=table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": key.pk}},
            ConsistentRead=True,
        )["Items"]
        return sum(row.get("entity_type", {}).get("S") == "inbound_receipt" for row in rows)

    require(count() == 1, "durable receipt before ACK")
    require(
        http.post(url + "/tg", json=update, headers=headers).status_code == 200 and count() == 1,
        "duplicate webhook receipt",
    )
    require(
        http.post(
            url + "/tg", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "forged"}
        ).status_code
        == 401,
        "webhook forgery",
    )
    completed = False
    for _ in range(30):
        row = ddb.get_item(
            TableName=table,
            Key={"PK": {"S": key.pk}, "SK": {"S": key.sk}},
            ConsistentRead=True,
        ).get("Item", {})
        if json.loads(row.get("body", {}).get("S", "{}")).get("state") == "completed":
            completed = True
            break
        time.sleep(0.5)
    require(completed, "async worker completion")
    return {
        "accepted": 200,
        "duplicate": 200,
        "wrong_secret": 401,
        "receipt_count": 1,
        "worker_completed": True,
        "update_id": update_id,
    }


def tenant(aws: Any, table: str, url: str, *, bot_id: str | None = None) -> dict[str, Any]:
    store = DynamoStore(client(aws, "dynamodb"), table)
    # A separate synthetic bot namespace prevents the live bot from delivering
    # this account-service smoke's outbox. Values are impossible Telegram IDs.
    bot, admin = bot_id or "9907" + str(secrets.randbelow(10**12)), "999999999999999900"
    accounts = AccountService(
        store,
        utc_now,
        IdentityConfig(bot_id=bot, admin_user_id=admin),
        lambda key, language, fields: wording.render(key, language, **fields),
        approve_label=(wording.APPROVE_BUTTON, "Approve"),
        reject_label=(wording.REJECT_BUTTON, "Reject"),
    )
    doctors = []
    actors = []
    for subject in ("999999" + str(secrets.randbelow(10**12)).zfill(12) for _ in range(2)):
        result = accounts.apply(
            ApplyAsDoctor(
                command_id=uuid4().hex,
                actor=store.authorize(bot, subject).principal,
                private_chat_id=subject,
                claimed_name="Synthetic Deployment Doctor",
            )
        )
        require(result.status == "accepted", "synthetic doctor application")
        application = accounts.application(keys.digest(f"{bot}:{subject}"))
        require(application is not None, "application stored")
        assert application
        approved = accounts.approve(
            ApproveDoctor(
                command_id=uuid4().hex,
                actor=store.authorize(bot, admin).principal,
                application_id=application.id,
                expected_application_version=application.version,
            )
        )
        require(approved.status == "accepted", "synthetic doctor approval")
        actor = store.authorize(bot, subject).principal
        require(
            actor.actor_kind == "doctor" and actor.doctor_id is not None, "doctor authorization"
        )
        actors.append(actor)
        doctors.append(actor.doctor_id)
    require(
        doctors[0] != doctors[1]
        and store.authorize(bot, actors[0].subject).principal.doctor_id == doctors[0],
        "tenant A authorization isolation",
    )
    claims = ClaimService(
        accounts,
        url,
        consent_policy=lambda doctor_id: consent_policy(clinic_contact="Synthetic clinic contact"),
    )
    created = claims.create_stub(
        CreatePatientStub(
            command_id=uuid4().hex, actor=actors[0], display_name="Synthetic Deployment Patient"
        )
    )
    require(created.status == "accepted", "synthetic patient stub")
    assert doctors[0] and doctors[1]
    rows, _ = store.list_patients(TenantScope(doctor_id=doctors[0]))
    require(len(rows) == 1, "tenant A patient list")
    patient = rows[0].id
    require(
        store.get_patient_profile(PatientScope(doctor_id=doctors[0], patient_id=patient))
        is not None,
        "tenant A patient read",
    )
    require(
        store.get_patient_profile(PatientScope(doctor_id=doctors[1], patient_id=patient)) is None,
        "tenant B denied foreign patient",
    )
    require(
        not store.list_patients(TenantScope(doctor_id=doctors[1]))[0],
        "tenant B patient list isolation",
    )
    return {
        "doctors": 2,
        "patient_stubs": 1,
        "foreign_scope_denied": True,
        "synthetic_bot_namespace": bot,
        "doctor_id": doctors[0],
        "doctor_subject": actors[0].subject,
        "patient_id": patient,
    }


def enrollment(
    aws: Any, http: httpx.Client, url: str, table: str, values: dict[str, str]
) -> dict[str, Any]:
    """Issue through the product and test the deployed worker's consent wiring."""
    bot = values["bot-token"].split(":", 1)[0]
    fixture = tenant(aws, table, url, bot_id=bot)
    store = DynamoStore(client(aws, "dynamodb"), table)
    accounts = AccountService(
        store,
        utc_now,
        IdentityConfig(bot_id=bot, admin_user_id=values["admin-telegram-id"]),
        lambda key, language, fields: wording.render(key, language, **fields),
        approve_label=(wording.APPROVE_BUTTON, "Approve"),
        reject_label=(wording.REJECT_BUTTON, "Reject"),
    )
    claims = ClaimService(
        accounts,
        url,
        consent_policy=lambda doctor_id: consent_policy(clinic_contact=values["clinic-contact"]),
    )
    invitation = claims.issue_invitation(
        IssueInvitation(
            command_id=uuid4().hex,
            actor=store.authorize(bot, fixture["doctor_subject"]).principal,
            patient_id=fixture["patient_id"],
        )
    )
    require(isinstance(invitation, IssuedInvitation), "enrollment invitation not issued")
    assert isinstance(invitation, IssuedInvitation)
    subject = "999998" + str(secrets.randbelow(10**12)).zfill(12)
    id = secrets.randbelow(2**31)
    response = http.post(
        url + "/tg",
        headers={"X-Telegram-Bot-Api-Secret-Token": values["webhook-secret"]},
        json={
            "update_id": id,
            "message": {
                "message_id": id,
                "date": int(time.time()),
                "from": {"id": subject, "is_bot": False},
                "chat": {"id": subject, "type": "private"},
                "text": "/start " + invitation.token.get_secret_value(),
            },
        },
    )
    require(response.status_code == 200, "enrollment receipt refused")
    for _ in range(30):
        inv = claims.invitation(keys.digest(invitation.token.get_secret_value()))
        pending = (
            claims.patient_claim(inv.pending_claim_id) if inv and inv.pending_claim_id else None
        )
        if pending and pending.state == "pending" and pending.candidate_subject == subject:
            return {"invitation_issued": True, "pending_claim": True, "patient_activated": False}
        time.sleep(0.5)
    raise OperationError("enrollment pending claim not observed")


def browser_session(http: httpx.Client, url: str) -> dict[str, Any]:
    path = "/d/unknown-synthetic-" + secrets.token_hex(8)
    response = http.get(url + path)
    require(response.status_code == 200 and "Continue" in response.text, "neutral Continue page")
    require(
        all(response.headers.get(k) == v for k, v in HEADERS.items()), "browser security headers"
    )
    require(
        http.post(url + path, headers={"Origin": url}).status_code == 403, "browser CSRF denial"
    )
    return {"continue": 200, "without_csrf": 403, "security_headers": True}


def cost(aws: Any, out: dict[str, str]) -> dict[str, Any]:
    now = datetime.now(UTC)
    functions = {}
    observed_cost = steady_cost = 0.0
    for key, memory in (("AppFunctionName", 3008), ("RelayFunctionName", 128)):
        name = out[key]
        numbers = {}
        for metric in ("Invocations", "Duration"):
            result = client(aws, "cloudwatch").get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName=metric,
                Dimensions=[{"Name": "FunctionName", "Value": name}],
                StartTime=now - timedelta(hours=24),
                EndTime=now,
                Period=3600,
                Statistics=["Sum"],
            )
            numbers[metric] = sum(p["Sum"] for p in result["Datapoints"])
        avg_ms = numbers["Duration"] / numbers["Invocations"] if numbers["Invocations"] else None
        gb_seconds = numbers["Duration"] / 1000 * memory / 1024
        observed_cost += gb_seconds * ARM_GB_SECOND_USD + numbers["Invocations"] * REQUEST_USD
        if avg_ms is not None:
            steady_cost += 43200 * (avg_ms / 1000 * memory / 1024 * ARM_GB_SECOND_USD + REQUEST_USD)
        functions[name] = dict(
            numbers, memory_mb=memory, average_duration_ms=avg_ms, gb_seconds=gb_seconds
        )
    credits: Any = "unavailable"
    try:
        plan = client(aws, "freetier").get_account_plan_state()
        credits = plan.get("accountPlanRemainingCredits", "unavailable")
    except ClientError as error:
        credits = "unavailable: " + error.response["Error"]["Code"]
    return {
        "window_hours": 24,
        "measured_at": now.isoformat(),
        "functions": functions,
        "arm_gb_second_usd": ARM_GB_SECOND_USD,
        "request_usd": REQUEST_USD,
        "observed_24h_lambda_usd": round(observed_cost, 6),
        "30_day_observed_window_lambda_usd": round(observed_cost * 30, 4),
        "30_day_minute_schedule_lambda_usd": round(steady_cost, 4),
        "remaining_credit": credits,
        "limitations": (
            "Gross Lambda only, before free tier/credits; 24h includes bootstrap, "
            "not 24h steady service. Minute forecast uses mixed measured mean durations. "
            "ECR, CodeBuild, storage, logs and models are separate."
        ),
    }


SYNTHETIC_SPEECH = (
    "Synthetic speech check. Record the number five. "
    "This is a test recording, with no patient information."
)


def synthetic_english_clip() -> bytes:
    """Generate the shared 15-second English smoke/live fixture locally.

    No TTS provider or credential is involved. Uses macOS say or local espeak,
    then ffmpeg to pad/cut the synthetic recording to exactly fifteen seconds.
    """
    with tempfile.TemporaryDirectory(prefix="sanad-synthetic-") as directory:
        source = Path(directory) / "speech.wav"
        if shutil.which("say"):
            argv = [
                "say",
                "-v",
                "Samantha",
                "--data-format=LEI16@16000",
                "-o",
                str(source),
                SYNTHETIC_SPEECH,
            ]
        elif shutil.which("espeak"):
            argv = ["espeak", "-v", "en", "-w", str(source), SYNTHETIC_SPEECH]
        else:
            raise OperationError("Local English speech generator unavailable")
        subprocess.run(argv, check=True, capture_output=True, timeout=30)
        return subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(source),
                "-af",
                "apad",
                "-t",
                "15",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "48k",
                "-f",
                "mp3",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout


def gemini(aws: Any, env: str) -> dict[str, Any]:
    """Prove decrypted SSM configuration from the operator, not Lambda egress."""
    from sanad.domain import Provenance
    from sanad.media.audio import ConvertedAudio, FFmpegConverter
    from sanad.media.speech import SpeechAdapter, Transcript
    from sanad.models.gemini import GeminiCaller

    name = f"/sanad/{env}/gemini_api_key"
    response = client(aws, "ssm").get_parameters(Names=[name], WithDecryption=True)
    values = {p["Name"]: p["Value"] for p in response["Parameters"]}
    require(set(values) == {name} and bool(values[name]), "media configuration unavailable")
    source = Provenance(
        source_observation_id="synthetic-smoke",
        actor_kind="doctor",
        actor_id="synthetic-doctor",
        source_kind="doctor_statement",
        received_at=datetime.now(UTC),
    )
    adapter = SpeechAdapter(
        GeminiCaller("11M-smoke", values[name], timeout=30), FFmpegConverter(), source
    )
    result = asyncio.run(
        adapter.transcribe_converted(
            ConvertedAudio(data=synthetic_english_clip(), duration=15), expected_language="en"
        )
    )
    require(isinstance(result, Transcript), "media smoke unavailable")
    assert isinstance(result, Transcript)
    require("5" in result.numbers, "media smoke did not recover the synthetic number")
    return {
        "ok": True,
        "latency_ms": round(result.metadata.latency_ms, 2),
        "input_tokens": result.metadata.input_tokens if result.metadata.usage_known else None,
        "output_tokens": result.metadata.output_tokens if result.metadata.usage_known else None,
    }


def run_smoke(aws: Any, env: str, out: dict[str, str], revision: str) -> dict[str, Any]:
    values, _ = parameter_values(client(aws, "ssm"), env)
    url = out["FunctionUrl"].rstrip("/")
    checks = {}
    with httpx.Client(trust_env=False, timeout=35) as http:
        functions: dict[str, Callable[[], dict[str, Any]]] = {
            "gemini": lambda: gemini(aws, env),
            "health": lambda: health(http, url, revision),
            "tick": lambda: tick(aws, env, http, url, values["tick-secret"]),
            "webhook": lambda: webhook(aws, http, url, out["TableName"], values),
            "tenant": lambda: tenant(aws, out["TableName"], url),
            "enrollment": lambda: enrollment(aws, http, url, out["TableName"], values),
            "session": lambda: browser_session(http, url),
            "cost": lambda: cost(aws, out),
        }
        for name, fn in functions.items():
            try:
                checks[name] = dict(status="PASSED", **fn())
            except Exception as error:
                reason = str(error) if isinstance(error, OperationError) else type(error).__name__
                checks[name] = {"status": "FAILED", "reason": reason}
            print("smoke", name, json.dumps(checks[name], sort_keys=True, default=str))
    try:
        checks["health"]["lambda_errors_last_15_minutes"] = app_errors(aws, out["AppFunctionName"])
        checks["health"]["scan_requests_last_15_minutes"] = scan_requests(aws, out["TableName"])
    except Exception as error:
        checks["health"].update(
            status="FAILED",
            reason=str(error) if isinstance(error, OperationError) else type(error).__name__,
        )
    return checks


def scan_requests(aws: Any, table_name: str) -> float:
    now = datetime.now(UTC)
    result = client(aws, "cloudwatch").get_metric_statistics(
        Namespace="AWS/DynamoDB",
        MetricName="SuccessfulRequestLatency",
        Dimensions=[
            {"Name": "TableName", "Value": table_name},
            {"Name": "Operation", "Value": "Scan"},
        ],
        StartTime=now - timedelta(minutes=15),
        EndTime=now,
        Period=60,
        Statistics=["SampleCount"],
    )
    count = float(sum(p["SampleCount"] for p in result["Datapoints"]))
    require(count <= 5, "DynamoDB Scan requests exceed 5 in the last 15 minutes")
    return count


def app_errors(aws: Any, function_name: str) -> float:
    now = datetime.now(UTC)
    result = client(aws, "cloudwatch").get_metric_statistics(
        Namespace="AWS/Lambda",
        MetricName="Errors",
        Dimensions=[{"Name": "FunctionName", "Value": function_name}],
        StartTime=now - timedelta(minutes=15),
        EndTime=now,
        Period=60,
        Statistics=["Sum"],
    )
    require(bool(result["Datapoints"]), "App Lambda error count is not yet measurable")
    count = float(sum(p["Sum"] for p in result["Datapoints"]))
    require(count == 0, "App Lambda errors in the last 15 minutes")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    environment(parser)
    args = parser.parse_args()
    aws = session()
    out = outputs(client(aws, "cloudformation"), args.env)
    results = run_smoke(aws, args.env, out, out["ImageDigest"])
    if any(r["status"] == "FAILED" for r in results.values()):
        raise OperationError("Smoke failed")


if __name__ == "__main__":
    command(main)
