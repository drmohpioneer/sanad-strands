import argparse
import io
import json
import urllib.error
import zipfile
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from deploy import build, common, deploy, relay, rollback, smoke

DIGEST_A, DIGEST_B = "sha256:" + "a" * 64, "sha256:" + "b" * 64


def aws_error(code: str, message: str = "synthetic", operation: str = "Synthetic") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class FakeDynamo:
    def __init__(self, version: int | None = None):
        self.version = version
        self.writes: list[dict[str, Any]] = []

    def get_item(self, **args: Any) -> dict[str, Any]:
        assert args["ConsistentRead"] is True
        return {"Item": {"schema_version": {"N": str(self.version)}}} if self.version else {}

    def put_item(self, **args: Any) -> None:
        self.writes.append(args)
        assert args["ConditionExpression"] == "attribute_not_exists(PK)"
        if self.version is not None:
            raise aws_error("ConditionalCheckFailedException")
        self.version = int(args["Item"]["schema_version"]["N"])


class FakeSSM:
    def __init__(self) -> None:
        self.values = {f"/sanad/dev/{k}": "synthetic" for k in common.PARAMETERS}
        self.puts: list[dict[str, Any]] = []

    def get_parameters(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "Parameters": [
                {"Name": n, "Value": self.values[n], "Version": 1}
                for n in kwargs["Names"]
                if n in self.values
            ]
        }

    def put_parameter(self, **args: Any) -> None:
        self.values[args["Name"]] = args["Value"]
        self.puts.append(args)


class FakeCloudFormation:
    def __init__(self, *, exists: bool = True, noop: bool = False):
        self.exists, self.noop = exists, noop
        self.mutations: list[dict[str, Any]] = []
        self.out = {
            "TableName": "sanad-dev-data",
            "BucketName": "synthetic-bucket",
            "RepositoryUri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/sanad-dev-app",
            "BuildProjectName": "sanad-dev-build",
            "FunctionUrl": "https://synthetic.lambda-url.us-east-1.on.aws/",
            "ImageDigest": DIGEST_A,
        }
        if not exists:
            self.out.pop("FunctionUrl")
            self.out.pop("ImageDigest")

    def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
        if not self.exists:
            raise aws_error("ValidationError", "Stack does not exist")
        return {
            "Stacks": [
                {
                    "StackStatus": "UPDATE_COMPLETE",
                    "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in self.out.items()],
                }
            ]
        }

    def validate_template(self, **kwargs: Any) -> None:
        assert json.loads(kwargs["TemplateBody"])["Resources"]

    def create_stack(self, **kwargs: Any) -> None:
        self.exists = True
        self.mutations.append(kwargs)

    def update_stack(self, **kwargs: Any) -> None:
        self.mutations.append(kwargs)
        if self.noop:
            raise aws_error("ValidationError", "No updates are to be performed.")


class FakeAws:
    def __init__(self, *, version: int | None = None, exists: bool = True, noop: bool = False):
        self.cfn = FakeCloudFormation(exists=exists, noop=noop)
        self.ddb = FakeDynamo(version)
        self.ssm = FakeSSM()
        self.uploads: list[dict[str, Any]] = []

    def client(self, name: str, **kwargs: Any) -> Any:
        return {"cloudformation": self.cfn, "dynamodb": self.ddb, "ssm": self.ssm}.get(name, self)

    def describe_key(self, **kwargs: Any) -> dict[str, Any]:
        return {"KeyMetadata": {"Arn": "arn:aws:kms:us-east-1:123456789012:key/synthetic"}}

    def put_object(self, **args: Any) -> dict[str, str]:
        self.uploads.append(args)
        return {"VersionId": "synthetic-version"}

    def describe_images(self, **args: Any) -> dict[str, Any]:
        return {"imageDetails": [{"imageDigest": DIGEST_A, "imageSizeInBytes": 100000000}]}


@pytest.fixture
def operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        deploy, "env_values", lambda names: {"SANAD_BUDGET_EMAIL": "synthetic@example.test"}
    )
    monkeypatch.setattr(deploy, "git_sha", lambda: "c" * 40)
    monkeypatch.setattr(smoke, "run_smoke", lambda *args: {"health": {"status": "PASSED"}})


def test_schema_initializes_once_and_mismatch_stops_before_stack_mutation(
    tmp_path: Path, operator: None
) -> None:
    fake = FakeAws()
    assert deploy.check_schema(fake.ddb, "synthetic", initialize=True) == 1
    assert deploy.check_schema(fake.ddb, "synthetic", initialize=True) == 1
    assert len(fake.ddb.writes) == 1
    fake.ddb.version = 2
    with pytest.raises(common.OperationError, match="schema_version mismatch"):
        deploy.deploy(fake, "dev", DIGEST_A, release_path=tmp_path / "dev.json")
    assert not fake.cfn.mutations


def test_bootstrap_records_skips_and_full_noop_deploy_appends_history(
    tmp_path: Path, operator: None
) -> None:
    path = tmp_path / "dev.json"
    fake = FakeAws(exists=False)
    first = deploy.deploy(fake, "dev", None, release_path=path)
    assert first["status"] == "infrastructure"
    assert all("SKIPPED" in value for value in first["smoke"].values())
    fake.cfn.out["FunctionUrl"] = "https://synthetic.lambda-url.us-east-1.on.aws/"
    fake.cfn.out["ImageDigest"] = DIGEST_A
    fake.cfn.noop = True
    second = deploy.deploy(fake, "dev", DIGEST_A, release_path=path)
    third = deploy.deploy(fake, "dev", DIGEST_A, release_path=path)
    assert second["status"] == third["status"] == "passed"
    assert len(common.history(path)) == 3
    assert second["schema_version"] == second["table_schema_version"] == 1
    assert second["configuration_parameter_versions"]
    assert "synthetic@example.test" not in path.read_text()
    assert fake.ssm.puts[-1]["Name"] == "/sanad/dev/public-base-url"


def test_failed_smoke_is_recorded_without_implicit_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operator: None
) -> None:
    path, fake = tmp_path / "dev.json", FakeAws()
    monkeypatch.setattr(
        smoke,
        "run_smoke",
        lambda *args: {"tick": {"status": "FAILED", "reason": "synthetic failure"}},
    )
    with pytest.raises(common.OperationError, match="Smoke failed: tick"):
        deploy.deploy(fake, "dev", DIGEST_B, release_path=path)
    assert len(fake.cfn.mutations) == 1
    record = common.history(path)[0]
    assert record["status"] == "failed" and record["image_digest"] == DIGEST_B


def test_no_image_cannot_remove_running_app_and_missing_secrets_stop(
    tmp_path: Path, operator: None
) -> None:
    fake = FakeAws()
    with pytest.raises(common.OperationError, match="would remove"):
        deploy.deploy(fake, "dev", None, release_path=tmp_path / "dev.json")
    fake.ssm.values.clear()
    with pytest.raises(common.OperationError, match="Missing SSM"):
        deploy.deploy(fake, "dev", DIGEST_A, release_path=tmp_path / "dev.json")
    assert not fake.cfn.mutations


def test_rollback_selects_previous_distinct_passing_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [
        {"image_digest": DIGEST_A, "status": "passed", "schema_version": 1},
        {"image_digest": DIGEST_A, "status": "passed", "schema_version": 1},
        {"image_digest": DIGEST_B, "status": "failed", "schema_version": 1},
    ]
    assert common.previous_release(records) == records[1]
    with pytest.raises(common.OperationError, match="No previous distinct"):
        common.previous_release(records[:2])
    calls = []
    monkeypatch.setattr(rollback, "history", lambda _: records)
    monkeypatch.setattr(rollback, "session", lambda: "synthetic-aws")
    monkeypatch.setattr(rollback, "deploy", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr("sys.argv", ["rollback.py", "--env", "dev"])
    rollback.main()
    assert calls == [(("synthetic-aws", "dev", DIGEST_A), {"action": "rollback"})]


@pytest.mark.parametrize(
    "value", ["latest", "sha256:bad", "https://other.example/image", "sha256:" + "A" * 64]
)
def test_mutable_or_foreign_image_arguments_refused(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        common.digest(value)


def test_build_archive_excludes_secrets_caches_symlinks_and_private_history(tmp_path: Path) -> None:
    for name in (
        "src/module.py",
        ".env",
        ".env.backup",
        "local.env",
        "lane/history.md",
        ".venv/file.py",
        ".git/config",
        ".tools/jar",
        "credentials-copy.json",
        "deploy/releases/dev.json",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic fixture")
    (tmp_path / "linked.py").symlink_to(tmp_path / ".env")
    data, hash = build.source_archive(tmp_path)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["src/module.py"]
    assert build.source_archive(tmp_path) == (data, hash)
    (tmp_path / "src/module.py").write_text("changed")
    assert build.source_archive(tmp_path)[1] != hash


@pytest.mark.parametrize("failure", ["throttle", "timeout", "connect_timeout", "server"])
def test_relay_skips_contention_but_other_failures_raise(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(relay, "_secret", "synthetic-tick-secret")
    monkeypatch.setenv("APP_URL", "https://synthetic.lambda-url.us-east-1.on.aws/")

    def fail(*args: Any, **kwargs: Any) -> Any:
        if failure == "timeout":
            raise TimeoutError()
        if failure == "connect_timeout":
            raise urllib.error.URLError(TimeoutError())
        raise urllib.error.HTTPError(
            "synthetic", 429 if failure == "throttle" else 503, "synthetic", Message(), None
        )

    monkeypatch.setattr("urllib.request.urlopen", fail)
    if failure == "server":
        with pytest.raises(RuntimeError, match="tick HTTP 503"):
            relay.handler({}, None)
    else:
        assert "skipped" in relay.handler({}, None)


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "TIMED_OUT", "FAULT", "STOPPED"])
def test_cloud_build_pins_source_version_and_reports_failed_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    class CloudBuildFake(FakeAws):
        def __init__(self) -> None:
            super().__init__()
            self.image_reads = 0
            self.started: dict[str, Any] = {}

        def describe_images(self, **args: Any) -> dict[str, Any]:
            self.image_reads += 1
            if self.image_reads == 1:
                raise aws_error("ImageNotFoundException")
            return super().describe_images(**args)

        def start_build(self, **args: Any) -> dict[str, Any]:
            self.started = args
            return {"build": {"id": "synthetic-build"}}

        def batch_get_builds(self, **args: Any) -> dict[str, Any]:
            return {
                "builds": [
                    {
                        "currentPhase": "COMPLETED",
                        "buildStatus": status,
                        "logs": {"groupName": "synthetic", "streamName": "build"},
                    }
                ]
            }

        def get_log_events(self, **args: Any) -> dict[str, Any]:
            return {"events": [{"message": "synthetic failing phase"}]}

    fake = CloudBuildFake()
    (tmp_path / "module.py").write_text("synthetic source")
    monkeypatch.setattr(build, "ROOT", tmp_path)
    monkeypatch.setattr(build, "git_sha", lambda: "a" * 40)
    if status == "SUCCEEDED":
        result = build.build(fake, "dev", root=tmp_path)
        assert result["image_digest"] == DIGEST_A and result["image_size_bytes"] == 100000000
    else:
        with pytest.raises(common.OperationError, match=status):
            build.build(fake, "dev", root=tmp_path)
        assert "synthetic failing phase" in capsys.readouterr().out
        assert not (tmp_path / "deploy/releases/dev-build.json").exists()
    assert fake.started["sourceVersion"] == "synthetic-version"
    assert fake.uploads[0]["Key"] == "builds/" + "a" * 40 + ".zip"


def test_relay_request_matches_shared_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanad.ops.tick_signing import authentic

    captured = []

    class Response(io.BytesIO):
        status = 200

    def receive(request: Any, **kwargs: Any) -> Response:
        captured.append(request)
        return Response(b'{"accepted":true}')

    monkeypatch.setattr(relay, "_secret", "synthetic-tick-secret")
    monkeypatch.setenv("APP_URL", "https://synthetic.lambda-url.us-east-1.on.aws/")
    monkeypatch.setattr("urllib.request.urlopen", receive)
    event = {"timestamp": "1788720000", "nonce": "a1" * 32}
    assert relay.handler(event, None)["status"] == 200
    request = captured[0]
    assert authentic(
        "synthetic-tick-secret",
        {k.lower(): v for k, v in request.header_items()},
        request.data,
        1788720000,
    )


def test_secret_bootstrap_preserves_generated_secrets_and_never_prints_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from deploy import ops

    fake = FakeAws(exists=False)
    fake.ssm.values.clear()
    fake.cfn.exists = True
    local = {
        "GEMINI_API_KEY": "synthetic-gemini",
        "TELEGRAM_BOT_TOKEN_SANAD_STRANDS": "4242:synthetic-secret-token",
        "SANAD_ADMIN_TELEGRAM_USER_ID": "10001",
        "SANAD_TELEGRAM_BOT_USERNAME": "synthetic_bot",
        "SANAD_BUDGET_EMAIL": "synthetic@example.test",
    }
    monkeypatch.setattr(ops, "env_values", lambda _: local)
    ops.secrets_set(fake, "dev")
    first = dict(fake.ssm.values)
    ops.secrets_set(fake, "dev")
    assert first == fake.ssm.values and len(fake.ssm.puts) == 8
    assert len(first["/sanad/dev/tick-secret"]) == len(first["/sanad/dev/webhook-secret"]) == 64
    printed = capsys.readouterr().out
    assert all(value not in printed for value in first.values())


def test_configuration_revision_detects_recreated_parameters_without_hashing_values() -> None:
    class MetadataSSM(FakeSSM):
        modified = "2026-09-06T00:00:00+00:00"

        def get_parameters(self, **kwargs: Any) -> dict[str, Any]:
            result = super().get_parameters(**kwargs)
            for parameter in result["Parameters"]:
                parameter["LastModifiedDate"] = self.modified
            return result

    ssm = MetadataSSM()
    first = common.configuration_revision(ssm, "dev")
    assert first == common.configuration_revision(ssm, "dev")
    ssm.modified = "2026-09-06T01:00:00+00:00"
    assert first != common.configuration_revision(ssm, "dev")


@pytest.mark.parametrize("fail", [False, True])
def test_retained_bucket_cleanup_handles_versions_markers_and_uploads(fail: bool) -> None:
    from deploy.cleanup import empty_bucket

    class S3:
        def __init__(self) -> None:
            self.deleted: list[dict[str, str]] = []
            self.aborted = False

        def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
            return (
                {}
                if self.deleted
                else {
                    "Versions": [{"Key": "synthetic", "VersionId": "version"}],
                    "DeleteMarkers": [{"Key": "synthetic", "VersionId": "marker"}],
                }
            )

        def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
            self.deleted.extend(kwargs["Delete"]["Objects"])
            return {"Errors": [{"Code": "AccessDenied"}]} if fail else {}

        def list_multipart_uploads(self, **kwargs: Any) -> dict[str, Any]:
            return {} if self.aborted else {"Uploads": [{"Key": "synthetic", "UploadId": "upload"}]}

        def abort_multipart_upload(self, **kwargs: Any) -> None:
            self.aborted = True

    s3 = S3()
    if fail:
        with pytest.raises(common.OperationError, match="bucket retained"):
            empty_bucket(s3, "synthetic")
        assert not s3.aborted
    else:
        assert empty_bucket(s3, "synthetic") == 2 and s3.aborted
        assert {x["VersionId"] for x in s3.deleted} == {"version", "marker"}
