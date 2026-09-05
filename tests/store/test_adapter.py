import socket
from pathlib import Path
from typing import Any

import pytest
import pytest_socket

from sanad.store._base import Check, StoreBase, Write
from sanad.store.dynamodb import DynamoStore
from sanad.store.dynamodb_local import DynamoDBLocal
from sanad.store.keys import Key
from store.conftest import require_local


def test_missing_local_tools_skip_explicit_opt_in_but_fail_required_run(tmp_path: Path) -> None:
    server = DynamoDBLocal(repo_root=tmp_path)
    assert not server.available and server.process is None
    with pytest.raises(FileNotFoundError, match=".tools/"):
        server.__enter__()
    with pytest.raises(pytest.skip.Exception, match="DynamoDB Local missing"):
        require_local(server, required=False)
    with pytest.raises(pytest.fail.Exception, match="DynamoDB Local missing"):
        require_local(server, required=True)


def test_local_fixture_only_allows_loopback_dns(loopback_network: None) -> None:
    assert socket.getaddrinfo("127.0.0.1", 8000)
    with (
        pytest.warns(UserWarning, match="loopback"),
        pytest.raises(pytest_socket.SocketBlockedError),
    ):
        socket.getaddrinfo("synthetic.invalid", 443)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.warns(UserWarning), pytest.raises(pytest_socket.SocketConnectBlockedError):
            sock.connect(("192.0.2.1", 443))


class CapturedClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        return {}

    def transact_write_items(self, **kwargs: Any) -> None:
        self.calls.append(("transaction", kwargs))

    def update_item(self, **kwargs: Any) -> None:
        self.calls.append(("update", kwargs))


def test_dynamodb_uses_injected_client_strong_reads_and_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "synthetic-must-not-be-opened")
    client = CapturedClient()
    store = DynamoStore(client, "synthetic-table")
    assert client.calls == []
    assert store._read(Key("synthetic-pk", "synthetic-sk")) is None
    assert client.calls[-1][1]["ConsistentRead"] is True
    writes = [
        Write({"PK": "synthetic-pk", "SK": "new", "version": 1}, None),
        Write({"PK": "synthetic-pk", "SK": "updated", "version": 2}, 1),
    ]
    assert store._atomic(writes, [Check(Key("synthetic-pk", "guard"), 3)])
    transaction = client.calls[-1][1]["TransactItems"]
    assert transaction[0]["Put"]["ConditionExpression"] == "attribute_not_exists(#pk)"
    assert transaction[1]["Put"]["ExpressionAttributeValues"] == {":before": {"N": "1"}}
    assert transaction[2]["ConditionCheck"]["ExpressionAttributeValues"] == {":before": {"N": "3"}}
    assert store._update({"PK": "synthetic-pk", "SK": "lease", "version": 3, "body": "{}"}, 2)
    assert client.calls[-1][0] == "update"
    assert client.calls[-1][1]["ConditionExpression"] == "#version = :before"


def test_unexpected_backend_failure_remains_visible(
    store: StoreBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domain_fixtures import mission

    from sanad.store.records import to_record
    from store.fixtures import SCOPE, request

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic storage outage")

    monkeypatch.setattr(store, "_read", unavailable)
    with pytest.raises(RuntimeError, match="synthetic storage outage"):
        store.commit(request(to_record(mission(), SCOPE)))
