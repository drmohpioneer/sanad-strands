"""Offline capacity telemetry and the shared AWS retry configuration."""

from datetime import timedelta
from typing import Any

import pytest
from handover_fakes import NOW, FakeAWS

from deploy import ops, smoke
from deploy.common import OperationError
from sanad.api import lambda_entry


@pytest.mark.parametrize("count", [0, 5, 6])
def test_health_scan_threshold(count: int) -> None:
    class Metrics(FakeAWS):
        def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("metrics", kwargs))
            return {"Datapoints": [{"SampleCount": count}]}

    aws = Metrics()
    if count > 5:
        with pytest.raises(OperationError, match="Scan"):
            smoke.scan_requests(aws, "synthetic-table")
    else:
        assert smoke.scan_requests(aws, "synthetic-table") == count
    call = next(c for n, c in aws.calls if n == "metrics")
    assert call["Namespace"] == "AWS/DynamoDB"
    assert call["MetricName"] == "SuccessfulRequestLatency"
    assert call["Statistics"] == ["SampleCount"]
    assert call["Dimensions"] == [
        {"Name": "TableName", "Value": "synthetic-table"},
        {"Name": "Operation", "Value": "Scan"},
    ]
    assert call["EndTime"] - call["StartTime"] == timedelta(minutes=15)


def test_health_capacity_units_are_hourly_totals() -> None:
    class Metrics(FakeAWS):
        def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
            if kwargs["Namespace"] != "AWS/DynamoDB":
                return super().get_metric_statistics(**kwargs)
            self.calls.append(("ddb_metrics", kwargs))
            assert kwargs["EndTime"] - kwargs["StartTime"] == timedelta(hours=1)
            key = kwargs["Statistics"][0]
            return {"Datapoints": [{key: 2}, {key: 3}]}

    aws = Metrics()
    result = ops.health_report(aws, "dev", now=NOW)["measures"]
    for key in (
        "consumed_read_units_last_hour",
        "consumed_write_units_last_hour",
        "scan_requests_last_hour",
    ):
        assert result[key]["value"] == 5
    assert len([c for n, c in aws.calls if n == "ddb_metrics"]) == 3


def test_shared_store_config_retains_timeouts_and_adaptive_four_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    class Captured(Exception):
        pass

    def config(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise Captured()

    monkeypatch.setattr(lambda_entry, "Config", config)
    with pytest.raises(Captured):
        lambda_entry.configure("synthetic")
    assert captured == {
        "connect_timeout": 2,
        "read_timeout": 3,
        "retries": {"mode": "adaptive", "total_max_attempts": 4},
    }
