import json
from datetime import timedelta
from typing import Any, cast

import pytest
from handover_fakes import NOW, FakeAWS, item

from deploy import ops
from deploy.common import OperationError, command

UNMEASURABLE = {
    "scheduler_lag",
    "leases_expired_last_hour",
    "leases_reclaimed_last_hour",
    "model_tool_asr_errors",
    "clarification_rate",
    "durable_webhook_ack",
    "readable_text_safety",
    "ordinary_text_response",
    "cost",
}


def test_every_measure_with_seeded_rows_and_paginated_logs() -> None:
    aws = FakeAWS()
    old = (NOW - timedelta(hours=2)).isoformat()
    recent = (NOW - timedelta(minutes=30)).isoformat()
    future = (NOW + timedelta(hours=1)).isoformat()
    for id, body in enumerate(
        [
            {
                "entity_type": "inbound_receipt",
                "state": "processing",
                "received_at": old,
                "processing_claim": {"expires_at": recent},
            },
            {"entity_type": "inbound_receipt", "state": "completed", "received_at": old},
            {"entity_type": "media_work", "state": "pending", "created_at": recent},
            {"entity_type": "mission", "state": "overdue", "due_at": old},
            {"entity_type": "mission", "state": "fulfilled"},
            {"entity_type": "followup", "state": "scheduled"},
            {"entity_type": "review", "state": "open", "id": "review", "created_at": old},
            {"entity_type": "review", "state": "acknowledged", "id": "seen", "created_at": recent},
            {"entity_type": "incident", "state": "open", "review_obligation_id": "review"},
            {"entity_type": "incident", "state": "open", "review_obligation_id": "seen"},
            {"entity_type": "incident", "state": "open", "review_obligation_id": "missing"},
            {"entity_type": "delivery_attempt", "outcome": "definite_failure", "ended_at": recent},
            {"entity_type": "delivery_attempt", "outcome": "uncertain", "ended_at": recent},
            {"entity_type": "delivery_attempt", "outcome": "uncertain", "ended_at": future},
            {
                "entity_type": "delivery_attempt",
                "outcome": "uncertain",
                "ended_at": (NOW - timedelta(days=2)).isoformat(),
            },
            {
                "entity_type": "outbound_intent",
                "status": "provider_accepted",
                "audience": "patient",
                "notification_purpose": "routine_prompt",
            },
            {"entity_type": "patient", "contact_status": "opted_out"},
        ]
    ):
        aws.add(item("D#d#P#p", f"ROW#{id}", **cast(dict[str, Any], body)))
    valid = item(
        "D#d#P#p",
        "MISSION#future",
        entity_type="mission",
        state="open",
        work_clock={"next_action_at": future},
    )
    valid.update(due_lane_shard={"S": "mission#0"}, due_sort={"S": future})
    aws.add(valid)
    aws.logs["/aws/lambda/sanad-dev-app"] = [
        {
            "timestamp": int((NOW - timedelta(minutes=i)).timestamp() * 1000),
            "message": "tick accepted nonce=hidden-token private@example.test",
        }
        for i in (40, 5, 20)
    ]
    report = ops.health_report(aws, "dev", now=NOW)
    m = report["measures"]
    assert report["rows_scanned"] == 18
    assert m["oldest_unfinished_inbound_receipt"]["value"]["age_seconds"] == 7200
    assert m["oldest_unfinished_media_work"]["value"]["age_seconds"] == 1800
    assert m["currently_expired_claims"]["value"] == 1
    assert m["mission_without_due_work"]["value"] == 1
    assert m["followup_without_due_work"]["value"] == 1
    assert m["review_without_due_work"]["value"] == 2
    assert m["oldest_overdue"]["value"]["age_seconds"] == 7200
    assert m["oldest_open_review"]["value"]["age_seconds"] == 7200
    assert m["definite_failure_deliveries_last_24h"]["value"] == 1
    assert m["uncertain_deliveries_last_24h"]["value"] == 1
    assert m["unacknowledged_urgent_incidents"]["value"] == {"count": 1, "missing_review": 1}
    assert m["lambda_errors_last_hour"]["value"] == {"app": 2, "relay": 2}
    assert m["contact_count"]["value"] == m["opt_outs"]["value"] == 1
    assert m["scheduler_lag"]["last_tick_accepted_at"] == (NOW - timedelta(minutes=5)).isoformat()
    assert {
        name for name, data in m.items() if data["value"] == "not measurable in this build"
    } == UNMEASURABLE
    assert len(m) == 23
    assert all(cost > 0 for cost in m["cost"]["lambda_gross_usd_last_24h"].values())
    for name in UNMEASURABLE:
        assert m[name]["reason"] and m[name]["source"]
    for name in ops.THRESHOLDS:
        assert m[name]["alarm"] == "no alarm"
    assert len(report["missing_alarms"]) == 4
    assert "hidden-token" not in json.dumps(report) and "private@example.test" not in json.dumps(
        report
    )


def test_empty_data_is_not_a_measured_latency_or_zero_metric() -> None:
    aws = FakeAWS()
    aws.errors = []
    result = ops.health_report(aws, "dev", now=NOW)["measures"]
    assert result["lambda_errors_last_hour"]["value"] == {"app": None, "relay": None}
    assert result["oldest_open_review"]["value"]["oldest_at"] is None
    assert result["scheduler_lag"]["last_tick_accepted_at"] is None


def test_health_refuses_judge_and_wrong_table(monkeypatch: pytest.MonkeyPatch) -> None:
    aws = FakeAWS()
    with pytest.raises(OperationError):
        ops.health_report(aws, "judge")
    aws.table_name = "other"
    with pytest.raises(OperationError):
        ops.health_report(aws, "dev")
    monkeypatch.setattr("sys.argv", ["ops", "health-report", "--env", "judge"])
    monkeypatch.setattr(ops, "session", lambda: pytest.fail("No AWS"))
    with pytest.raises(SystemExit, match="--env dev"):
        command(ops.main)


def test_overdue_before_tick_and_suppressed_followup_still_owe_work() -> None:
    aws = FakeAWS()
    aws.add(
        item(
            "D#d#P#p",
            "MISSION#late",
            entity_type="mission",
            state="open",
            due_at=(NOW - timedelta(minutes=12)).isoformat(),
        )
    )
    aws.add(
        item(
            "D#d#P#p",
            "FOLLOWUP#stopped",
            entity_type="followup",
            state="contact_suppressed",
        )
    )
    aws.add(
        item(
            "D#d#P#p",
            "PROFILE",
            entity_type="patient_profile",
            lease_expires_at=(NOW - timedelta(minutes=1)).isoformat(),
        )
    )
    m = ops.health_report(aws, "dev", now=NOW)["measures"]
    assert m["oldest_overdue"]["value"]["age_seconds"] == 720
    assert m["followup_without_due_work"]["value"] == 1
    assert m["currently_expired_claims"]["value"] == 1
