"""Explicit private-bucket dependency, with scope-constrained content-addressed keys."""

import re
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Protocol, cast
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


class UploadS3Client(S3Client, Protocol):
    def delete_object(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass
class S3MediaStore:
    bucket: str
    client: S3Client = field(repr=False)

    def _upload_key(self, scope: PatientScope, upload_id: str, digest: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id) or not re.fullmatch(
            r"[0-9a-f]{64}", digest
        ):
            raise ValueError("invalid_upload_reference")
        return prefix(scope) + "uploads/" + upload_id + "/" + digest

    def upload_reference(self, scope: PatientScope, upload_id: str, digest: str) -> str:
        return f"s3://{self.bucket}/" + self._upload_key(scope, upload_id, digest)

    def put_upload(
        self, scope: PatientScope, upload_id: str, digest: str, data: bytes, mime: str
    ) -> str:
        if sha256(data).hexdigest() != digest:
            raise ValueError("invalid_upload_digest")
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._upload_key(scope, upload_id, digest),
            Body=data,
            ContentType=mime,
            ServerSideEncryption="AES256",
        )
        return self.upload_reference(scope, upload_id, digest)

    def get_upload(
        self, scope: PatientScope, upload_id: str, digest: str, limit: int
    ) -> bytes | None:
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=self._upload_key(scope, upload_id, digest)
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
            if len(data) > limit or sha256(data).hexdigest() != digest:
                raise ValueError("invalid_upload_blob")
            return data
        finally:
            body.close()

    def delete_upload(self, scope: PatientScope, upload_id: str, digest: str) -> None:
        cast(UploadS3Client, self.client).delete_object(
            Bucket=self.bucket, Key=self._upload_key(scope, upload_id, digest)
        )

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

    def put_page(
        self,
        scope: MediaScope,
        receipt_id: str,
        page_index: int,
        renderer_version: str,
        data: bytes,
    ) -> str:
        if not 1 <= page_index <= 10:
            raise ValueError("invalid_page_index")
        variant = (
            f"pages/{sha256(receipt_id.encode()).hexdigest()}/{page_index}/"
            f"{sha256(renderer_version.encode()).hexdigest()}/"
        )
        key = prefix(scope) + variant + sha256(data).hexdigest()
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType="image/png",
            ServerSideEncryption="AES256",
        )
        return f"s3://{self.bucket}/{key}"

    def get(self, scope: MediaScope, reference: str, limit: int) -> bytes:
        root = f"s3://{self.bucket}/"
        if not reference.startswith(root + prefix(scope)):
            raise ValueError("media_scope_mismatch")
        key = reference[len(root) :]
        suffix = key[len(prefix(scope)) :]
        if re.fullmatch(r"pages/[0-9a-f]{64}/(?:[1-9]|10)/[0-9a-f]{64}/[0-9a-f]{64}", suffix):
            suffix = suffix.rsplit("/", 1)[1]
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


class UploadStorage(Protocol):
    """Separate staging IO; old MediaStore implementations remain compatible."""

    def upload_reference(self, scope: PatientScope, upload_id: str, digest: str) -> str: ...
    def put_upload(
        self, scope: PatientScope, upload_id: str, digest: str, data: bytes, mime: str
    ) -> str: ...
    def get_upload(
        self, scope: PatientScope, upload_id: str, digest: str, limit: int
    ) -> bytes | None: ...
    def delete_upload(self, scope: PatientScope, upload_id: str, digest: str) -> None: ...
