"""Attachment objects in an S3-compatible store through the AWS SDK (boto3), D-04.

* The key is the content hash: ``att/<sha256>``. A key is never overwritten: the upload is a
  conditional ``PutObject`` (``If-None-Match: *``), and an existing key is only read back.
* No presigned URLs, no multipart upload: the app streams bytes in and out itself, and an
  object is at most ``ATTACHMENT_MAX_BYTES`` (≤ 5 MiB), so a single PutObject is enough.
* The endpoint, region, bucket and credentials come from the environment ("config swap"
  dependency in §8.10). Without explicit keys boto3 uses its default chain (IRSA, Pod Identity).
"""

from __future__ import annotations

import hashlib
import logging

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import S3Settings

log = logging.getLogger(__name__)

_EXISTS_CODES = {"PreconditionFailed", "412", "ConditionalRequestConflict", "409"}
_MISSING_CODES = {"NoSuchKey", "404", "NotFound"}


class StoreError(RuntimeError):
    """The object store failed or returned bytes that do not match the hash."""


class ObjectMissing(StoreError):
    """The key is not in this store (for example, not copied to this environment yet)."""


def object_key(sha256_hex: str) -> str:
    return f"att/{sha256_hex}"


class ObjectStore:
    def __init__(self, settings: S3Settings) -> None:
        self.settings = settings
        self.store_id = settings.store_id
        self.bucket = settings.bucket
        cfg = Config(
            region_name=settings.region,
            s3={"addressing_style": settings.addressing_style},
            # Only send/validate checksums when the API requires them: several S3-compatible
            # stores reject the newer default CRC trailers. Integrity is checked with SHA-256 below.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
            config=cfg,
        )

    @staticmethod
    def _code(exc: ClientError) -> str:
        err = exc.response.get("Error", {})
        return str(err.get("Code") or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if self._code(exc) in _MISSING_CODES:
                return False
            raise StoreError(f"HeadObject failed: {self._code(exc)}") from exc

    def put_if_absent(self, key: str, body: bytes, content_type: str) -> bool:
        """Store ``body`` under ``key`` unless the key exists. Returns True if this call wrote it."""
        if self.exists(key):
            return False
        kwargs: dict[str, object] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
            "ContentLength": len(body),
        }
        if self.settings.conditional_put:
            kwargs["IfNoneMatch"] = "*"
        try:
            self.client.put_object(**kwargs)
            return True
        except ClientError as exc:
            if self._code(exc) in _EXISTS_CODES:
                return False  # a concurrent upload of the same content won the race
            raise StoreError(f"PutObject failed: {self._code(exc)}") from exc

    def read(self, key: str, max_bytes: int) -> tuple[bytes, str | None]:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if self._code(exc) in _MISSING_CODES:
                raise ObjectMissing(key) from exc
            raise StoreError(f"GetObject failed: {self._code(exc)}") from exc
        body = resp["Body"]
        try:
            data = body.read(max_bytes + 1)
        finally:
            body.close()
        if len(data) > max_bytes:
            raise StoreError("object is larger than the attachment limit")
        return data, resp.get("ContentType")

    def verify(self, key: str, sha256_hex: str, max_bytes: int) -> None:
        """Read the stored object back and compare its SHA-256 with the key (D-04 step 2)."""
        data, _ = self.read(key, max_bytes)
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256_hex:
            raise StoreError("stored object does not match its SHA-256 key")

    def ping(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            raise StoreError(f"HeadBucket failed: {self._code(exc)}") from exc
