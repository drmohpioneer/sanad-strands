"""Patient-scoped private export and deliberate deletion; no age-based retention policy."""

import argparse
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from boto3.dynamodb.types import TypeSerializer  # type: ignore[import-untyped]

from deploy.cleanup import delete_rows, row_body, scan_rows, selection_counts, versioned_objects
from deploy.common import (
    OperationError,
    client,
    command,
    dev_only,
    dev_outputs,
    operator_arguments,
    session,
)
from sanad.domain import PatientScope, Principal, TenantScope
from sanad.media.storage import prefix
from sanad.store import keys
from sanad.store.records import AuditEvent, record_item, to_record


def references_patient(value: Any, patient: str) -> bool:
    if isinstance(value, dict):
        return any(
            (key in {"patient_id", "selected_patient_id", "source_patient_id"} and child == patient)
            or references_patient(child, patient)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(references_patient(child, patient) for child in value)
    return False


def patient_selection(
    ddb: Any,
    table: str,
    scope: PatientScope,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    selected = list(scan_rows(ddb, table, "PK", keys.partition(scope), prefix=False))
    left = []
    audited = False
    tenant = keys.tenant_pk(scope)
    for row in scan_rows(ddb, table, "PK", tenant):
        pk, sk = row["PK"]["S"], row["SK"]["S"]
        if pk != tenant and not pk.startswith(tenant + "#INTAKE#"):
            continue
        body = row_body(row)
        if body.get("event_type") == "PATIENT_RECORDS_DELETE" and (
            "patient:" + scope.patient_id in body.get("source_refs", [])
        ):
            audited = True
        if pk == tenant and sk.startswith("NAME#") and body.get("patient_id") == scope.patient_id:
            selected.append(row)
        elif references_patient(body, scope.patient_id):
            left.append(row)
    for pk_prefix in ("SUBJECT#", "TOKEN#", "SESSION#", "IN#"):
        for row in scan_rows(ddb, table, "PK", pk_prefix):
            body = row_body(row)
            owner = body if pk_prefix == "SUBJECT#" else body.get("scope", {})
            # LoginExchange, Invitation and WebSession use AccountScope; their
            # clinical ownership is stored on the body, not inside that scope.
            if pk_prefix in {"TOKEN#", "SESSION#"} and owner.get("bot_id"):
                owner = body
            if (
                owner.get("doctor_id") == scope.doctor_id
                and owner.get("patient_id") == scope.patient_id
            ):
                selected.append(row)
    return selected, left, audited


def current_objects(s3: Any, bucket: str, patient_prefix: str) -> list[dict[str, str]]:
    result = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=patient_prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].startswith(patient_prefix):
                raise OperationError("Media scope mismatch; export refused")
            result.append({"Key": obj["Key"]})
    return result


def linked_media(
    ddb: Any,
    table: str,
    bucket: str,
    scope: PatientScope,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Export only media explicitly associated to this patient, including intake originals."""
    linked: dict[tuple[str, str], dict[str, Any]] = {}
    objects: dict[str, dict[str, str]] = {}
    for row in rows:
        body = row_body(row)
        if body.get("entity_type") != "patient_media":
            continue
        media_scope = body.get("media_scope", {})
        if "intake_id" not in media_scope:
            continue
        if media_scope.get("doctor_id") != scope.doctor_id:
            raise OperationError("Linked media doctor mismatch; export refused")
        owner = keys.IntakeScope.model_validate(media_scope)
        media_key = keys.intake(owner, "MEDIA", body["media_work_id"])
        found = [
            r
            for r in scan_rows(ddb, table, "PK", media_key.pk, prefix=False)
            if r["SK"]["S"] == media_key.sk
        ]
        if len(found) != 1:
            raise OperationError("Linked original media record missing; export incomplete")
        work = row_body(found[0])
        if work.get("scope") != media_scope or work.get("receipt_id") != body["source_receipt_id"]:
            raise OperationError("Linked media provenance mismatch; export refused")
        linked[(media_key.pk, media_key.sk)] = found[0]
        for field in (
            "source_blob_ref",
            "normalized_blob_ref",
            "transcript_ref",
            "association_ref",
        ):
            reference = work.get(field)
            if not reference:
                continue
            base = f"s3://{bucket}/"
            if not reference.startswith(base + prefix(owner)):
                raise OperationError("Linked media location mismatch; export refused")
            key = reference.removeprefix(base)
            objects[key] = {"Key": key}
    return list(linked.values()), list(objects.values())


def audit_delete(
    ddb: Any,
    table: str,
    scope: PatientScope,
    counts: dict[str, Any],
) -> None:
    now = datetime.now(UTC)
    event_id = "records-delete:" + keys.digest(keys.partition(scope) + now.isoformat())
    event = AuditEvent(
        id=event_id,
        event_id=event_id,
        command_id=event_id,
        scope=TenantScope(doctor_id=scope.doctor_id),
        event_type="PATIENT_RECORDS_DELETE",
        actor=Principal(subject="operator:records", actor_kind="system"),
        created_at=now,
        updated_at=now,
        accepted_at=now,
        source_refs=(
            "patient:" + scope.patient_id,
            "rows:" + str(counts["rows"]),
            "s3_versions:" + str(counts["s3_objects"]),
            "left_in_place:" + str(counts["left_in_place"]["rows"]),
        ),
    )
    serializer = TypeSerializer()
    item = {
        k: serializer.serialize(v) for k, v in record_item(to_record(event, event.scope)).items()
    }
    ddb.put_item(TableName=table, Item=item, ConditionExpression="attribute_not_exists(PK)")


def records(
    aws: Any,
    action: str,
    env: str,
    doctor: str,
    patient: str,
    *,
    out_dir: Path | None = None,
    yes: bool = False,
) -> dict[str, Any]:
    dev_only(env)
    if action not in {"export", "delete"}:
        raise OperationError("Unsupported records action")
    scope = PatientScope(doctor_id=doctor, patient_id=patient)
    out = dev_outputs(aws, env)
    ddb, s3 = client(aws, "dynamodb"), client(aws, "s3")
    table, bucket = out["TableName"], out["BucketName"]
    rows, left, audited = patient_selection(ddb, table, scope)
    exists = any(r["PK"]["S"] == keys.partition(scope) for r in rows)
    if not exists and not (action == "delete" and audited):
        raise OperationError("Patient not found in this doctor's partition; operation refused")
    objects = (
        versioned_objects(s3, bucket, prefix(scope))
        if action == "delete"
        else current_objects(s3, bucket, prefix(scope))
    )
    linked_rows: list[dict[str, Any]] = []
    if action == "export":
        linked_rows, linked_objects = linked_media(ddb, table, bucket, scope, rows)
        objects = list({obj["Key"]: obj for obj in objects + linked_objects}.values())
    elif any(not obj["Key"].startswith(prefix(scope)) for obj in objects):
        raise OperationError("S3 prefix mismatch; deletion refused")
    counts = selection_counts(rows, len(objects)) | {"left_in_place": selection_counts(left, 0)}
    print(json.dumps({"mode": "execute" if yes else "dry-run", **counts}, sort_keys=True))
    if not yes:
        return counts
    if action == "export":
        if out_dir is None or out_dir.exists():
            raise OperationError("Export requires a new --out directory; refusing overwrite")
        out_dir.mkdir(mode=0o700, parents=True)
        # Numeric local names cannot interpret a stored object key as a filesystem path.
        manifest = []
        try:
            for index, obj in enumerate(objects):
                filename = f"media-{index:06d}.bin"
                response = s3.get_object(Bucket=bucket, Key=obj["Key"])
                try:
                    with (out_dir / filename).open("xb") as target:
                        os.chmod(target.name, 0o600)
                        shutil.copyfileobj(response["Body"], target)
                finally:
                    response["Body"].close()
                manifest.append({"key": obj["Key"], "file": filename})
            with (out_dir / "records.json").open("x") as target:
                os.chmod(target.name, 0o600)
                json.dump(
                    {
                        "rows": rows,
                        "linked_media_rows": linked_rows,
                        "media": manifest,
                        "counts": counts,
                    },
                    target,
                    indent=2,
                )
                target.write("\n")
        except Exception:
            # Keep a partial private export for diagnosis; never print its contents.
            raise OperationError("Export incomplete; private partial directory retained") from None
        return counts
    if rows or objects:
        audit_delete(ddb, table, scope, counts)
    for start in range(0, len(objects), 1000):
        result = s3.delete_objects(
            Bucket=bucket, Delete={"Objects": objects[start : start + 1000], "Quiet": True}
        )
        if result.get("Errors"):
            raise OperationError("S3 deletion failed; patient rows retained; rerun records delete")
    delete_rows(ddb, table, rows)
    remaining, _, _ = patient_selection(ddb, table, scope)
    media_left = versioned_objects(s3, bucket, prefix(scope))
    print(json.dumps({"remaining": selection_counts(remaining, len(media_left))}, sort_keys=True))
    if remaining or media_left:
        raise OperationError("Patient rows or media reappeared; deletion not verified; rerun")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("export", "delete"):
        child = sub.add_parser(action)
        operator_arguments(child)
        child.add_argument("--doctor", required=True)
        child.add_argument("--patient", required=True)
        if action == "export":
            child.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    dev_only(args.env)
    if not args.doctor.strip() or not args.patient.strip():
        raise OperationError("Doctor and patient IDs must be nonblank")
    records(
        session(),
        args.action,
        args.env,
        args.doctor,
        args.patient,
        out_dir=getattr(args, "out", None),
        yes=args.yes,
    )


if __name__ == "__main__":
    command(main)
