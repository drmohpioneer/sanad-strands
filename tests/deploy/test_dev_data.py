"""Dev reset selection, pagination, media ordering and restart detection without AWS."""

import copy
import json
import sys
import time
from typing import Any

import pytest

from deploy import cleanup
from deploy.common import OperationError, command

TENANT_DELETE = (
    "NAME",
    "REVIEW",
    "REVIEW_OFFER",
    "LIAISON_NOTICE",
    "REUSE_OFFER",
    "REUSABLE_ANSWER",
    "PHOTO_ASSOCIATION_WORK",
    "INTAKE_DRAFT",
    "INTAKE_CALLBACK",
    "SCRIBE_STATE",
    "SCRIBE_PROPOSAL",
    "SCRIBE_CALLBACK",
    "SCRIBE_INVITATION_WORK",
    "PATIENT_ACTION",
    "BUNDLE",
    "QUESTION_DIGEST",
    "EVENT",
    "OUT",
    "ATTEMPT",
    "CMD",
    "OUTKEY",
    "REVIEWKEY",
    "FUTURE_KIND",
)
ACCOUNT_DELETE = ("PATIENT_CLAIM", "EVENT", "OUT", "ATTEMPT", "CMD", "OUTKEY", "FUTURE_KIND")


def item(pk: str, sk: str, **body: Any) -> dict[str, Any]:
    return {"PK": {"S": pk}, "SK": {"S": sk}, "body": {"S": json.dumps(body)}}


def key(row: dict[str, Any]) -> tuple[str, str]:
    return row["PK"]["S"], row["SK"]["S"]


class FakeAWS:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.delete_expected: set[tuple[str, str]] = set()
        self.objects: list[dict[str, str]] = []
        self.calls: list[tuple[str, Any]] = []
        self.table = "sanad-dev-data"
        self.reappear = False
        self.unprocessed = True
        self.always_unprocessed = False
        self.s3_failure = False
        self.doctor_scans = 0
        self.bot = "1234"
        for bot, doctor in [("1234", "dev%23doctor:1"), ("9876", "other")]:
            dev = bot == self.bot
            scope = {"doctor_id": "dev#doctor:1" if dev else doctor}
            self.add(item(f"D#{doctor}", "DOCTOR", telegram_bot_id=bot, scope=scope))
            for keep in ("PROFILE", "POLICY#v1", "APPLICATION#x", "NAME_CACHE#x"):
                self.add(item(f"D#{doctor}", keep))
            for prefix in TENANT_DELETE:
                self.add(item(f"D#{doctor}", prefix + "#x"), delete=dev)
            for partition in (f"D#{doctor}#P#patient%23one", f"D#{doctor}#INTAKE#intake:one"):
                for prefix in ("PROFILE", "FACT", "MISSION", "MEDIA", "FUTURE_KIND"):
                    self.add(item(partition, prefix + "#x"), delete=dev)
            for prefix in ACCOUNT_DELETE:
                self.add(item(f"ACCT#{bot}", prefix + "#x"), delete=dev)
            for prefix in ("ADMIN_ACCOUNT", "APPLICATION", "ACK", "NAME_CACHE"):
                self.add(item(f"ACCT#{bot}", prefix + "#x"))
            for purpose in ("doctor_login", "admin_login", "patient_login", "invitation", "future"):
                self.add(
                    item(f"ACCT#{bot}", "TOKEN_HEAD#" + purpose, purpose=purpose),
                    delete=dev and purpose not in {"doctor_login", "admin_login"},
                )
            for i, roles in enumerate([["patient"], ["doctor"], ["admin"], ["doctor", "admin"]]):
                self.add(
                    item(f"SUBJECT#{bot}#{i}", "BINDING", role_set=roles),
                    delete=dev and roles == ["patient"],
                )
            for kind in (
                "login_exchange",
                "invitation",
                "claim_callback",
                "pre_session",
                "callback",
            ):
                self.add(item(f"TOKEN#{kind}#{bot}", "META", scope={"bot_id": bot}), delete=dev)
            for role in ("doctor", "patient", "admin"):
                self.add(
                    item(f"SESSION#{bot}-{role}", "META", scope={"bot_id": bot}, role=role),
                    delete=dev,
                )
            for label, inbound_scope in [
                ("tenant", scope),
                ("patient", scope | {"patient_id": "patient#one"}),
                ("intake", scope | {"intake_id": "intake:one"}),
                ("account", {"bot_id": bot}),
            ]:
                self.add(
                    item(f"IN#telegram#{bot}-{label}", "META", scope=inbound_scope), delete=dev
                )
        for pk, sk in [
            ("META", "schema_version"),
            ("OPS#1234", "ISSUE#x"),
            ("ACCT#rxnorm", "NAME_CACHE#x"),
            ("ACCT#12345", "PATIENT_CLAIM#x"),
            ("D#dev%23doctor:10", "OUT#x"),
        ]:
            self.add(item(pk, sk))
        # Same synthetic tenant-smoke namespace as the deploy rail must survive.
        self.add(item("D#smoke", "DOCTOR", telegram_bot_id="synthetic-tenant-smoke"))
        self.add(item("D#smoke#P#p", "PROFILE"))
        for prefix in (
            "dev%23doctor%3A1/patient%23one/",
            "dev%23doctor%3A1/intake/intake%3Aone/",
            "other/patient%23one/",
            "other/intake/intake%3Aone/",
            "builds/",
        ):
            for suffix in ("blob", "uploads/data", "transcript", "association", "evidence-reads/x"):
                for version in ("v1", "v2", "marker"):
                    self.objects.append({"Key": prefix + suffix, "VersionId": version})

    def add(self, row: dict[str, Any], *, delete: bool = False) -> None:
        self.rows[key(row)] = row
        if delete:
            self.delete_expected.add(key(row))

    def client(self, service: str, **kwargs: Any) -> "FakeAWS":
        assert service in {"cloudformation", "ssm", "dynamodb", "s3"}
        return self

    def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"StackName": "sanad-dev"}
        return {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": "TableName", "OutputValue": self.table},
                        {"OutputKey": "BucketName", "OutputValue": "synthetic-dev-bucket"},
                    ]
                }
            ]
        }

    def get_parameters(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["WithDecryption"] is True
        assert all(n.startswith("/sanad/dev/") for n in kwargs["Names"])
        return {
            "Parameters": [
                {
                    "Name": "/sanad/dev/bot-token",
                    "Value": "1234:never-print-this-secret",
                    "Version": 1,
                }
            ]
        }

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["TableName"] == "sanad-dev-data" and kwargs["ConsistentRead"]
        field = kwargs["ExpressionAttributeNames"]["#field"]
        value = kwargs["ExpressionAttributeValues"][":value"]["S"]
        if field == "SK" and "ExclusiveStartKey" not in kwargs:
            self.doctor_scans += 1
            if self.reappear and self.doctor_scans == 2:
                self.add(item("D#dev%23doctor:1", "OUT#reappeared"))
        self.calls.append(("scan", (field, value, kwargs.get("ExclusiveStartKey"))))
        rows = sorted(self.rows.values(), key=key)
        if "ExclusiveStartKey" in kwargs:
            rows = [r for r in rows if key(r) > key(kwargs["ExclusiveStartKey"])]
        page = rows[:7]  # Filter after paging: empty pages still have a cursor.
        matches = (
            [r for r in page if r[field]["S"].startswith(value)]
            if kwargs["FilterExpression"].startswith("begins_with")
            else [r for r in page if r[field]["S"] == value]
        )
        return {
            "Items": matches,
            **(
                {"LastEvaluatedKey": {k: page[-1][k] for k in ("PK", "SK")}}
                if len(rows) > 7
                else {}
            ),
        }

    def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["Bucket"] == "synthetic-dev-bucket"
        prefix = kwargs["Prefix"]
        assert prefix in {
            "dev%23doctor%3A1/patient%23one/",
            "dev%23doctor%3A1/intake/intake%3Aone/",
        }
        self.calls.append(("s3-list", prefix))
        objects = sorted(
            [o for o in self.objects if o["Key"].startswith(prefix)],
            key=lambda o: (o["Key"], o["VersionId"]),
        )
        if "KeyMarker" in kwargs:
            marker = (kwargs["KeyMarker"], kwargs["VersionIdMarker"])
            objects = [o for o in objects if (o["Key"], o["VersionId"]) > marker]
        page = objects[:4]
        return {
            "Versions": [o for o in page if o["VersionId"] != "marker"],
            "DeleteMarkers": [o for o in page if o["VersionId"] == "marker"],
            "IsTruncated": len(objects) > 4,
            **(
                {"NextKeyMarker": page[-1]["Key"], "NextVersionIdMarker": page[-1]["VersionId"]}
                if len(objects) > 4
                else {}
            ),
        }

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("s3-delete", kwargs["Delete"]["Objects"]))
        if self.s3_failure:
            self.objects.remove(kwargs["Delete"]["Objects"][0])
            return {"Errors": [{"Code": "AccessDenied"}]}
        for obj in kwargs["Delete"]["Objects"]:
            assert obj["Key"].startswith("dev%23doctor%3A1/")
            self.objects.remove(obj)
        return {}

    def batch_write_item(self, **kwargs: Any) -> dict[str, Any]:
        requests = kwargs["RequestItems"]["sanad-dev-data"]
        assert 1 <= len(requests) <= 25
        assert not [o for o in self.objects if o["Key"].startswith("dev%23doctor%3A1/")]
        self.calls.append(("ddb-delete", copy.deepcopy(requests)))
        pending = requests[:1] if self.unprocessed else []
        if not self.always_unprocessed:
            self.unprocessed = False
        for request in requests[len(pending) :]:
            self.rows.pop(key(request["DeleteRequest"]["Key"]), None)
        return {"UnprocessedItems": {"sanad-dev-data": pending}}


def test_dev_data_default_dry_run_and_exact_keep_lists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    aws = FakeAWS()
    monkeypatch.setattr(cleanup, "session", lambda: aws)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["cleanup", "dev-data", "--env", "dev"])
    before, objects = copy.deepcopy(aws.rows), copy.deepcopy(aws.objects)
    cleanup.main()
    dry = json.loads(capsys.readouterr().out)
    assert dry["mode"] == "dry-run" and dry["rows"] == len(aws.delete_expected)
    assert dry["s3_objects"] == 30
    assert dry["partition_classes"] == {
        "patient": 5,
        "intake": 5,
        "doctor": 23,
        "account": 10,
        "global": 13,
    }
    assert dry["global_pk_prefixes"] == {"SUBJECT#": 1, "TOKEN#": 5, "SESSION#": 3, "IN#": 4}
    assert dry["sk_prefixes"]["doctor"] == dict.fromkeys(TENANT_DELETE, 1)
    assert aws.rows == before and aws.objects == objects
    assert not any(name.endswith("delete") for name, _ in aws.calls)
    aws.doctor_scans = 0
    monkeypatch.setattr(sys, "argv", ["cleanup", "dev-data", "--env", "dev", "--yes"])
    cleanup.main()
    execute = json.loads(capsys.readouterr().out)
    assert execute == dry | {"mode": "execute"}
    assert set(aws.rows) == set(before) - aws.delete_expected
    assert aws.rows == {k: v for k, v in before.items() if k not in aws.delete_expected}
    assert aws.objects == [o for o in objects if not o["Key"].startswith("dev%23doctor%3A1/")]
    assert aws.doctor_scans == 2
    assert sum(name == "ddb-delete" for name, _ in aws.calls) == 4  # Three batches plus retry.
    assert "never-print-this-secret" not in json.dumps(execute)
    assert cleanup.dev_data(aws, "dev", yes=True)["rows"] == 0


@pytest.mark.parametrize("env,table", [("judge", "sanad-dev-data"), ("dev", "sanad-judge-data")])
def test_dev_data_refuses_other_targets(env: str, table: str) -> None:
    aws = FakeAWS()
    aws.table = table
    with pytest.raises(OperationError):
        cleanup.dev_data(aws, env, yes=True)
    assert aws.calls == []


def test_dev_data_cli_refuses_judge_before_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["cleanup", "dev-data", "--env", "judge", "--yes"])
    monkeypatch.setattr(cleanup, "session", lambda: pytest.fail("AWS must not be opened"))
    with pytest.raises(SystemExit, match="--env dev"):
        command(cleanup.main)


def test_reappearing_row_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    aws = FakeAWS()
    aws.reappear = True
    monkeypatch.setattr(cleanup, "session", lambda: aws)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["cleanup", "dev-data", "--env", "dev", "--yes"])
    with pytest.raises(SystemExit, match="reappeared"):
        command(cleanup.main)
    report = json.loads(capsys.readouterr().out.splitlines()[-1])["reappeared"]
    assert report["rows"] == 1 and report["sk_prefixes"] == {"doctor": {"OUT": 1}}
    assert ("D#dev%23doctor:1", "OUT#reappeared") in aws.rows


def test_s3_failure_retains_all_rows_for_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    aws = FakeAWS()
    aws.s3_failure = True
    before = copy.deepcopy(aws.rows)
    with pytest.raises(OperationError, match="rows retained"):
        cleanup.dev_data(aws, "dev", yes=True)
    assert aws.rows == before
    aws.s3_failure = False
    monkeypatch.setattr(time, "sleep", lambda _: None)
    cleanup.dev_data(aws, "dev", yes=True)
    assert set(aws.rows) == set(before) - aws.delete_expected


def test_exhausted_unprocessed_items_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    aws = FakeAWS()
    aws.always_unprocessed = True
    monkeypatch.setattr(time, "sleep", lambda _: None)
    with pytest.raises(OperationError, match="Unprocessed"):
        cleanup.dev_data(aws, "dev", yes=True)
    assert sum(name == "ddb-delete" for name, _ in aws.calls) == 8


@pytest.mark.parametrize("extra", [["--before", "2026-09-01"], []])
def test_dev_data_requires_explicit_env_and_has_no_date_cut(
    extra: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    args = ["cleanup", "dev-data"]
    if extra:
        args += ["--env", "dev"] + extra
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(cleanup, "session", lambda: pytest.fail("No AWS before valid arguments"))
    with pytest.raises(SystemExit) as refused:
        cleanup.main()
    assert refused.value.code == 2
