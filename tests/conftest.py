"""Block internet sockets and DNS before test-module imports, not just in tests."""

import socket

import pytest
import pytest_socket

_REAL_GETADDRINFO = socket.getaddrinfo


def pytest_addoption(parser: pytest.Parser) -> None:
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
    # asyncio uses a local socket pair; AF_UNIX permits no internet connection.
    pytest_socket.disable_socket(allow_unix_socket=True)
    patch = pytest.MonkeyPatch()
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        patch.setattr(socket, name, _deny_dns)
    config.add_cleanup(patch.undo)
    config.add_cleanup(pytest_socket.enable_socket)
