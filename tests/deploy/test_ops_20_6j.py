"""Operator counts read the same projected store rows without widening reset."""

from copy import deepcopy
from typing import Any

import pytest
from boto3.dynamodb.types import TypeSerializer  # type: ignore[import-untyped]
from handover_fakes import FakeAWS as HealthAWS
from store.conftest import Clock
from store.conftest import clock as clock
from store.conftest import ddb_server as ddb_server
from store.conftest import pytest_generate_tests as pytest_generate_tests
from store.conftest import store as store
from store.scribe_fixtures import ScribeWorld
from store.test_sweep_20_6j import intent, put, quiet
from test_dev_data import FakeAWS, key
from test_dev_data import item as fake_item

from deploy import cleanup, ops
from sanad.store._base import StoreBase, Write
from sanad.store.dynamodb import DynamoStore
from sanad.store.memory import MemoryStore


class StoreScan:
    def __init__(self, store: StoreBase):
        self.store = store

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        if isinstance(self.store, DynamoStore):
            return dict(self.store._client.scan(**(kwargs | {"TableName": self.store._table})))
        assert isinstance(self.store, MemoryStore)
        aws = FakeAWS()
        serializer = TypeSerializer()
        aws.rows = {
            key(value): value
            for value in (
                {k: serializer.serialize(v) for k, v in row.items()}
                for row in self.store._items.values()
            )
        }
        return aws.scan(**kwargs)


def test_f26_health_and_cleanup_read_only_leftovers(
    store: StoreBase,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = ScribeWorld.create(store, clock)
    world.approve(language="en")
    quiet(world)
    rows = [intent(world, i, foreign=f"foreign-{i // 2}") for i in range(6)]
    own = intent(world, 10)
    for row in [*rows, own]:
        put(store, row)
    # An unrelated catalog key remains outside selection as before.
    assert store._atomic(
        [Write({"PK": "META", "SK": "catalog", "version": 1, "body": "{}"}, None)], []
    )
    client = StoreScan(store)
    selected, prefixes = cleanup.dev_selection(
        client, "sanad-dev-data", world.runtime.settings.bot_id
    )
    before = deepcopy(selected), prefixes.copy()
    assert all(
        key(r)[0] not in {"ACCT#foreign-0", "ACCT#foreign-1", "ACCT#foreign-2", "META"}
        for r in selected
    )
    assert world.doctor.id in " ".join(key(r)[0] for r in selected)
    writes = []
    original = store._atomic

    def capture(*args: Any, **kwargs: Any) -> bool:
        writes.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_atomic", capture)
    leftovers = cleanup.due_leftovers(
        client, "sanad-dev-data", selected, world.runtime.settings.bot_id
    )
    assert leftovers == {
        "by_lane_partition_class": {"delivery#0": {"account": 6}},
        "surviving_due_receipts": 0,
        "surviving_current_bot_receipts": 0,
    }
    assert cleanup.dev_selection(client, "sanad-dev-data", world.runtime.settings.bot_id) == before
    aws = HealthAWS()
    aws.parameters["/sanad/dev/bot-token"] = world.runtime.settings.bot_id + ":synthetic"
    base_client = aws.client
    monkeypatch.setattr(
        aws,
        "client",
        lambda service, **kwargs: (
            client if service == "dynamodb" else base_client(service, **kwargs)
        ),
    )
    report = ops.health_report(aws, "dev", now=clock())
    assert rows[0].work_clock
    assert report["unowned_due_by_lane"]["delivery#0"] == {
        "count": 6,
        "oldest_due": rows[0]
        .work_clock.next_action_at.isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
    }
    assert writes == []
    assert "mode" not in leftovers and "fresh" not in str(leftovers)


def test_cleanup_dry_run_preserves_original_selection_and_reports_three_foreign_bots() -> None:
    aws = FakeAWS()
    before, prefixes = cleanup.dev_selection(aws, aws.table, aws.bot)
    for i in range(3):
        row = fake_item(f"ACCT#foreign-{i}", "OUT#left", entity_type="outbound_intent")
        row.update(due_lane_shard={"S": "delivery#0"}, due_sort={"S": "2026-09-01T00:00:00Z"})
        aws.add(row)
    selected, after_prefixes = cleanup.dev_selection(aws, aws.table, aws.bot)
    assert selected == before and after_prefixes == prefixes
    result = cleanup.dev_data(aws, "dev")
    assert result["due_leftovers"] == {
        "by_lane_partition_class": {"delivery#0": {"account": 3}},
        "surviving_due_receipts": 0,
        "surviving_current_bot_receipts": 0,
    }
    assert not any(name in {"ddb-delete", "s3-delete"} for name, _ in aws.calls)
