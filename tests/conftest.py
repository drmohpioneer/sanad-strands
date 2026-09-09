"""Block internet sockets and DNS before test-module imports, not just in tests."""

import os
import socket

import pytest
import pytest_socket

_REAL_GETADDRINFO = socket.getaddrinfo

# The deployed build renders English whatever a record stores (Decision 023), so
# the switch defaults on in production. The suite turns it off so the bilingual
# machinery underneath keeps its coverage; tests/test_contest_language.py is the
# one place that exercises the switch on.
os.environ.setdefault("SANAD_CONTEST_ENGLISH", "0")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--live", action="store_true", help="explicit isolated live-check suite")
    parser.addoption("--ddb", action="store_true", help="include DynamoDB Local store parity")
    parser.addoption(
        "--require-ddb", action="store_true", help="fail rather than skip missing Local"
    )


@pytest.fixture
def loopback_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the explicit Local fixture can allow numeric loopback, never internet DNS."""

    def local_dns(host: str, port: int, *args: object, **kwargs: object) -> object:
        if host != "127.0.0.1":
            raise pytest_socket.SocketBlockedError(
                "only numeric DynamoDB Local loopback is allowed"
            )
        return _REAL_GETADDRINFO(host, port, *args, **kwargs)  # type: ignore[arg-type]

    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts(["127.0.0.1"])
    monkeypatch.setattr(socket, "getaddrinfo", local_dns)


def _deny_dns(*args: object, **kwargs: object) -> None:
    raise pytest_socket.SocketBlockedError("DNS is disabled for offline tests")


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--live"):
        if os.environ.get("SANAD_LIVE") != "1" or config.args != ["tests/live"]:
            raise pytest.UsageError("--live requires SANAD_LIVE=1 and exactly tests/live")
        return
    # asyncio uses a local socket pair; AF_UNIX permits no internet connection.
    pytest_socket.disable_socket(allow_unix_socket=True)
    patch = pytest.MonkeyPatch()
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        patch.setattr(socket, name, _deny_dns)
    config.add_cleanup(patch.undo)
    config.add_cleanup(pytest_socket.enable_socket)


@pytest.fixture(autouse=True)
def coordinator_scripted_only(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """16b has no live allowance: every default Coordinator is a scripted refusal."""
    if request.config.getoption("--live"):
        return
    from providers.fixtures import ScriptedModel, response
    from strands.models import Model

    from sanad.coordinator import agent
    from sanad.models.registry import ModelRegistry, ModelRole

    real_calls: list[bool] = []

    def forbidden(*args: object, **kwargs: object) -> Model:
        real_calls.append(True)
        raise AssertionError("Contract 16b forbids real provider calls")

    def scripted(registry: ModelRegistry, role: ModelRole) -> Model:
        return ScriptedModel(response('{"refused": true}'))

    monkeypatch.setattr(agent, "bedrock_model", forbidden)
    monkeypatch.setattr(agent, "model_factory", scripted)
    request.addfinalizer(lambda: _assert_no_coordinator_provider_calls(real_calls))


def _assert_no_coordinator_provider_calls(calls: list[bool]) -> None:
    assert calls == [], "zero real Coordinator provider calls required"
