import copy
import json
from datetime import timedelta
from typing import Any, cast

import pytest
from handover_fakes import NOW, FakeAWS, item
from store.fixtures import SCOPE, intent, profile

from deploy import restore
from deploy.common import OperationError, command
from sanad.store.records import IdentityConfig, Incident, OutboundIntent, to_record

SETTINGS = IdentityConfig(bot_id="1234", admin_user_id="10001")


@pytest.mark.parametrize("suffix", ["", "../live", "sanad-dev-data", "UPPER", "x" * 64, "x/y"])
def test_name_guard(suffix: str) -> None:
    aws = FakeAWS()
    if suffix == "sanad-dev-data":  # Still a NEW prefixed target, never the live table.
        assert restore.restore_name(suffix) != "sanad-dev-data"
    else:
        with pytest.raises(OperationError):
            restore.restore(aws, "dev", suffix, yes=True)
        assert aws.calls == []


@pytest.mark.parametrize("tag", [[], [{"Key": "sanad-restore", "Value": "someone-else"}]])
def test_tag_guard_and_live_refusal(tag: list[dict[str, str]]) -> None:
    aws = FakeAWS()
    name = restore.restore_name("proof")
    aws.tables[name] = {}
    aws.tags[name] = tag
    with pytest.raises(OperationError, match="tag"):
        restore.restore(aws, "dev", "proof", delete=True, yes=True)
    with pytest.raises(OperationError, match="non-restore"):
        restore.tagged_table(aws, "sanad-dev-data", "proof")
    assert not any(c[0] == "delete-table" for c in aws.calls)


def test_restore_default_dry_run_then_tagged_delete() -> None:
    aws = FakeAWS()
    result = restore.restore(aws, "dev", "proof", now=NOW)
    assert result["mode"] == "dry-run" and result["tables_to_restore"] == 1
    assert result["restore_at"] == (NOW - timedelta(minutes=5)).isoformat()
    assert len(aws.tables) == 1
    restore.restore(aws, "dev", "proof", now=NOW, yes=True)
    name = restore.restore_name("proof")
    assert aws.tags[name] == [{"Key": "sanad-restore", "Value": "proof"}]
    with pytest.raises(OperationError, match="already exists"):
        restore.restore(aws, "dev", "proof", yes=True)
    restore.restore(aws, "dev", "proof", delete=True)
    assert name in aws.tables
    restore.restore(aws, "dev", "proof", delete=True, yes=True)
    assert set(aws.tables) == {"sanad-dev-data"}


def test_seeded_reconciliation_imports_real_freshness_and_never_writes() -> None:
    aws = FakeAWS()
    for pk, sk in [
        ("ACCT#1234", "EVENT#x"),
        ("D#doctor", "DOCTOR"),
        ("D#doctor#INTAKE#i", "PROFILE"),
        ("D#doctor#P#p", "PROFILE"),
        ("OPS#ops", "ISSUE#x"),
    ]:
        aws.add(item(pk, sk))
    # Safety response validates patient recipient and safety epoch without doctor coverage.
    p = profile(recipient_ref="patient-chat", recipient_subject="patient", recipient_auth_epoch=0)
    aws.add_model(p, SCOPE)
    incident = Incident(
        id="danger",
        scope=SCOPE,
        unique_source_key="danger-source",
        facts={},
        severity="urgent",
        raised_at=NOW,
        review_obligation_id="danger-review",
        alert_intent_ids=("ready",),
        template_id="synthetic-safety",
        created_at=NOW,
        updated_at=NOW,
    )
    aws.add_model(incident, SCOPE)
    for id, change in [
        ("ready", {}),
        ("retry", {"retry_count": 1}),
        (
            "future",
            {"work_clock": {"work_lane": "delivery", "next_action_at": NOW + timedelta(hours=1)}},
        ),
        ("stale", {"safety_epoch_seen": 99}),
        ("failed", {"status": "failed", "review_obligation_id": "review", "work_clock": None}),
        (
            "uncertain",
            {"status": "uncertain", "review_obligation_id": "review", "work_clock": None},
        ),
    ]:
        out = intent(
            id=id,
            template_id="synthetic-safety",
            source_versions=(to_record(incident, SCOPE).ref,),
            notification_purpose="patient_safety_response",
            recipient_ref="patient-chat",
            recipient_auth_epoch_seen=0,
            safety_epoch_seen=0,
            created_at=NOW,
            updated_at=NOW,
            expires_at=NOW + timedelta(hours=2),
            work_clock={"work_lane": "delivery", "next_action_at": NOW - timedelta(minutes=2)},
        ).model_copy(update=cast(dict[str, Any], change))
        aws.add_model(OutboundIntent.model_validate(out.model_dump(warnings=False)), SCOPE)
    aws.add(
        item(
            "IN#telegram#receipt",
            "META",
            entity_type="inbound_receipt",
            state="processing",
            received_at=(NOW - timedelta(minutes=3)).isoformat(),
        )
    )
    target = restore.restore_name("proof")
    aws.tables[target] = copy.deepcopy(aws.tables["sanad-dev-data"])
    aws.tables[target][("D#doctor", "DOCTOR")]["version"] = {"N": "2"}
    aws.add(item("D#doctor#INTAKE#i", "EXTRA"), target)
    before = copy.deepcopy(aws.tables)
    report = restore.reconcile(aws, "sanad-dev-data", target, NOW, SETTINGS)
    assert report["differing_versions"] == dict.fromkeys(restore.CLASSES, 0) | {"doctor": 1}
    assert report["live"]["patient"]["would_send_by_kind"] == {"patient_safety_response": 2}, (
        report["live"]["patient"]["suppressed_by_reason"]
    )
    assert report["live"]["patient"]["suppressed_by_reason"] == {"safety_epoch": 1}
    assert report["live"]["global"]["oldest_unfinished_inbound"]["age_seconds"] == 180
    assert report["restored"]["intake"]["rows"] == 2
    assert report["live"]["patient"]["oldest_due_work"]["age_seconds"] == 120
    assert report["sends_enabled"] is False and aws.tables == before
    assert all(name == "scan" for name, _ in aws.calls)
    assert "patient-chat" not in json.dumps(report)


@pytest.mark.parametrize("env,table", [("judge", "sanad-dev-data"), ("dev", "other")])
def test_restore_environment_guard(env: str, table: str) -> None:
    aws = FakeAWS()
    aws.table_name = table
    with pytest.raises(OperationError):
        restore.restore(aws, env, "proof", yes=True)
    assert aws.calls == []


def test_cli_refuses_judge_before_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["restore", "--env", "judge", "--into", "proof"])
    monkeypatch.setattr(restore, "session", lambda: pytest.fail("No AWS"))
    with pytest.raises(SystemExit, match="--env dev"):
        command(restore.main)


def test_read_only_client_refuses_mutation() -> None:
    read = restore.ReadOnlyClient(FakeAWS())
    with pytest.raises(AttributeError):
        cast(Any, read).put_item()
