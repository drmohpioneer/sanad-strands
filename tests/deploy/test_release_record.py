import json
from pathlib import Path

import pytest
from handover_fakes import IMAGE, NOW, PRIOR, SHA, FakeAWS, release_root

from deploy import common, release_record
from deploy.common import OperationError, command
from deploy.restore import CLASSES

SENSITIVE = [
    "sk-" + "x" * 40,
    "+201001234567",
    "operator@example.test",
    "QwErTyUiOpAsDfGhJkLzXcVbNm123456",
]
SECTIONS = {
    "Revision and image",
    "Schema",
    "Configuration and parameter versions",
    "Policy and model versions",
    "Alarms",
    "Budget and current spend",
    "Log retention",
    "Point-in-time recovery",
    "Last restore rehearsal",
    "Rollback rehearsal",
    "Limits and open items",
    "Accountable operator",
}


def test_public_record_sections_allowlist_and_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    aws = FakeAWS()
    root = release_root(tmp_path, aws)
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    aws.parameters["/sanad/dev/unknown"] = SENSITIVE[0]
    destination = release_record.release_record(aws, "dev", root=root)
    assert not destination.exists()
    assert json.loads(capsys.readouterr().out)["sections"] == 12
    _, text = release_record.render_record(aws, "dev", root=root, now=NOW)
    assert {
        line.removeprefix("## ") for line in text.splitlines() if line.startswith("## ")
    } == SECTIONS
    assert SHA in text and IMAGE in text and "not set" in text
    assert "ENABLED" in text and "3.5 USD" in text and "30 days" in text
    assert "No previous" not in text and PRIOR in text
    assert "Not run for this image pair" in text
    assert "Activation CLOSED" in text and text.count("no alarm") == 4
    assert all(
        s not in text for s in [*SENSITIVE, *aws.parameters.values(), "private@example.test"]
    )
    assert "version 1" in text
    release_record.release_record(aws, "dev", root=root, yes=True)
    assert destination == root / "docs/releases" / f"dev-{SHA}.md"
    assert destination.read_text().startswith("# Sanad")


@pytest.mark.parametrize("value", SENSITIVE)
@pytest.mark.parametrize("field", ["operator", "alarm", "backlog"])
def test_sensitive_allowed_fields_refuse_before_write(
    value: str,
    field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    aws = FakeAWS()
    root = release_root(tmp_path, aws)
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    if field == "operator":
        aws.parameters["/sanad/dev/operator-name"] = value
    elif field == "alarm":
        aws.alarm_state = value
    else:
        (root / "docs/backlog.md").write_text("| " + value + " | before row 21 |")
    with pytest.raises(OperationError, match="Sensitive"):
        release_record.release_record(aws, "dev", root=root, yes=True)
    assert not (root / "docs/releases").exists()
    assert value not in capsys.readouterr().out


def test_restore_evidence_and_both_rollback_directions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aws = FakeAWS()
    root = release_root(tmp_path, aws)
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    counts = {
        "rows": 5,
        "oldest_unfinished_inbound": {"count": 0, "oldest_at": None},
        "oldest_due_work": {"count": 1, "oldest_at": NOW.isoformat()},
        "would_send_by_kind": {"DANGER": 2},
        "secret": SENSITIVE[0],
    }
    reconciliation = {
        "sends_enabled": False,
        "reconciled_at": NOW.isoformat(),
        "differing_versions": dict.fromkeys(CLASSES, 1),
        "live": dict.fromkeys(CLASSES, counts),
        "restored": dict.fromkeys(CLASSES, counts),
    }
    evidence = root / "docs/evidence/restore-dev-proof.jsonl"
    evidence.write_text(
        json.dumps({"mode": "dry-run"})
        + "\n"
        + json.dumps(
            {
                "mode": "execute",
                "source": "sanad-dev-data",
                "target": "sanad-dev-data-restore-proof",
                "reconciliation": reconciliation,
            }
        )
    )
    history = root / "deploy/releases/dev.json"
    entries = json.loads(history.read_text())
    for image in (PRIOR, IMAGE):
        entries.append(
            {
                "action": "rollback",
                "image_digest": image,
                "git_sha": SHA,
                "timestamp": NOW.isoformat(),
                "status": "passed",
                "smoke": {"health": {"status": "PASSED", "raw": SENSITIVE[0]}},
            }
        )
    history.write_text(json.dumps(entries))
    _, text = release_record.render_record(aws, "dev", root=root)
    assert "Both directions recorded" in text and "health: PASSED" in text
    assert "differing stored versions: 1" in text and "Would send DANGER: 2" in text
    assert SENSITIVE[0] not in text


def test_schema_history_and_target_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aws = FakeAWS()
    root = release_root(tmp_path, aws)
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    with pytest.raises(OperationError):
        release_record.release_record(aws, "judge", root=root, yes=True)
    aws.table_name = "other"
    with pytest.raises(OperationError):
        release_record.release_record(aws, "dev", root=root, yes=True)
    aws.table_name = "sanad-dev-data"
    aws.tables["sanad-dev-data"][("META", "schema_version")]["schema_version"] = {"N": "2"}
    with pytest.raises(OperationError, match="schema_version"):
        release_record.release_record(aws, "dev", root=root, yes=True)
    assert not (root / "docs/releases").exists()


def test_release_cli_refuses_before_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["release_record", "--env", "judge", "--yes"])
    monkeypatch.setattr(release_record, "session", lambda: pytest.fail("No AWS"))
    with pytest.raises(SystemExit, match="--env dev"):
        command(release_record.main)


def test_operator_name_is_optional_and_not_application_configuration() -> None:
    aws = FakeAWS()
    assert common.PARAMETERS["operator-name"] == ("String", None)
    before = common.configuration_revision(aws, "dev")
    aws.parameters["/sanad/dev/operator-name"] = "Synthetic Operator"
    values, _ = common.parameter_values(aws, "dev")
    assert "operator-name" not in values
    assert common.configuration_revision(aws, "dev") == before
    assert (
        common.parameter_values(aws, "dev", include_operator=True)[0]["operator-name"]
        == "Synthetic Operator"
    )


def test_operator_setting_preserves_optional_name_and_generated_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_operator_deployment import FakeAws

    from deploy import ops

    aws = FakeAws()
    aws.ssm.values.clear()
    local = {
        "GEMINI_API_KEY": "synthetic-gemini",
        "TELEGRAM_BOT_TOKEN_SANAD_STRANDS": "4242:synthetic-secret-token",
        "SANAD_ADMIN_TELEGRAM_USER_ID": "10001",
        "SANAD_TELEGRAM_BOT_USERNAME": "synthetic_bot",
        "SANAD_BUDGET_EMAIL": "synthetic@example.test",
    }
    monkeypatch.setattr(ops, "env_values", lambda _: local)
    ops.secrets_set(aws, "dev", operator_name="Synthetic Operator")
    assert aws.ssm.values["/sanad/dev/operator-name"] == "Synthetic Operator"
    before = dict(aws.ssm.values)
    assert len(aws.ssm.puts) == 9
    ops.secrets_set(aws, "dev")
    ops.secrets_set(aws, "dev", operator_name="Synthetic Operator")
    assert aws.ssm.values == before and len(aws.ssm.puts) == 9
    for value in SENSITIVE:
        with pytest.raises(OperationError):
            ops.secrets_set(aws, "dev", operator_name=value)
    assert len(aws.ssm.puts) == 9
