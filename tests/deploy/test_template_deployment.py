import io
import json
import zipfile

from deploy.common import PARAMETERS, ROOT
from deploy.deploy import relay_archive
from sanad.store._base import INDEX_FIELDS


def test_template_declares_scoped_stack_and_account_concurrency_cap() -> None:
    source = (ROOT / "deploy/stack.yaml").read_text()
    template = json.loads(source)
    resources = template["Resources"]
    assert not any(r["Type"].startswith("AWS::SSM::") for r in resources.values())
    assert "ReservedConcurrentExecutions" not in source
    assert set(template["Parameters"]["Environment"]["AllowedValues"]) == {"dev", "judge"}
    assert template["Conditions"]["HasImage"]
    app = resources["AppFunction"]["Properties"]
    assert app["Architectures"] == ["arm64"] and app["MemorySize"] == 3008
    assert app["Timeout"] == 120 and resources["AppFunction"]["Condition"] == "HasImage"
    assert resources["RelayFunction"]["Properties"]["Timeout"] == 25
    assert resources["RelayFunction"]["Properties"]["Runtime"] == "python3.12"
    assert resources["AppThrottles"]["Properties"]["MetricName"] == "Throttles"
    assert resources["AppThrottles"]["Properties"]["Period"] == 300
    assert resources["PublicUrlPermission"]["Properties"] == {
        "FunctionName": {"Ref": "AppFunction"},
        "Action": "lambda:InvokeFunctionUrl",
        "Principal": "*",
        "FunctionUrlAuthType": "NONE",
    }
    assert resources["PublicInvokePermission"]["Properties"]["InvokedViaFunctionUrl"] is True
    assert resources["PublicInvokePermission"]["Properties"]["Action"] == "lambda:InvokeFunction"
    assert resources["TickRule"]["Properties"]["ScheduleExpression"] == "rate(1 minute)"
    assert resources["Budget"]["Properties"]["Budget"]["BudgetLimit"] == {
        "Amount": 20,
        "Unit": "USD",
    }
    assert len(PARAMETERS) == 8 and sum(p[0] == "SecureString" for p in PARAMETERS.values()) == 5
    for forbidden in (
        "TELEGRAM_BOT_TOKEN_SANAD_STRANDS=",
        "AWS_SECRET_ACCESS_KEY",
        "SANAD_TICK_SECRET=",
    ):
        assert forbidden not in source
    for name in ("AppLogs", "RelayLogs", "BuildLogs"):
        assert resources[name]["Properties"]["RetentionInDays"] == 30


def test_table_exact_indexes_ttl_backup_and_private_versioned_bucket() -> None:
    resources = json.loads((ROOT / "deploy/stack.yaml").read_text())["Resources"]
    table = resources["Table"]["Properties"]
    assert table["KeySchema"] == [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]
    assert {
        x["IndexName"]: tuple(k["AttributeName"] for k in x["KeySchema"])
        for x in table["GlobalSecondaryIndexes"]
    } == INDEX_FIELDS
    assert all(
        i["Projection"] == {"ProjectionType": "ALL"} for i in table["GlobalSecondaryIndexes"]
    )
    assert table["BillingMode"] == "PAY_PER_REQUEST"
    assert table["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}
    assert table["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
    assert table["DeletionProtectionEnabled"] == {"Fn::If": ["IsJudge", True, False]}
    bucket = resources["Bucket"]["Properties"]
    assert all(bucket["PublicAccessBlockConfiguration"].values())
    assert bucket["VersioningConfiguration"]["Status"] == "Enabled"
    assert (
        bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"][0][
            "ServerSideEncryptionByDefault"
        ]["SSEAlgorithm"]
        == "AES256"
    )
    assert resources["Bucket"]["DeletionPolicy"] == "RetainExceptOnCreate"
    assert resources["Repository"]["Properties"]["EmptyOnDelete"] is True
    policy = json.loads(
        resources["Repository"]["Properties"]["LifecyclePolicy"]["LifecyclePolicyText"]
    )
    assert policy["rules"][0]["selection"]["countNumber"] == 5


def test_bedrock_has_only_thirteen_explicit_measured_arns_and_own_invocation() -> None:
    resources = json.loads((ROOT / "deploy/stack.yaml").read_text())["Resources"]
    statements = resources["AppRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    bedrock = next(s for s in statements if "bedrock:InvokeModel" in s["Action"])
    arns = [r["Fn::Sub"] for r in bedrock["Resource"]]
    assert len(arns) == 13 and not any("*" in arn for arn in arns)
    assert sum(":inference-profile/" in a for a in arns) == 3
    assert sum("mistral.voxtral-small-24b-2507" in a for a in arns) == 1
    assert all(
        any(
            model in a
            for model in (
                "nova-lite-v1:0",
                "nova-pro-v1:0",
                "nova-micro-v1:0",
                "voxtral-small-24b-2507",
            )
        )
        for a in arns
    )
    assert not any(s["Resource"] == "*" for s in statements)
    assert any("dynamodb:ConditionCheckItem" in s["Action"] for s in statements)
    own = next(s for s in statements if "lambda:InvokeFunction" in s["Action"])
    assert own["Resource"]["Fn::Sub"].endswith("function:sanad-${Environment}-app")


def test_relay_zip_packages_exact_shared_signing_source_and_small_runtime_image() -> None:
    with zipfile.ZipFile(io.BytesIO(relay_archive())) as archive:
        assert (
            archive.read("sanad/ops/tick_signing.py")
            == (ROOT / "src/sanad/ops/tick_signing.py").read_bytes()
        )
        assert archive.read("relay.py") == (ROOT / "deploy/relay.py").read_bytes()
    docker = (ROOT / "deploy/Dockerfile").read_text()
    assert "aws-lambda-adapter:0.9.1" in docker and "python:3.12-slim" in docker
    assert "--locked --no-dev" in docker and "uv build" in docker
    assert "whisper" not in docker.lower() and "--no-access-log" in docker


def test_patient_history_scan_is_limited_to_this_table() -> None:
    resources = json.loads((ROOT / "deploy/stack.yaml").read_text())["Resources"]
    statements = resources["AppRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    scan = [s for s in statements if "dynamodb:Scan" in s["Action"]]
    assert scan == [
        {
            "Effect": "Allow",
            "Action": ["dynamodb:Scan"],
            "Resource": {"Fn::GetAtt": ["Table", "Arn"]},
        }
    ]
