"""Object Lock sink against a real bucket (no stand-in). Set BRAVO_TEST_AUDIT_BUCKET (and BRAVO_AUDIT_REGION,
optional BRAVO_AUDIT_S3_ENDPOINT for RGW). Each run writes 2 small records under events/test-<run>/ that stay
locked for the bucket's retention (1 day by default) and cannot be deleted before then.
"""

from __future__ import annotations

import os
import uuid

import pytest

from bravo_audit.sink import ObjectLockSink, canonical, event_key


def test_key_and_canonical_body() -> None:
    ev = {"plan_id": "p1", "id": 7, "kind": "gate.verdict", "data": {"b": 1, "a": 2}}
    assert event_key(ev) == "events/p1/000000000007.json"
    assert canonical(ev) == canonical(dict(reversed(list(ev.items()))))
    with pytest.raises(ValueError):
        event_key({"plan_id": "a/b", "id": 1})


@pytest.fixture(scope="module")
def sink() -> ObjectLockSink:
    bucket = os.environ.get("BRAVO_TEST_AUDIT_BUCKET", "").strip()
    if not bucket:
        pytest.skip("set BRAVO_TEST_AUDIT_BUCKET to a real Object Lock bucket (AWS S3 or Ceph RGW)")
    s = ObjectLockSink(bucket, region=os.environ.get("BRAVO_AUDIT_REGION", "ap-northeast-2"),
                       endpoint_url=os.environ.get("BRAVO_AUDIT_S3_ENDPOINT") or None)
    s.check_bucket()
    return s


@pytest.mark.s3
def test_write_once_locked_and_verifiable(sink: ObjectLockSink) -> None:
    plan = f"test-{uuid.uuid4().hex[:12]}"
    ev1 = {"id": 1, "plan_id": plan, "kind": "state.changed", "state": "GATE", "mode": "live", "data": {}}
    ev2 = {"id": 2, "plan_id": plan, "kind": "gate.verdict", "state": "GATE", "mode": "live",
           "data": {"verdict": "BLOCK"}}
    r1 = sink.put_event(ev1)
    assert r1.outcome == "written" and r1.version_id
    assert sink.put_event(ev1).outcome == "exists"
    assert sink.put_event({**ev1, "state": "TAMPERED"}).outcome == "conflict"
    sink.put_event(ev2)
    assert sink.last_event_id(plan) == 2
    v = sink.verify(plan)
    assert v["records"] == 2 and v["bad"] == []
    assert {r["lock_mode"] for r in v["rows"]} == {"COMPLIANCE"}
    # A locked version cannot be deleted: S3 answers AccessDenied for a versioned delete.
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        sink.s3.delete_object(Bucket=sink.bucket, Key=r1.key, VersionId=r1.version_id)
