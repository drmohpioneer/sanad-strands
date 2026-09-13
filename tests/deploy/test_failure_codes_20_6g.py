"""Boot/worker/tick sources and the deployed health error-count oracle, offline."""

from datetime import timedelta
from typing import Any

import pytest
from handover_fakes import NOW, FakeAWS
from store.account_fixtures import settings
from store.login_fixtures import TestClient  # type: ignore[attr-defined]
from system.test_stability_20_6g import conflict

from deploy import ops, smoke
from deploy.common import OperationError
from sanad.api.app import create_app
from sanad.api.failures import REASONS
from sanad.channels.transport import CapturedTransport
from sanad.ops.nonce_store import NonceStore, TickVerifier
from sanad.ops.tick_signing import signed_headers
from sanad.store.memory import MemoryStore
from sanad.store.records import AuthorizationUnavailable


@pytest.mark.parametrize("path", ["/internal/tick", "/events"])
@pytest.mark.parametrize("failure", ["authorization", "conflict", "unexpected", "unexpected_value"])
def test_internal_http_failure_sources(
    path: str, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from sanad.ops.worker import worker_body

    store = MemoryStore(clock=lambda: NOW)

    def fail(*args: Any) -> dict[str, Any]:
        if failure == "authorization":
            raise AuthorizationUnavailable("private")
        if failure == "conflict":
            raise conflict()
        if failure == "unexpected_value":
            raise ValueError("private")
        raise RuntimeError("private")

    app = create_app(
        store=store,
        clock=lambda: NOW,
        telegram_settings=settings(),
        transport=CapturedTransport(),
        tick_verifier=TickVerifier("secret", NonceStore(store, "test"), lambda: NOW),
        tick_sweep=fail,
    )
    event: dict[str, Any] = {"type": "process_receipt", "receipt": {}}
    body = worker_body(event) if path == "/events" else b"{}"
    headers = signed_headers("secret", str(int(NOW.timestamp())), "a1" * 32, body)
    if path == "/events":
        event["headers"] = headers
        body = json.dumps(event).encode()
        monkeypatch.setattr("sanad.api.internal.process_event", fail)
    with TestClient(app) as client:
        result = client.post(path, content=body, headers=headers)
    reason = {
        "authorization": "authorization_unavailable",
        "conflict": "ingress_conflict",
        "unexpected": "unhandled",
        "unexpected_value": "unhandled",
    }[failure]
    assert result.status_code == (500 if reason == "unhandled" else 409)
    assert result.json()["reason"] == reason


def test_configuration_source_and_metadata_filter(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from sanad.api import lambda_entry

    # Runtime startup sets provider logger levels; restore them after this boot rail.
    for name in ("httpx", "httpcore", "botocore", "boto3", "urllib3"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setenv("SANAD_REVISION", "synthetic")

    def fail(*args: Any) -> Any:
        raise RuntimeError("private credential")

    monkeypatch.setattr(lambda_entry, "configure", fail)
    with TestClient(lambda_entry.create_runtime_app()) as client:
        result = client.get("/api/patient/me")
    assert result.status_code == 503 and result.json()["reason"] == "configuration"
    assert "reason=configuration route_family=patient" in caplog.text
    assert "private credential" not in caplog.text
    record = logging.LogRecord(
        "uvicorn.error", 40, "", 1, "private", (), (RuntimeError, RuntimeError("private"), None)
    )
    assert lambda_entry.MetadataOnlyErrors().filter(record)
    assert record.getMessage() == (
        "request_failed reason=unhandled route_family=web "
        "exception_class=RuntimeError module=unknown function=unknown"
    )
    assert record.exc_info is None


@pytest.mark.parametrize("points", [[], [{"Sum": 0}], [{"Sum": 1}]])
def test_smoke_counts_last_fifteen_minutes(points: list[dict[str, int]]) -> None:
    class Metrics(FakeAWS):
        def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("get_metric_statistics", kwargs))
            return {"Datapoints": points}

    aws = Metrics()
    if points == [{"Sum": 0}]:
        assert smoke.app_errors(aws, "sanad-dev-app") == 0
    else:
        with pytest.raises(OperationError):
            smoke.app_errors(aws, "sanad-dev-app")
    call = next(kwargs for name, kwargs in aws.calls if name == "get_metric_statistics")
    assert call["EndTime"] - call["StartTime"] == timedelta(minutes=15)
    assert call["MetricName"] == "Errors" and call["Dimensions"][0]["Value"] == "sanad-dev-app"


def test_health_report_counts_only_fixed_reason_lines() -> None:
    aws = FakeAWS()
    aws.logs["/aws/lambda/sanad-dev-app"] = [
        {
            "timestamp": int(NOW.timestamp() * 1000),
            "message": f"request_failed reason={reason} route_family=patient",
        }
        for reason in REASONS
        for _ in range(2)
    ] + [{"timestamp": int(NOW.timestamp() * 1000), "message": "private reason=unknown"}]
    report = ops.health_report(aws, "dev", now=NOW)
    assert report["measures"]["failed_requests_last_hour"]["value"] == dict.fromkeys(REASONS, 2)
