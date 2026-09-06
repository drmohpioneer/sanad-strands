from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

import pytest
from domain_fixtures import NOW
from harness import FakeClock

from sanad.store._base import StoreBase
from sanad.store.dynamodb import DynamoStore, ensure_table
from sanad.store.dynamodb_local import DynamoDBLocal
from sanad.store.memory import MemoryStore


@dataclass
class Clock(FakeClock):
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "store" in metafunc.fixturenames:
        backends = ["memory", "dynamodb"] if metafunc.config.getoption("--ddb") else ["memory"]
        metafunc.parametrize("store", backends, indirect=True)


@pytest.fixture(scope="session")
def ddb_server(request: pytest.FixtureRequest) -> Iterator[DynamoDBLocal]:
    server = DynamoDBLocal()
    require_local(server, required=request.config.getoption("--require-ddb"))
    with server:
        yield server
    assert server.process is not None and server.process.poll() is not None


def require_local(server: DynamoDBLocal, *, required: bool) -> None:
    if not server.available:
        message = "DynamoDB Local missing: install Java and DynamoDBLocal.jar/lib in .tools/"
        if required:
            pytest.fail(message)
        pytest.skip(message)


@pytest.fixture
def store(request: pytest.FixtureRequest, clock: Clock) -> Iterator[StoreBase]:
    if request.param == "memory":
        yield MemoryStore(clock=clock)
    else:
        request.getfixturevalue("loopback_network")
        server: DynamoDBLocal = request.getfixturevalue("ddb_server")
        table = "synthetic-" + uuid4().hex
        ensure_table(server.client, table)
        ensure_table(server.client, table)
        try:
            yield DynamoStore(server.client, table, clock=clock)
        finally:
            server.client.delete_table(TableName=table)
