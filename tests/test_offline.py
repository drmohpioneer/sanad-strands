import ast
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_socket

import sanad.domain


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
@pytest.mark.parametrize("kind", [socket.SOCK_STREAM, socket.SOCK_DGRAM])
def test_internet_sockets_are_blocked(family: int, kind: int) -> None:
    with (
        pytest.warns(UserWarning, match="A test tried"),
        pytest.raises(pytest_socket.SocketBlockedError),
    ):
        socket.socket(family, kind)


def test_dns_and_high_level_connections_are_blocked() -> None:
    with (
        pytest.warns(UserWarning, match="getaddrinfo"),
        pytest.raises(pytest_socket.SocketBlockedError),
    ):
        socket.getaddrinfo("synthetic.invalid", 443)
    with (
        pytest.warns(UserWarning, match="getaddrinfo"),
        pytest.raises(pytest_socket.SocketBlockedError),
    ):
        socket.create_connection(("synthetic.invalid", 443))


def test_domain_imports_only_pure_foundation_dependencies() -> None:
    assert sanad.domain.__file__ is not None
    for path in Path(sanad.domain.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"Review new relative domain import in {path.name}"
                names = [node.module or ""]
            else:
                continue
            assert all(
                name.split(".")[0] in sys.stdlib_module_names | {"pydantic"}
                or name == "sanad.domain"
                or name.startswith("sanad.domain.")
                for name in names
            ), f"Non-foundation import in {path.name}: {names}"


def test_clean_domain_import_loads_no_web_or_provider_implementation() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import sanad.domain; "
            "assert not {'fastapi', 'boto3', 'strands', 'httpx'} & set(sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
