"""Remove the explicitly replaced spike, or an already-deleted stack's retained bucket."""

import argparse
import json
import os
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from deploy.common import OperationError, client, command, environment, session


def absent_ok(call: Any, **kwargs: Any) -> Any:
    try:
        return call(**kwargs)
    except ClientError as error:
        if error.response["Error"]["Code"] not in {
            "ResourceNotFoundException",
            "NoSuchEntity",
            "NoSuchBucket",
            "RepositoryNotFoundException",
        }:
            raise
        return None


def empty_bucket(s3: Any, bucket: str) -> int:
    count = 0
    # Re-list from the start after each deletion; never rely on a deleted version's cursor.
    while True:
        response = absent_ok(s3.list_object_versions, Bucket=bucket, MaxKeys=1000)
        if response is None:
            return count
        objects = [
            {"Key": x["Key"], "VersionId": x["VersionId"]}
            for x in response.get("Versions", []) + response.get("DeleteMarkers", [])
        ]
        if not objects:
            break
        deleted = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
        if deleted.get("Errors"):
            raise OperationError("Bucket version deletion failed; bucket retained")
        count += len(objects)
    while True:
        uploads = s3.list_multipart_uploads(Bucket=bucket).get("Uploads", [])
        if not uploads:
            return count
        for upload in uploads:
            s3.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])


def delete_spikes(aws: Any) -> dict[str, Any]:
    events = client(aws, "events")
    targets = absent_ok(events.list_targets_by_rule, Rule="sanad-spike-tick")
    if targets and targets.get("Targets"):
        result = events.remove_targets(
            Rule="sanad-spike-tick", Ids=[t["Id"] for t in targets["Targets"]]
        )
        if result.get("FailedEntryCount"):
            raise OperationError("Spike tick targets could not be removed")
    absent_ok(events.delete_rule, Name="sanad-spike-tick")
    lam = client(aws, "lambda")
    absent_ok(lam.delete_function_url_config, FunctionName="sanad-spike")
    absent_ok(lam.delete_function, FunctionName="sanad-spike")
    absent_ok(client(aws, "codebuild").delete_project, name="sanad-spike-build")
    absent_ok(client(aws, "ecr").delete_repository, repositoryName="sanad-spike", force=True)
    iam = client(aws, "iam")
    for name in ("sanad-spike-lambda", "sanad-spike-codebuild"):
        inline = absent_ok(iam.list_role_policies, RoleName=name)
        if inline is None:
            continue
        for policy in inline["PolicyNames"]:
            iam.delete_role_policy(RoleName=name, PolicyName=policy)
        for policy in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=name, PolicyArn=policy["PolicyArn"])
        iam.delete_role(RoleName=name)
    s3 = client(aws, "s3")
    bucket = "sanad-spike-" + os.environ["AWS_ACCOUNT_ID"]
    versions = empty_bucket(s3, bucket)
    absent_ok(s3.delete_bucket, Bucket=bucket)
    logs = client(aws, "logs")
    for name in ("/aws/lambda/sanad-spike", "/aws/codebuild/sanad-spike-build"):
        absent_ok(logs.delete_log_group, logGroupName=name)
    return {
        "removed_or_already_absent": [
            "sanad-spike Lambda and function URL",
            "sanad-spike-tick rule and targets",
            "sanad-spike-build CodeBuild project",
            "sanad-spike ECR repository and images",
            "sanad-spike-lambda role and policies",
            "sanad-spike-codebuild role and policies",
            "sanad-spike-<owner-account> bucket",
            "/aws/lambda/sanad-spike log group",
            "/aws/codebuild/sanad-spike-build log group",
        ],
        "bucket_versions_and_delete_markers_removed": versions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("spikes")
    environment(sub.add_parser("retained-bucket"))
    args = parser.parse_args()
    aws = session()
    if args.action == "spikes":
        print(json.dumps(delete_spikes(aws), sort_keys=True))
    else:
        try:
            client(aws, "cloudformation").describe_stacks(StackName=f"sanad-{args.env}")
        except ClientError as error:
            if (
                error.response["Error"]["Code"] != "ValidationError"
                or "does not exist" not in error.response["Error"]["Message"]
            ):
                raise
        else:
            raise OperationError("Delete the stack before removing its retained bucket")
        s3 = client(aws, "s3")
        bucket = f"sanad-{args.env}-{os.environ['AWS_ACCOUNT_ID']}-us-east-1"
        count = empty_bucket(s3, bucket)
        absent_ok(s3.delete_bucket, Bucket=bucket)
        print(f"Retained bucket removed; {count} object versions/delete markers removed")


if __name__ == "__main__":
    command(main)
