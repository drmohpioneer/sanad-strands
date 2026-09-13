"""Paginated operator clients; no method can fall through to a real AWS client."""

import copy
import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from boto3.dynamodb.types import TypeSerializer  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from deploy.common import ROOT

NOW = datetime(2026, 9, 12, 8, tzinfo=UTC)
IMAGE = "sha256:" + "a" * 64
PRIOR = "sha256:" + "b" * 64
SHA = "c" * 40


def item(pk: str, sk: str, **body: Any) -> dict[str, Any]:
    return {
        "PK": {"S": pk},
        "SK": {"S": sk},
        "version": {"N": str(body.get("version", 1))},
        "body": {"S": json.dumps(body)},
    }


def key(row: dict[str, Any]) -> tuple[str, str]:
    return row["PK"]["S"], row["SK"]["S"]


def missing() -> ClientError:
    return ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "DescribeTable")


class Paginator:
    def __init__(self, aws: "FakeAWS", name: str):
        self.aws, self.name = aws, name

    def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        self.aws.calls.append((self.name, kwargs))
        values: list[dict[str, Any]]
        if self.name == "list_objects_v2":
            values = [{"Key": k} for k in self.aws.media if k.startswith(kwargs["Prefix"])]
            field = "Contents"
        elif self.name == "filter_log_events":
            values = self.aws.logs.get(kwargs["logGroupName"], [])
            field = "events"
        elif self.name == "describe_alarms":
            values = [
                {
                    "AlarmName": name,
                    "StateValue": self.aws.alarm_state,
                    "StateReason": "private@example.test token=hidden",
                }
                for name in kwargs["AlarmNames"]
            ]
            field = "MetricAlarms"
        elif self.name == "describe_log_groups":
            values = [{"logGroupName": kwargs["logGroupNamePrefix"], "retentionInDays": 30}]
            field = "logGroups"
        else:
            raise AssertionError(self.name)
        yield {field: []}
        for value in values:
            yield {field: [value]}


class FakeAWS:
    def __init__(self) -> None:
        self.tables: dict[str, dict[tuple[str, str], dict[str, Any]]] = {"sanad-dev-data": {}}
        self.tags: dict[str, list[dict[str, str]]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.media: dict[str, bytes] = {}
        self.objects: list[dict[str, str]] = []
        self.logs: dict[str, list[dict[str, Any]]] = {}
        self.alarm_state = "OK"
        self.errors: list[dict[str, Any]] = [{"Sum": 0}, {"Sum": 2}]
        self.table_name = "sanad-dev-data"
        self.s3_failure = False
        self.reappear: dict[str, Any] | None = None
        self.parameters = {
            "/sanad/dev/bot-token": "1234:do-not-print-this-token",
            "/sanad/dev/admin-telegram-id": "10001",
            "/sanad/dev/budget-email": "private@example.test",
        }

    def client(self, service: str, **kwargs: Any) -> "FakeAWS":
        assert service in {
            "dynamodb",
            "s3",
            "ssm",
            "cloudformation",
            "logs",
            "cloudwatch",
            "budgets",
        }
        return self

    def add(self, row: dict[str, Any], table: str = "sanad-dev-data") -> None:
        self.tables.setdefault(table, {})[key(row)] = row

    def add_model(self, model: Any, scope: Any, table: str = "sanad-dev-data") -> None:
        from sanad.store.records import record_item, to_record

        serializer = TypeSerializer()
        self.add(
            {k: serializer.serialize(v) for k, v in record_item(to_record(model, scope)).items()},
            table,
        )

    def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"StackName": "sanad-dev"}
        out = {
            "TableName": self.table_name,
            "BucketName": "synthetic-bucket",
            "ImageDigest": IMAGE,
            "AppFunctionName": "sanad-dev-app",
            "RelayFunctionName": "sanad-dev-relay",
        }
        return {
            "Stacks": [{"Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in out.items()]}]
        }

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["ConsistentRead"]
        self.calls.append(("scan", kwargs))
        rows = sorted(self.tables[kwargs["TableName"]].values(), key=key)
        if kwargs.get("ExclusiveStartKey"):
            rows = [r for r in rows if key(r) > key(kwargs["ExclusiveStartKey"])]
        page = rows[:3]
        field = kwargs["ExpressionAttributeNames"]["#field"]
        value = kwargs["ExpressionAttributeValues"][":value"]["S"]
        selected = [
            r
            for r in page
            if (
                r[field]["S"].startswith(value)
                if kwargs["FilterExpression"].startswith("begins_with")
                else r[field]["S"] == value
            )
        ]
        return {
            "Items": copy.deepcopy(selected),
            **(
                {"LastEvaluatedKey": {k: page[-1][k] for k in ("PK", "SK")}}
                if len(rows) > 3
                else {}
            ),
        }

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["ConsistentRead"]
        row = self.tables[kwargs["TableName"]].get(key(kwargs["Key"]))
        return {"Item": row} if row else {}

    def put_item(self, **kwargs: Any) -> None:
        assert kwargs["ConditionExpression"] == "attribute_not_exists(PK)"
        self.calls.append(("put", kwargs))
        assert key(kwargs["Item"]) not in self.tables[kwargs["TableName"]]
        self.add(kwargs["Item"], kwargs["TableName"])

    def describe_table(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["TableName"]
        if name not in self.tables:
            raise missing()
        return {"Table": {"TableArn": name, "TableStatus": "ACTIVE"}}

    def restore_table_to_point_in_time(self, **kwargs: Any) -> None:
        assert kwargs["SourceTableName"] == "sanad-dev-data"
        assert kwargs["TargetTableName"] not in self.tables
        self.calls.append(("restore", kwargs))
        self.tables[kwargs["TargetTableName"]] = copy.deepcopy(self.tables["sanad-dev-data"])

    def tag_resource(self, **kwargs: Any) -> None:
        self.calls.append(("tag", kwargs))
        self.tags[kwargs["ResourceArn"]] = kwargs["Tags"]

    def list_tags_of_resource(self, **kwargs: Any) -> dict[str, Any]:
        return {"Tags": self.tags.get(kwargs["ResourceArn"], [])}

    def delete_table(self, **kwargs: Any) -> None:
        name = kwargs["TableName"]
        assert name.startswith("sanad-dev-data-restore-") and self.tags[name]
        self.calls.append(("delete-table", name))
        del self.tables[name]

    def get_waiter(self, name: str) -> "FakeAWS":
        assert name in {"table_exists", "table_not_exists"}
        return self

    def wait(self, **kwargs: Any) -> None:
        self.calls.append(("wait", kwargs))

    def get_parameters(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "Parameters": [
                {"Name": name, "Value": value, "Version": 1, "LastModifiedDate": NOW}
                for name, value in self.parameters.items()
                if name in kwargs["Names"]
            ]
        }

    def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
        objects = sorted(
            [o for o in self.objects if o["Key"].startswith(kwargs["Prefix"])],
            key=lambda o: (o["Key"], o["VersionId"]),
        )
        if "KeyMarker" in kwargs:
            marker = (kwargs["KeyMarker"], kwargs["VersionIdMarker"])
            objects = [o for o in objects if (o["Key"], o["VersionId"]) > marker]
        page = objects[:2]
        return {
            "Versions": [o for o in page if o["VersionId"] != "marker"],
            "DeleteMarkers": [o for o in page if o["VersionId"] == "marker"],
            "IsTruncated": len(objects) > 2,
            **(
                {"NextKeyMarker": page[-1]["Key"], "NextVersionIdMarker": page[-1]["VersionId"]}
                if len(objects) > 2
                else {}
            ),
        }

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("s3-delete", kwargs))
        if self.s3_failure:
            return {"Errors": [{"Code": "AccessDenied"}]}
        for obj in kwargs["Delete"]["Objects"]:
            self.objects.remove(obj)
        return {}

    def batch_write_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("ddb-delete", kwargs))
        for table, requests in kwargs["RequestItems"].items():
            for req in requests:
                self.tables[table].pop(key(req["DeleteRequest"]["Key"]), None)
        if self.reappear:
            self.add(self.reappear)
        return {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        return {"Body": io.BytesIO(self.media[kwargs["Key"]])}

    def get_paginator(self, name: str) -> Paginator:
        return Paginator(self, name)

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["Namespace"] == "AWS/DynamoDB":
            assert kwargs["MetricName"] in {
                "ConsumedReadCapacityUnits",
                "ConsumedWriteCapacityUnits",
                "SuccessfulRequestLatency",
            }
            assert (kwargs["EndTime"] - kwargs["StartTime"]).total_seconds() == 3600
            self.calls.append(("metrics", kwargs))
            return {"Datapoints": [{kwargs["Statistics"][0]: 0}]}
        assert kwargs["Namespace"] == "AWS/Lambda" and kwargs["MetricName"] in {
            "Errors",
            "Invocations",
            "Duration",
        }
        assert kwargs["Statistics"] == ["Sum"]
        assert (kwargs["EndTime"] - kwargs["StartTime"]).total_seconds() == (
            3600 if kwargs["MetricName"] == "Errors" else 86400
        )
        self.calls.append(("metrics", kwargs))
        return {"Datapoints": self.errors}

    def describe_budget(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "Budget": {
                "BudgetLimit": {"Amount": "20", "Unit": "USD"},
                "CalculatedSpend": {"ActualSpend": {"Amount": "3.5", "Unit": "USD"}},
                "Subscribers": ["private@example.test"],
            }
        }

    def describe_continuous_backups(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "ContinuousBackupsDescription": {
                "PointInTimeRecoveryDescription": {
                    "PointInTimeRecoveryStatus": "ENABLED",
                    "LatestRestorableDateTime": NOW,
                }
            }
        }


def release_root(path: Path, aws: FakeAWS) -> Path:
    (path / "deploy/releases").mkdir(parents=True)
    (path / "docs/evidence").mkdir(parents=True)
    (path / "deploy/stack.yaml").write_text((ROOT / "deploy/stack.yaml").read_text())
    (path / "docs/backlog.md").write_text("| Policy review | owner | before row 21 |\n")
    (path / "deploy/releases/dev.json").write_text(
        json.dumps(
            [
                {
                    "image_digest": PRIOR,
                    "git_sha": "d" * 40,
                    "status": "passed",
                    "schema_version": 1,
                },
                {"image_digest": IMAGE, "git_sha": SHA, "status": "passed", "schema_version": 1},
            ]
        )
    )
    aws.add({"PK": {"S": "META"}, "SK": {"S": "schema_version"}, "schema_version": {"N": "1"}})
    return path
