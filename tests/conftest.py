"""Block internet sockets and DNS before test-module imports, not just in tests."""

import socket

import pytest
import pytest_socket


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
