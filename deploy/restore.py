"""Restore dev PITR into a tagged isolated table; reconcile without a sending transport."""

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from deploy.cleanup import row_body, scan_rows, selection_counts
from deploy.common import (
    OperationError,
    client,
    command,
    dev_only,
    dev_outputs,
    oldest,
    operator_arguments,
    parameter_values,
    parse_instant,
    session,
)
from sanad.steward.dispatch import freshness
from sanad.store.dynamodb import DynamoStore
from sanad.store.records import IdentityConfig, OutboundIntent

CLASSES = ("account", "doctor", "patient", "intake", "global")


def restore_name(suffix: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", suffix):
        raise OperationError("Restore suffix must be 1-63 lowercase letters, digits or hyphens")
    return "sanad-dev-data-restore-" + suffix


class ReadOnlyClient:
    """The freshness store cannot perform writes, even if a future helper attempts one."""

    def __init__(self, ddb: Any):
        self.ddb = ddb

    def get_item(self, **kwargs: Any) -> Any:
        return self.ddb.get_item(**kwargs)

    def query(self, **kwargs: Any) -> Any:
        return self.ddb.query(**kwargs)

    def scan(self, **kwargs: Any) -> Any:
        return self.ddb.scan(**kwargs)


def partition_class(row: dict[str, Any]) -> str:
    return str(next(iter(selection_counts([row], 0)["partition_classes"])))


def table_summary(
    ddb: Any,
    table: str,
    rows: list[dict[str, Any]],
    now: datetime,
    settings: IdentityConfig,
) -> dict[str, Any]:
    store = DynamoStore(ReadOnlyClient(ddb), table, clock=lambda: now)
    result = {}
    for kind in CLASSES:
        selected = [row for row in rows if partition_class(row) == kind]
        inbound: list[str | None] = []
        due: list[str | None] = []
        eligible: Counter[str] = Counter()
        suppressed: Counter[str] = Counter()
        for row in selected:
            body = row_body(row)
            if body.get("entity_type") == "inbound_receipt" and body.get("state") != "completed":
                inbound.append(body["received_at"])
            clock = body.get("work_clock") or {}
            at = clock.get("next_action_at")
            if at and parse_instant(at) <= now:
                due.append(at)
                # Retried deliveries are persisted as queued, with retry counters;
                # failed/uncertain terminal rows are never candidates for a send.
                if row["SK"]["S"].startswith("OUT#") and body.get("status") == "queued":
                    intent = OutboundIntent.model_validate(body)
                    reason = freshness(store, intent, now, settings=settings)
                    if reason:
                        suppressed[reason] += 1
                    else:
                        eligible[intent.notification_purpose] += 1
        result[kind] = {
            "rows": len(selected),
            "oldest_unfinished_inbound": oldest(inbound, now),
            "oldest_due_work": oldest(due, now),
            "would_send_by_kind": dict(eligible),
            "suppressed_by_reason": dict(suppressed),
        }
    return result


def reconcile(
    ddb: Any,
    live: str,
    restored: str,
    now: datetime,
    settings: IdentityConfig,
) -> dict[str, Any]:
    left = list(scan_rows(ddb, live, "PK", ""))
    right = list(scan_rows(ddb, restored, "PK", ""))
    index = {(r["PK"]["S"], r["SK"]["S"]): r for r in left}
    differences: Counter[str] = Counter()
    for row in right:
        prior = index.get((row["PK"]["S"], row["SK"]["S"]))
        if prior is not None and prior.get("version") != row.get("version"):
            differences[partition_class(row)] += 1
    return {
        "reconciled_at": now.isoformat(),
        "sends_enabled": False,
        "live": table_summary(ddb, live, left, now, settings),
        "restored": table_summary(ddb, restored, right, now, settings),
        "differing_versions": {kind: differences[kind] for kind in CLASSES},
        "limitations": "Read-only consistent scans are not an atomic cross-table snapshot; "
        "eligibility uses each table's authority, not permission to resume or repoint it.",
    }


def tagged_table(ddb: Any, table: str, suffix: str) -> dict[str, Any]:
    if table != restore_name(suffix):
        raise OperationError("Refusing to target a non-restore table")
    description: dict[str, Any] = ddb.describe_table(TableName=table)["Table"]
    request = {"ResourceArn": description["TableArn"]}
    tags = []
    while True:
        page = ddb.list_tags_of_resource(**request)
        tags.extend(page.get("Tags", []))
        if not page.get("NextToken"):
            break
        request["NextToken"] = page["NextToken"]
    if {"Key": "sanad-restore", "Value": suffix} not in tags:
        raise OperationError("Restore ownership tag missing or mismatched; operation refused")
    return description


def restore(
    aws: Any,
    env: str,
    suffix: str,
    *,
    at: datetime | None = None,
    yes: bool = False,
    delete: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    dev_only(env)
    target = restore_name(suffix)
    out = dev_outputs(aws, env)
    now = now or datetime.now(UTC)
    at = at or now - timedelta(minutes=5)
    if at.tzinfo is None or at > now:
        raise OperationError("Restore time must be an aware instant no later than now")
    ddb = client(aws, "dynamodb")
    report: dict[str, Any] = {
        "mode": "execute" if yes else "dry-run",
        "action": "delete" if delete else "restore",
        "source": out["TableName"],
        "target": target,
        "restore_at": at.isoformat(),
        "tables_to_delete": int(delete),
        "tables_to_restore": int(not delete),
    }
    if delete:
        tagged_table(ddb, target, suffix)
        print(json.dumps(report, sort_keys=True))
        if yes:
            ddb.delete_table(TableName=target)
            ddb.get_waiter("table_not_exists").wait(TableName=target)
        return report
    try:
        ddb.describe_table(TableName=target)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    else:
        raise OperationError("Restore target already exists; refusing to overwrite or adopt it")
    print(json.dumps(report, sort_keys=True))
    if not yes:
        return report
    values, _ = parameter_values(client(aws, "ssm"), env)
    settings = IdentityConfig(
        bot_id=values["bot-token"].split(":", 1)[0], admin_user_id=values["admin-telegram-id"]
    )
    ddb.restore_table_to_point_in_time(
        SourceTableName=out["TableName"],
        TargetTableName=target,
        RestoreDateTime=at,
    )
    ddb.get_waiter("table_exists").wait(
        TableName=target, WaiterConfig={"Delay": 20, "MaxAttempts": 180}
    )
    arn = ddb.describe_table(TableName=target)["Table"]["TableArn"]
    ddb.tag_resource(ResourceArn=arn, Tags=[{"Key": "sanad-restore", "Value": suffix}])
    tagged_table(ddb, target, suffix)
    # Reconciliation instant is after restore completion, not the pre-restore start.
    report["reconciliation"] = reconcile(ddb, out["TableName"], target, datetime.now(UTC), settings)
    print(json.dumps(report, sort_keys=True))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    operator_arguments(parser)
    parser.add_argument("--into", required=True)
    parser.add_argument("--at", type=parse_instant)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    dev_only(args.env)
    restore_name(args.into)
    if args.delete and args.at:
        raise OperationError("--at is not applicable to --delete")
    restore(session(), args.env, args.into, at=args.at, yes=args.yes, delete=args.delete)


if __name__ == "__main__":
    command(main)
