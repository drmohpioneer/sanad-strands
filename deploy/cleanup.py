"""Remove the explicitly replaced spike, or an already-deleted stack's retained bucket."""

import argparse
import json
import os
import time
from collections import Counter
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, unquote

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


def absent_ok(call: Any, **kwargs: Any) -> Any:
    try:
        return call(**kwargs)
    except ClientError as error:
        if error.response["Error"]["Code"] not in {
            "ResourceNotFoundException",
            "NoSuchEntity",
            "NoSuchBucket",
            "RepositoryNotFoundException",
        }:
            raise
        return None


def empty_bucket(s3: Any, bucket: str) -> int:
    count = 0
    # Re-list from the start after each deletion; never rely on a deleted version's cursor.
    while True:
        response = absent_ok(s3.list_object_versions, Bucket=bucket, MaxKeys=1000)
        if response is None:
            return count
        objects = [
            {"Key": x["Key"], "VersionId": x["VersionId"]}
            for x in response.get("Versions", []) + response.get("DeleteMarkers", [])
        ]
        if not objects:
            break
        deleted = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
        if deleted.get("Errors"):
            raise OperationError("Bucket version deletion failed; bucket retained")
        count += len(objects)
    while True:
        uploads = s3.list_multipart_uploads(Bucket=bucket).get("Uploads", [])
        if not uploads:
            return count
        for upload in uploads:
            s3.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])


def delete_spikes(aws: Any) -> dict[str, Any]:
    events = client(aws, "events")
    targets = absent_ok(events.list_targets_by_rule, Rule="sanad-spike-tick")
    if targets and targets.get("Targets"):
        result = events.remove_targets(
            Rule="sanad-spike-tick", Ids=[t["Id"] for t in targets["Targets"]]
        )
        if result.get("FailedEntryCount"):
            raise OperationError("Spike tick targets could not be removed")
    absent_ok(events.delete_rule, Name="sanad-spike-tick")
    lam = client(aws, "lambda")
    absent_ok(lam.delete_function_url_config, FunctionName="sanad-spike")
    absent_ok(lam.delete_function, FunctionName="sanad-spike")
    absent_ok(client(aws, "codebuild").delete_project, name="sanad-spike-build")
    absent_ok(client(aws, "ecr").delete_repository, repositoryName="sanad-spike", force=True)
    iam = client(aws, "iam")
    for name in ("sanad-spike-lambda", "sanad-spike-codebuild"):
        inline = absent_ok(iam.list_role_policies, RoleName=name)
        if inline is None:
            continue
        for policy in inline["PolicyNames"]:
            iam.delete_role_policy(RoleName=name, PolicyName=policy)
        for policy in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=name, PolicyArn=policy["PolicyArn"])
        iam.delete_role(RoleName=name)
    s3 = client(aws, "s3")
    bucket = "sanad-spike-" + os.environ["AWS_ACCOUNT_ID"]
    versions = empty_bucket(s3, bucket)
    absent_ok(s3.delete_bucket, Bucket=bucket)
    logs = client(aws, "logs")
    for name in ("/aws/lambda/sanad-spike", "/aws/codebuild/sanad-spike-build"):
        absent_ok(logs.delete_log_group, logGroupName=name)
    return {
        "removed_or_already_absent": [
            "sanad-spike Lambda and function URL",
            "sanad-spike-tick rule and targets",
            "sanad-spike-build CodeBuild project",
            "sanad-spike ECR repository and images",
            "sanad-spike-lambda role and policies",
            "sanad-spike-codebuild role and policies",
            "sanad-spike-<owner-account> bucket",
            "/aws/lambda/sanad-spike log group",
            "/aws/codebuild/sanad-spike-build log group",
        ],
        "bucket_versions_and_delete_markers_removed": versions,
    }


def scan_rows(
    ddb: Any, table: str, field: str, value: str, *, prefix: bool = True
) -> Iterator[dict[str, Any]]:
    """DynamoDB filters run before client-side decoding of the stored JSON body."""
    request: dict[str, Any] = {
        "TableName": table,
        "ConsistentRead": True,
        "FilterExpression": "begins_with(#field, :value)" if prefix else "#field = :value",
        "ExpressionAttributeNames": {"#field": field},
        "ExpressionAttributeValues": {":value": {"S": value}},
    }
    while True:
        page = ddb.scan(**request)
        yield from page.get("Items", [])
        if not page.get("LastEvaluatedKey"):
            return
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def row_body(row: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(row.get("body", {}).get("S", "{}"))
    if not isinstance(value, dict):
        raise OperationError("Invalid stored record body; no cleanup performed")
    return value


def dev_selection(ddb: Any, table: str, bot: str) -> tuple[list[dict[str, Any]], set[str]]:
    doctors = {
        row["PK"]["S"]
        for row in scan_rows(ddb, table, "SK", "DOCTOR", prefix=False)
        if row_body(row).get("telegram_bot_id") == bot
    }
    doctor_ids = {unquote(pk.removeprefix("D#")) for pk in doctors}
    selected: list[dict[str, Any]] = []
    prefixes: set[str] = set()
    account = "ACCT#" + quote(bot, safe="-_.:")
    for pk_prefix in (
        "D#",
        account,
        "SUBJECT#" + quote(bot, safe="-_.:") + "#",
        "TOKEN#",
        "SESSION#",
        "IN#",
    ):
        for row in scan_rows(ddb, table, "PK", pk_prefix):
            pk, sk = row["PK"]["S"], row["SK"]["S"]
            # Always keep shared vocabulary caches, regardless of partition class.
            if (
                sk == "NAME_CACHE"
                or sk.startswith("NAME_CACHE#")
                or row.get("entity_type", {}).get("S") == "name_cache"
            ):
                continue
            take = False
            if pk.startswith("D#"):
                parts = pk.split("#")
                tenant = "#".join(parts[:2])
                if tenant not in doctors:
                    continue
                if len(parts) == 4 and parts[2] in {"P", "INTAKE"}:
                    owner, subject = (
                        quote(unquote(parts[1]), safe=""),
                        quote(unquote(parts[3]), safe=""),
                    )
                    prefixes.add(
                        f"{owner}/" + ("intake/" if parts[2] == "INTAKE" else "") + f"{subject}/"
                    )
                    take = True
                elif pk == tenant:
                    take = sk not in {"DOCTOR", "PROFILE"} and not sk.startswith(
                        ("POLICY#", "APPLICATION#")
                    )
            elif pk == account:
                take = not sk.startswith(("ADMIN_ACCOUNT#", "APPLICATION#", "ACK#"))
                if sk.startswith("TOKEN_HEAD#") and row_body(row).get("purpose") in {
                    "doctor_login",
                    "admin_login",
                }:
                    take = False
            elif pk.startswith("SUBJECT#"):
                take = set(row_body(row).get("role_set", [])) == {"patient"}
            elif pk.startswith(("TOKEN#", "SESSION#", "IN#")):
                scope = row_body(row).get("scope", {})
                if pk.startswith("IN#"):
                    take = scope.get("doctor_id") in doctor_ids or scope.get("bot_id") == bot
                else:
                    take = scope.get("bot_id") == bot
            if take:
                selected.append(row)
    return selected, prefixes


def versioned_objects(s3: Any, bucket: str, prefix: str) -> list[dict[str, str]]:
    objects: list[dict[str, str]] = []
    request: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
    while True:
        page = s3.list_object_versions(**request)
        objects.extend(
            {"Key": x["Key"], "VersionId": x["VersionId"]}
            for x in page.get("Versions", []) + page.get("DeleteMarkers", [])
        )
        if not page.get("IsTruncated"):
            return objects
        request["KeyMarker"] = page["NextKeyMarker"]
        if "NextVersionIdMarker" in page:
            request["VersionIdMarker"] = page["NextVersionIdMarker"]
        else:
            request.pop("VersionIdMarker", None)


def selection_counts(rows: list[dict[str, Any]], objects: int) -> dict[str, Any]:
    partitions: Counter[str] = Counter()
    sk_prefixes: dict[str, Counter[str]] = {}
    globals_: Counter[str] = Counter()
    for row in rows:
        pk, sk = row["PK"]["S"], row["SK"]["S"]
        parts = pk.split("#")
        kind = (
            ("patient" if parts[2] == "P" else "intake")
            if len(parts) == 4 and parts[0] == "D"
            else "doctor"
            if parts[0] == "D"
            else "account"
            if parts[0] == "ACCT"
            else "global"
        )
        partitions[kind] += 1
        sk_prefixes.setdefault(kind, Counter())[sk.split("#", 1)[0]] += 1
        if kind == "global":
            globals_[parts[0] + "#"] += 1
    return {
        "rows": len(rows),
        "partition_classes": dict(partitions),
        "sk_prefixes": {k: dict(v) for k, v in sk_prefixes.items()},
        "global_pk_prefixes": dict(globals_),
        "s3_objects": objects,
    }


def delete_rows(ddb: Any, table: str, rows: list[dict[str, Any]]) -> None:
    for start in range(0, len(rows), 25):
        pending = [
            {"DeleteRequest": {"Key": {k: row[k] for k in ("PK", "SK")}}}
            for row in rows[start : start + 25]
        ]
        for attempt in range(8):
            result = ddb.batch_write_item(RequestItems={table: pending})
            pending = result.get("UnprocessedItems", {}).get(table, [])
            if not pending:
                break
            if attempt == 7:
                raise OperationError("Unprocessed row deletions remain; rerun dev-data")
            time.sleep(min(0.1 * 2**attempt, 2))


def due_leftovers(ddb: Any, table: str, selected: list[dict[str, Any]], bot: str) -> dict[str, Any]:
    """Read-only census of projected due keys; never widen reset selection."""
    removed = {(row["PK"]["S"], row["SK"]["S"]) for row in selected}
    counts: dict[str, dict[str, int]] = {}
    receipts = 0
    surviving_receipts = 0
    rows = list(scan_rows(ddb, table, "PK", ""))
    doctors = {
        row_body(row).get("scope", {}).get("doctor_id")
        for row in rows
        if row["SK"]["S"] == "DOCTOR" and row_body(row).get("telegram_bot_id") == bot
    }
    for row in rows:
        if (row["PK"]["S"], row["SK"]["S"]) in removed:
            continue
        body = row_body(row)
        scope = body.get("scope") or {}
        if body.get("entity_type") == "inbound_receipt" and (
            scope.get("bot_id") == bot
            or (scope.get("doctor_id") is not None and scope.get("doctor_id") in doctors)
        ):
            surviving_receipts += 1
        lane = row.get("due_lane_shard", {}).get("S")
        if not lane or not row.get("due_sort"):
            continue
        pk = row["PK"]["S"]
        kind = (
            "account"
            if pk.startswith("ACCT#")
            else "operational"
            if pk.startswith("OPS#")
            else "patient"
            if "#P#" in pk
            else "intake"
            if "#INTAKE#" in pk
            else "doctor"
            if pk.startswith("D#")
            else "global"
        )
        by_class = counts.setdefault(lane, {})
        by_class[kind] = by_class.get(kind, 0) + 1
        if row_body(row).get("entity_type") == "inbound_receipt":
            receipts += 1
    return {
        "by_lane_partition_class": counts,
        "surviving_due_receipts": receipts,
        "surviving_current_bot_receipts": surviving_receipts,
    }


def dev_data(aws: Any, env: str, *, yes: bool = False) -> dict[str, Any]:
    if env != "dev":
        raise OperationError("dev-data requires --env dev")
    out = outputs(client(aws, "cloudformation"), env)
    if out.get("TableName") != "sanad-dev-data":
        raise OperationError("dev-data requires stack TableName sanad-dev-data")
    values, _ = parameter_values(client(aws, "ssm"), env)
    token = values.get("bot-token", "")
    bot, separator, secret = token.partition(":")
    if not bot.isascii() or not bot.isdigit() or not separator or not secret:
        raise OperationError("Invalid dev bot parameter; cleanup refused")
    ddb, s3 = client(aws, "dynamodb"), client(aws, "s3")
    rows, prefixes = dev_selection(ddb, out["TableName"], bot)
    objects = [
        obj
        for prefix in sorted(prefixes)
        for obj in versioned_objects(s3, out["BucketName"], prefix)
    ]
    objects = list({(obj["Key"], obj["VersionId"]): obj for obj in objects}.values())
    counts = selection_counts(rows, len(objects))
    counts["due_leftovers"] = due_leftovers(ddb, out["TableName"], rows, bot)
    print(json.dumps({"mode": "execute" if yes else "dry-run", **counts}, sort_keys=True))
    if yes:
        # Leave every scope row intact until all its media prefixes have been cleared.
        for start in range(0, len(objects), 1000):
            result = s3.delete_objects(
                Bucket=out["BucketName"],
                Delete={"Objects": objects[start : start + 1000], "Quiet": True},
            )
            if result.get("Errors"):
                raise OperationError("S3 deletion failed; rows retained; rerun dev-data")
        delete_rows(ddb, out["TableName"], rows)
        remaining, _ = dev_selection(ddb, out["TableName"], bot)
        if remaining:
            print(json.dumps({"reappeared": selection_counts(remaining, 0)}, sort_keys=True))
            raise OperationError("Rows reappeared during cleanup; rerun dev-data")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("spikes")
    environment(sub.add_parser("retained-bucket"))
    reset = sub.add_parser("dev-data")
    reset.add_argument("--env", required=True, choices=("dev", "judge"))
    reset.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.action == "dev-data" and args.env != "dev":
        raise OperationError("dev-data requires --env dev")
    aws = session()
    if args.action == "dev-data":
        dev_data(aws, args.env, yes=args.yes)
    elif args.action == "spikes":
        print(json.dumps(delete_spikes(aws), sort_keys=True))
    else:
        try:
            client(aws, "cloudformation").describe_stacks(StackName=f"sanad-{args.env}")
        except ClientError as error:
            if (
                error.response["Error"]["Code"] != "ValidationError"
                or "does not exist" not in error.response["Error"]["Message"]
            ):
                raise
        else:
            raise OperationError("Delete the stack before removing its retained bucket")
        s3 = client(aws, "s3")
        bucket = f"sanad-{args.env}-{os.environ['AWS_ACCOUNT_ID']}-us-east-1"
        count = empty_bucket(s3, bucket)
        absent_ok(s3.delete_bucket, Bucket=bucket)
        print(f"Retained bucket removed; {count} object versions/delete markers removed")


if __name__ == "__main__":
    command(main)
