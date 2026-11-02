"""FR-1 AWS collection against the real account, read-only. Skips without credentials."""

from __future__ import annotations

import os
import re
from pathlib import Path

import boto3
import pytest

from bravo_collector.aws import AwsCollector
from bravo_collector.environments import AwsSection
from bravo_collector.model import Item

pytestmark = pytest.mark.aws
AWS_SRC = Path(__file__).resolve().parents[1] / "src" / "bravo_collector" / "aws.py"


def test_aws_module_is_read_only_by_construction():
    src = AWS_SRC.read_text(encoding="utf-8")
    assert "get_secret_value" not in src
    for verb in ("put_", "delete_", "create_", "update_", "tag_", "untag_", "attach_", "detach_"):
        assert not re.search(rf"\.{verb}\w*\(", src), f"write-like call {verb} found in aws.py"


@pytest.fixture(scope="module")
def session() -> boto3.session.Session:
    s = boto3.session.Session()
    if s.get_credentials() is None:
        pytest.skip("no AWS credentials in the default chain")
    region = os.environ.get("AWS_REGION") or s.region_name
    if not region:
        pytest.skip("no AWS region configured")
    try:
        boto3.session.Session(region_name=region).client("sts").get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"sts:GetCallerIdentity failed: {type(exc).__name__}")
    return boto3.session.Session(region_name=region)


def _walk(v):
    if isinstance(v, Item):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _walk(x)
    elif isinstance(v, list | tuple):
        for x in v:
            yield from _walk(x)


def test_real_account_snapshot(session):
    buckets = sorted(b["Name"] for b in session.client("s3").list_buckets().get("Buckets", []))[:2]
    section = AwsSection(region=session.region_name, s3_buckets=tuple(buckets))
    data = AwsCollector(section, session=session).collect()
    assert data["caller"].status == "ok"
    assert data["caller"].value["arn"].startswith("arn:aws:")
    assert set(data["s3_buckets"]) == set(buckets)
    for b in data["s3_buckets"].values():
        assert set(b) == {"policy", "policy_status", "public_access_block", "encryption", "versioning"}
    assert isinstance(data["cloudtrail"], Item)
    items = list(_walk(data))
    assert items and all(i.status in {"ok", "absent", "error"} for i in items)


def test_unknown_resources_become_error_items(session):
    section = AwsSection(region=session.region_name, secret_ids=("bravo-collector-test/does-not-exist",),
                         kms_key_ids=("alias/bravo-collector-test-does-not-exist",),
                         iam_role_names=("bravo-collector-test-does-not-exist",),
                         account_public_access_block=False)
    data = AwsCollector(section, session=session).collect()
    assert data["secrets"]["bravo-collector-test/does-not-exist"]["metadata"].status == "error"
    assert data["kms_keys"]["alias/bravo-collector-test-does-not-exist"]["metadata"].status == "error"
    assert data["iam_roles"]["bravo-collector-test-does-not-exist"]["role"].status == "error"
