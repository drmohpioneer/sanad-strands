"""Build the current workspace in the stack's ARM CodeBuild project."""

import argparse
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from deploy.common import (
    ROOT,
    OperationError,
    client,
    command,
    environment,
    git_sha,
    outputs,
    session,
)

EXCLUDED = {
    ".git",
    ".venv",
    ".tools",
    "lane",
    ".agents",
    ".uv-cache",
    ".uv-python",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "node_modules",
    ".DS_Store",
}


def source_archive(root: Path) -> tuple[bytes, str]:
    buffer = io.BytesIO()
    hashed = hashlib.sha256()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        paths: list[Path] = []
        for directory, directories, files in root.walk():
            directories[:] = sorted(
                d for d in directories if d not in EXCLUDED and not (directory / d).is_symlink()
            )
            paths.extend(directory / name for name in sorted(files))
        for path in sorted(paths):
            relative = path.relative_to(root)
            if (
                not path.is_file()
                or path.is_symlink()
                or EXCLUDED.intersection(relative.parts)
                or path.name.startswith(".env")
                or path.suffix in {".env", ".pem", ".pyc", ".wav", ".ogg", ".mp4"}
                or "credentials" in path.name.lower()
                or relative.parts[:2] == ("deploy", "releases")
            ):
                continue
            data = path.read_bytes()
            hashed.update(relative.as_posix().encode() + b"\0" + data)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buffer.getvalue(), hashed.hexdigest()


def build(aws: Any, env: str, *, root: Path = ROOT) -> dict[str, Any]:
    out = outputs(client(aws, "cloudformation"), env)
    source, source_hash = source_archive(root)
    sha = git_sha()
    tag = f"{sha[:12]}-{source_hash[:20]}"
    ecr = client(aws, "ecr")
    repository = out["RepositoryUri"].split("/", 1)[1]
    image = None
    try:
        image = ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": tag}])[
            "imageDetails"
        ][0]
    except ClientError as error:
        if error.response["Error"]["Code"] != "ImageNotFoundException":
            raise
    build_id = None
    if image is None:
        key = f"builds/{sha}.zip"
        uploaded = client(aws, "s3").put_object(Bucket=out["BucketName"], Key=key, Body=source)
        cb = client(aws, "codebuild")
        started = cb.start_build(
            projectName=out["BuildProjectName"],
            sourceLocationOverride=f"{out['BucketName']}/{key}",
            sourceVersion=uploaded["VersionId"],
            environmentVariablesOverride=[{"name": "IMAGE_TAG", "value": tag, "type": "PLAINTEXT"}],
        )
        build_id = started["build"]["id"]
        print("CodeBuild started", build_id)
        last_phase = None
        for _ in range(360):
            state = cb.batch_get_builds(ids=[build_id])["builds"][0]
            if state["currentPhase"] != last_phase:
                last_phase = state["currentPhase"]
                print("CodeBuild phase", last_phase)
            if state["buildStatus"] != "IN_PROGRESS":
                if state["buildStatus"] != "SUCCEEDED":
                    logs = state.get("logs", {})
                    if logs.get("streamName"):
                        tail = client(aws, "logs").get_log_events(
                            logGroupName=logs["groupName"],
                            logStreamName=logs["streamName"],
                            limit=30,
                        )
                        for entry in tail["events"]:
                            # Source archives exclude secrets; also redact token-like URLs.
                            from deploy.ops import redact_log

                            print(redact_log(entry["message"]))
                    raise OperationError("CodeBuild " + state["buildStatus"])
                break
            time.sleep(5)
        else:
            raise OperationError("CodeBuild wait timed out")
        image = ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": tag}])[
            "imageDetails"
        ][0]
    result = {
        "git_sha": sha,
        "source_sha256": source_hash,
        "image_digest": image["imageDigest"],
        "image_size_bytes": image["imageSizeInBytes"],
        "build_id": build_id,
        "image_tag": tag,
    }
    destination = ROOT / "deploy" / "releases" / f"{env}-build.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    environment(parser)
    args = parser.parse_args()
    build(session(), args.env)


if __name__ == "__main__":
    command(main)
