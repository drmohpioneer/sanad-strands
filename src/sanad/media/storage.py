"""Explicit private-bucket dependency, with scope-constrained content-addressed keys."""

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import quote

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from sanad.domain import PatientScope
from sanad.store.keys import IntakeScope

type MediaScope = PatientScope | IntakeScope


def prefix(scope: MediaScope) -> str:
    owner = quote(scope.doctor_id, safe="")
    subject = (
        quote(scope.patient_id, safe="")
        if isinstance(scope, PatientScope)
        else ("intake/" + quote(scope.intake_id, safe=""))
    )
    return f"{owner}/{subject}/"


class MediaStore(Protocol):
    def put(self, scope: MediaScope, data: bytes, mime: str) -> str: ...
    def get(self, scope: MediaScope, reference: str, limit: int) -> bytes: ...
    def put_read_checkpoint(self, scope: MediaScope, receipt_id: str, data: bytes) -> bytes: ...
    def get_read_checkpoint(
        self, scope: MediaScope, receipt_id: str, limit: int
    ) -> bytes | None: ...


class S3Client(Protocol):
    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass
class S3MediaStore:
    bucket: str
    client: S3Client = field(repr=False)

    def _read_key(self, scope: MediaScope, receipt_id: str) -> str:
        return prefix(scope) + "evidence-reads/" + sha256(receipt_id.encode()).hexdigest()

    def get_read_checkpoint(self, scope: MediaScope, receipt_id: str, limit: int) -> bytes | None:
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=self._read_key(scope, receipt_id)
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise
        body = response["Body"]
        try:
            if response.get("ContentLength", 0) > limit:
                raise ValueError("too_large")
            data: bytes = body.read(limit + 1)
            if len(data) > limit:
                raise ValueError("too_large")
            return data
        finally:
            body.close()

    def put_read_checkpoint(self, scope: MediaScope, receipt_id: str, data: bytes) -> bytes:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._read_key(scope, receipt_id),
                Body=data,
                ContentType="application/json",
                ServerSideEncryption="AES256",
                IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
                raise
        saved = self.get_read_checkpoint(scope, receipt_id, len(data) + 1024 * 1024)
        if saved is None:
            raise ValueError("read_checkpoint_missing")
        return saved

    def put(self, scope: MediaScope, data: bytes, mime: str) -> str:
        key = prefix(scope) + sha256(data).hexdigest()
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=mime, ServerSideEncryption="AES256"
        )
        return f"s3://{self.bucket}/{key}"

    def get(self, scope: MediaScope, reference: str, limit: int) -> bytes:
        root = f"s3://{self.bucket}/"
        if not reference.startswith(root + prefix(scope)):
            raise ValueError("media_scope_mismatch")
        key = reference[len(root) :]
        suffix = key[len(prefix(scope)) :]
        if len(suffix) != 64 or any(c not in "0123456789abcdef" for c in suffix):
            raise ValueError("invalid_blob_reference")
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            if response.get("ContentLength", 0) > limit:
                raise ValueError("too_large")
            data: bytes = body.read(limit + 1)
            if len(data) > limit or sha256(data).hexdigest() != suffix:
                raise ValueError("invalid_blob")
            return data
        finally:
            body.close()
