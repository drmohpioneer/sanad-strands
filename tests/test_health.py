import asyncio
import socket

import httpx
import pytest
import pytest_socket

# This probe runs at collection, before importing the application.
with (
    pytest.warns(UserWarning, match="A test tried"),
    pytest.raises(pytest_socket.SocketBlockedError),
):
    socket.socket(socket.AF_INET, socket.SOCK_STREAM)

from sanad.api.app import create_app  # noqa: E402


@pytest.mark.parametrize("revision", ["dev", "synthetic-revision-123"])
def test_health_without_external_services(revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANAD_REVISION", "must-not-be-read")
    app = create_app() if revision == "dev" else create_app(revision=revision)

    async def request() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://sanad.test", trust_env=False
        ) as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "service": "sanad", "revision": revision}
        assert response.headers["content-type"] == "application/json"

    asyncio.run(request())


def test_factories_keep_independent_revisions() -> None:
    first, second = create_app("first"), create_app("second")

    async def request() -> None:
        for app, expected in ((first, "first"), (second, "second"), (first, "first")):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://sanad.test",
                trust_env=False,
            ) as client:
                assert (await client.get("/health")).json()["revision"] == expected

    asyncio.run(request())
