"""Create infrastructure, or deploy a digest and record real smoke evidence."""

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from deploy.common import (
    ROOT,
    OperationError,
    client,
    command,
    configuration_revision,
    digest,
    env_values,
    environment,
    git_sha,
    history,
    outputs,
    parameter_values,
    record_release,
    revision,
    session,
    timestamp,
    wait_stack,
)
from sanad.store.keys import SCHEMA_KEY, SCHEMA_VERSION


def relay_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for destination, source in {
            "relay.py": ROOT / "deploy/relay.py",
            "sanad/ops/tick_signing.py": ROOT / "src/sanad/ops/tick_signing.py",
        }.items():
            archive.writestr(
                zipfile.ZipInfo(destination, (2026, 1, 1, 0, 0, 0)), source.read_bytes()
            )
        for path in ("sanad/__init__.py", "sanad/ops/__init__.py"):
            archive.writestr(zipfile.ZipInfo(path, (2026, 1, 1, 0, 0, 0)), "")
    return buffer.getvalue()


def check_schema(ddb: Any, table: str, *, initialize: bool) -> int:
    key = {"PK": {"S": SCHEMA_KEY[0]}, "SK": {"S": SCHEMA_KEY[1]}}
    item = ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
    if item is None and initialize:
        try:
            ddb.put_item(
                TableName=table,
                Item=key | {"schema_version": {"N": str(SCHEMA_VERSION)}},
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        return check_schema(ddb, table, initialize=False)
    stored = int(item.get("schema_version", {}).get("N", "0")) if item else 0
    if stored != SCHEMA_VERSION:
        raise OperationError(
            f"schema_version mismatch: code={SCHEMA_VERSION}, table={stored}; "
            "no migration available"
        )
    return stored


def deploy(
    aws: Any,
    env: str,
    image: str | None,
    *,
    action: str = "deploy",
    release_path: Path | None = None,
) -> dict[str, Any]:
    cfn, ddb, ssm = (client(aws, name) for name in ("cloudformation", "dynamodb", "ssm"))
    name = f"sanad-{env}"
    existing = None
    try:
        existing = outputs(cfn, env)
    except ClientError as error:
        if (
            error.response["Error"]["Code"] != "ValidationError"
            or "does not exist" not in error.response["Error"]["Message"]
        ):
            raise
    if image is None and existing and existing.get("ImageDigest"):
        raise OperationError("Infrastructure pass would remove the app; supply its image digest")
    if existing:
        check_schema(ddb, existing["TableName"], initialize=True)
    if image is not None and existing is None:
        raise OperationError(
            "First run deploy.py --env dev without --image to create build infrastructure"
        )
    template = (ROOT / "deploy/stack.yaml").read_text()
    relay = relay_archive()
    relay_key = "relay/" + hashlib.sha256(relay).hexdigest() + ".zip"
    image_uri = ""
    config_revision = "infrastructure-only"
    if image is not None:
        digest(image)
        assert existing is not None
        values, _ = parameter_values(ssm, env)
        config_revision = configuration_revision(ssm, env)
        required = set(values) - {"public-base-url"}
        if len(required) != 6 or not all(values[k] for k in required):
            raise OperationError(
                "Missing SSM configuration; run ops.py secrets set before the app pass"
            )
        client(aws, "s3").put_object(Bucket=existing["BucketName"], Key=relay_key, Body=relay)
        image_uri = existing["RepositoryUri"] + "@" + image
        client(aws, "ecr").describe_images(
            repositoryName=existing["RepositoryUri"].split("/", 1)[1],
            imageIds=[{"imageDigest": image}],
        )
    email = env_values({"SANAD_BUDGET_EMAIL"})["SANAD_BUDGET_EMAIL"]
    key_arn = client(aws, "kms").describe_key(KeyId="alias/aws/ssm")["KeyMetadata"]["Arn"]
    stack_revision = revision(template, relay)
    args = {
        "StackName": name,
        "TemplateBody": template,
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "Parameters": [
            {"ParameterKey": k, "ParameterValue": v}
            for k, v in {
                "Environment": env,
                "ImageUri": image_uri,
                "BudgetEmail": email,
                "StackRevision": stack_revision,
                "RelayCodeKey": relay_key if image else "",
                "SsmKeyArn": key_arn,
                "ConfigRevision": config_revision,
            }.items()
        ],
        "Tags": [{"Key": "Project", "Value": "Sanad"}, {"Key": "Environment", "Value": env}],
    }
    cfn.validate_template(TemplateBody=template)
    if existing:
        try:
            cfn.update_stack(**args)
        except ClientError as error:
            if "No updates are to be performed" not in error.response["Error"]["Message"]:
                raise
            print("Stack no-op; recording and checking the deployed revision")
    else:
        cfn.create_stack(**args, OnFailure="ROLLBACK")
    state = wait_stack(cfn, name)
    print(name, state)
    out = outputs(cfn, env)
    table_schema = check_schema(ddb, out["TableName"], initialize=True)
    destination = release_path or ROOT / "deploy/releases" / f"{env}.json"
    record: dict[str, Any] = {
        "timestamp": timestamp(),
        "action": action,
        "git_sha": git_sha(),
        "image_digest": image,
        "stack_revision": stack_revision,
        "function_url": out.get("FunctionUrl"),
        "schema_version": SCHEMA_VERSION,
        "table_schema_version": table_schema,
        "configuration_revision": config_revision,
    }
    if image is None:
        record.update(
            status="infrastructure",
            smoke={
                k: "SKIPPED: infrastructure only"
                for k in ("health", "tick", "webhook", "tenant", "session", "cost")
            },
        )
    else:
        current_values, _ = parameter_values(ssm, env)
        if current_values.get("public-base-url") != out["FunctionUrl"].rstrip("/"):
            ssm.put_parameter(
                Name=f"/sanad/{env}/public-base-url",
                Value=out["FunctionUrl"].rstrip("/"),
                Type="String",
                Overwrite=True,
            )
        _, versions = parameter_values(ssm, env)
        record["configuration_parameter_versions"] = versions
        build_path = ROOT / "deploy/releases" / f"{env}-build.json"
        build_record = json.loads(build_path.read_text()) if build_path.exists() else {}
        prior = next(
            (r for r in reversed(history(destination)) if r.get("image_digest") == image), {}
        )
        for field in ("source_sha256", "image_size_bytes", "build_id", "image_tag"):
            record[field] = (
                build_record if build_record.get("image_digest") == image else prior
            ).get(field)
        from deploy.smoke import run_smoke

        checks = run_smoke(aws, env, out, image)
        record["smoke"] = checks
        failed = [k for k, v in checks.items() if v.get("status") == "FAILED"]
        record["status"] = "failed" if failed else "passed"
        record_release(destination, record)
        print(json.dumps(record, sort_keys=True))
        if failed:
            raise OperationError(
                "Smoke failed: " + ", ".join(failed) + "; stack remains deployed; run rollback.py"
            )
        return record
    record_release(destination, record)
    print(json.dumps(record, sort_keys=True))
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    environment(parser)
    parser.add_argument("--image", type=digest, help="omit for the first infrastructure pass")
    args = parser.parse_args()
    deploy(session(), args.env, args.image)


if __name__ == "__main__":
    command(main)
