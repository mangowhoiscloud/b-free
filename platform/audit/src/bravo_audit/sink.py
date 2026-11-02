"""Write-once audit records through the S3 Object Lock API (AWS S3 and Ceph RGW use the same calls).

Key:   events/<plan_id>/<event_id:012d>.json      (event ids are a per-plan sequence, so keys sort in order)
Body:  the event as canonical JSON (sorted keys, UTF-8), unchanged from the API
Lock:  ObjectLockMode + ObjectLockRetainUntilDate on every PUT (the bucket default applies too)
Check: x-amz-checksum-sha256 on PUT (Object Lock PUTs need an integrity checksum); metadata ``sha256`` (hex)
A key that already exists is never written again: same content -> "exists", different -> "conflict".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

PREFIX = "events/"


def canonical(event: dict[str, Any]) -> bytes:
    return json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def event_key(event: dict[str, Any]) -> str:
    plan, eid = str(event["plan_id"]), int(event["id"])
    if not plan or "/" in plan or eid < 0:
        raise ValueError("event needs a plan_id without '/' and a non-negative integer id")
    return f"{PREFIX}{plan}/{eid:012d}.json"


@dataclass(frozen=True, slots=True)
class PutResult:
    key: str
    outcome: str  # written | exists | conflict
    sha256: str
    version_id: str | None = None
    retain_until: str | None = None


class ObjectLockSink:
    def __init__(self, bucket: str, *, region: str, endpoint_url: str | None = None, mode: str = "COMPLIANCE",
                 retention_days: int = 1, session: boto3.Session | None = None) -> None:
        if mode not in ("COMPLIANCE", "GOVERNANCE"):
            raise ValueError("mode must be COMPLIANCE or GOVERNANCE")
        self.bucket, self.mode, self.days = bucket, mode, retention_days
        self.s3 = (session or boto3.Session()).client(
            "s3", region_name=region, endpoint_url=endpoint_url,
            config=Config(retries={"max_attempts": 4, "mode": "standard"}, s3={"addressing_style": "path"}
                          if endpoint_url else {}))

    def check_bucket(self) -> dict[str, Any]:
        """Object Lock must be enabled with a default retention, or the sink refuses to start."""
        cfg = self.s3.get_object_lock_configuration(Bucket=self.bucket)["ObjectLockConfiguration"]
        if cfg.get("ObjectLockEnabled") != "Enabled":
            raise RuntimeError(f"bucket {self.bucket} has no Object Lock")
        return cfg

    def _head(self, key: str) -> dict[str, Any] | None:
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if str(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")) == "404":
                return None
            raise

    def put_event(self, event: dict[str, Any], *, now: datetime | None = None) -> PutResult:
        key, body = event_key(event), canonical(event)
        digest = hashlib.sha256(body).hexdigest()
        head = self._head(key)
        if head is not None:
            same = head.get("Metadata", {}).get("sha256") == digest
            return PutResult(key, "exists" if same else "conflict", digest, head.get("VersionId"),
                             str(head.get("ObjectLockRetainUntilDate")))
        until = (now or datetime.now(UTC)) + timedelta(days=self.days)
        resp = self.s3.put_object(
            Bucket=self.bucket, Key=key, Body=body, ContentType="application/json",
            ChecksumAlgorithm="SHA256", Metadata={"sha256": digest, "kind": str(event.get("kind", ""))},
            ObjectLockMode=self.mode, ObjectLockRetainUntilDate=until,
        )
        return PutResult(key, "written", digest, resp.get("VersionId"), until.isoformat(timespec="seconds"))

    def last_event_id(self, plan_id: str) -> int | None:
        """Highest event id already stored for the plan (resume point; no local state)."""
        last = None
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket,
                                                                       Prefix=f"{PREFIX}{plan_id}/"):
            for obj in page.get("Contents", []):
                name = obj["Key"].rsplit("/", 1)[-1].removesuffix(".json")
                if name.isdigit():
                    last = max(last or 0, int(name))
        return last

    def verify(self, plan_id: str) -> dict[str, Any]:
        """Re-read every stored record: content hash, lock mode and retain-until. Read-only."""
        rows, bad = [], []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket,
                                                                       Prefix=f"{PREFIX}{plan_id}/"):
            for obj in page.get("Contents", []):
                got = self.s3.get_object(Bucket=self.bucket, Key=obj["Key"])
                body = got["Body"].read()
                digest = hashlib.sha256(body).hexdigest()
                ok = digest == got.get("Metadata", {}).get("sha256")
                row = {"key": obj["Key"], "sha256_ok": ok, "lock_mode": got.get("ObjectLockMode"),
                       "retain_until": str(got.get("ObjectLockRetainUntilDate")),
                       "kind": json.loads(body).get("kind")}
                rows.append(row)
                if not ok or got.get("ObjectLockMode") not in ("COMPLIANCE", "GOVERNANCE"):
                    bad.append(obj["Key"])
        return {"plan_id": plan_id, "records": len(rows), "bad": bad, "rows": rows}
