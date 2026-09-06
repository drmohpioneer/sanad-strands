"""DynamoDB single-table CAS adapter; client construction/configuration belongs to callers."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from sanad.store._base import (
    INDEX_FIELDS,
    Check,
    Item,
    StoreBase,
    Write,
    query_identity,
    size_failure,
    utc_now,
)
from sanad.store.keys import Key
from sanad.store.records import PROJECTION_FIELDS, Cursor

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()


def _encode(item: Item) -> Item:
    return {key: _SERIALIZER.serialize(value) for key, value in item.items()}


def _numbers(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_numbers(item) for item in value]
    return value


def _decode(item: Item) -> Item:
    return {key: _numbers(_DESERIALIZER.deserialize(value)) for key, value in item.items()}


def ensure_table(client: Any, table_name: str) -> None:
    """Idempotent table creation; this helper is explicitly invoked, never on adapter init."""
    try:
        client.describe_table(TableName=table_name)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        attributes = {"PK", "SK", *(field for pair in INDEX_FIELDS.values() for field in pair)}
        try:
            client.create_table(
                TableName=table_name,
                AttributeDefinitions=[
                    {"AttributeName": name, "AttributeType": "S"} for name in sorted(attributes)
                ],
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                BillingMode="PAY_PER_REQUEST",
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": name,
                        "KeySchema": [
                            {"AttributeName": fields[0], "KeyType": "HASH"},
                            {"AttributeName": fields[1], "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                    for name, fields in INDEX_FIELDS.items()
                ],
            )
        except ClientError as race:
            if race.response["Error"]["Code"] != "ResourceInUseException":
                raise
    client.get_waiter("table_exists").wait(
        TableName=table_name, WaiterConfig={"Delay": 1, "MaxAttempts": 30}
    )


class DynamoStore(StoreBase):
    def __init__(self, client: Any, table_name: str, *, clock: Callable[[], datetime] = utc_now):
        super().__init__(clock=clock)
        self._client = client
        self._table = table_name

    def _read(self, key: Key) -> Item | None:
        result = self._client.get_item(
            TableName=self._table, Key=_encode({"PK": key.pk, "SK": key.sk}), ConsistentRead=True
        )
        return _decode(result["Item"]) if "Item" in result else None

    @staticmethod
    def _condition(before: int | None) -> Item:
        if before is None:
            return {
                "ConditionExpression": "attribute_not_exists(#pk)",
                "ExpressionAttributeNames": {"#pk": "PK"},
            }
        return {
            "ConditionExpression": "#version = :before",
            "ExpressionAttributeNames": {"#version": "version"},
            "ExpressionAttributeValues": _encode({":before": before}),
        }

    def _atomic(self, writes: list[Write], checks: list[Check]) -> bool:
        if size_failure(writes, checks) is not None:
            return False
        transactions = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": _encode(write.item),
                    **self._condition(write.before),
                }
            }
            for write in writes
        ]
        transactions += [
            {
                "ConditionCheck": {
                    "TableName": self._table,
                    "Key": _encode({"PK": check.key.pk, "SK": check.key.sk}),
                    **self._condition(check.version),
                }
            }
            for check in checks
        ]
        try:
            self._client.transact_write_items(TransactItems=transactions)
            return True
        except ClientError as error:
            if error.response["Error"]["Code"] == "TransactionCanceledException":
                reasons = {r["Code"] for r in error.response.get("CancellationReasons", [])}
                if reasons & {"ConditionalCheckFailed", "TransactionConflict"} and not reasons - {
                    "None",
                    "ConditionalCheckFailed",
                    "TransactionConflict",
                }:
                    return False
            raise  # Provider, capacity and validation failures must stay visible.

    def _update(self, item: Item, before: int) -> bool:
        if size_failure([Write(item, before)], []) is not None:
            return False
        names = {"#version": "version"}
        values: Item = {":before": before}
        assignments = []
        for index, (key, value) in enumerate(item.items()):
            if key in {"PK", "SK"}:
                continue
            name = "#version" if key == "version" else f"#a{index}"
            names[name] = key
            values[f":v{index}"] = value
            assignments.append(f"{name} = :v{index}")
        removals = []
        for index, key in enumerate((*PROJECTION_FIELDS, "processing_claim")):
            if key not in item:
                name = f"#remove{index}"
                names[name] = key
                removals.append(name)
        expression = "SET " + ", ".join(assignments)
        if removals:
            expression += " REMOVE " + ", ".join(removals)
        try:
            self._client.update_item(
                TableName=self._table,
                Key=_encode({"PK": item["PK"], "SK": item["SK"]}),
                UpdateExpression=expression,
                ConditionExpression="#version = :before",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=_encode(values),
            )
            return True
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def _delete_nonce(self, key: Key, before: int) -> bool:
        if not key.pk.startswith("OPS#") or not key.sk.startswith("NONCE#"):
            return False
        try:
            self._client.delete_item(
                TableName=self._table,
                Key=_encode({"PK": key.pk, "SK": key.sk}),
                **self._condition(before),
            )
            return True
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def _query(
        self,
        pk: str,
        *,
        index: str | None = None,
        prefix: str = "",
        through: str | None = None,
        cursor: Cursor | None = None,
        limit: int = 100,
    ) -> tuple[list[Item], Cursor | None]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("page limit must be an integer between 1 and 1000")
        identity = query_identity(pk, index, prefix, through)
        if cursor is not None and cursor.query != identity:
            return [], None
        pk_field, sk_field = INDEX_FIELDS[index] if index else ("PK", "SK")
        names = {"#pk": pk_field}
        values: Item = {":pk": pk}
        condition = "#pk = :pk"
        if prefix:
            names["#sk"] = sk_field
            values[":prefix"] = prefix
            condition += " AND begins_with(#sk, :prefix)"
        if through is not None:
            names["#sk"] = sk_field
            values[":through"] = through
            condition += " AND #sk <= :through"
        args: Item = {
            "TableName": self._table,
            "KeyConditionExpression": condition,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": _encode(values),
            "Limit": limit,
            "ConsistentRead": index is None,
        }
        if index is not None:
            args["IndexName"] = index
        if cursor is not None:
            if (
                cursor.position.get(pk_field) != pk
                or sk_field not in cursor.position
                or set(cursor.position) != {"PK", "SK", pk_field, sk_field}
            ):
                return [], None
            args["ExclusiveStartKey"] = _encode(cursor.position)
        result = self._client.query(**args)
        last = result.get("LastEvaluatedKey")
        return (
            [_decode(item) for item in result["Items"]],
            Cursor(query=identity, position=_decode(last)) if last else None,
        )
