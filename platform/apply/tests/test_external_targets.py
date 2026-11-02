"""P3: rgw_bucket_policy and openbao_policy are in T31's scope now. Scope rules (pure) and the stdlib SigV4 signer."""

from __future__ import annotations

import hashlib

import pytest

from bravo_apply import digests, policies, targets
from bravo_apply.runtime import Stop

BUCKET = "bravo-demo-attachments"
APP = "arn:aws:iam:::user/obc-chat-bravo-demo-attachments-1"
DEST = {"namespace": "chat", "policy_types": ["k8s_rbac", "external_secret", "rgw_bucket_policy", "openbao_policy"],
        "object_store": {"bucket": BUCKET, "secret": "envs/target-onprem/object-store",
                         "rgw_service": "rook-ceph/rook-ceph-rgw-bravo"},
        "secret_store": {"kv_mount": "secret", "path_prefix": "envs/target-onprem/", "policy_prefix": "app-target-onprem-"}}
POLICY = {"Version": "2012-10-17", "Statement": [
    {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
     "Resource": [f"arn:aws:s3:::{BUCKET}", f"arn:aws:s3:::{BUCKET}/*"],
     "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
    {"Sid": "AppReadWrite", "Effect": "Allow", "Principal": {"AWS": [APP]}, "Action": ["s3:GetObject", "s3:PutObject"],
     "Resource": f"arn:aws:s3:::{BUCKET}/*"}]}
BAO = {"path": {"secret/data/envs/target-onprem/chat-db": {"capabilities": ["read"]}}}


def _items(*pairs: tuple[str, str, dict]) -> list[dict]:
    return [{"source": {"type": "aws_s3_bucket_policy", "id": "x"}, "target": {"type": t, "name": n}, "document": d,
             "control_ids": ["IAM-01"]} for t, n, d in pairs]


def test_bucket_and_openbao_policies_are_planned_for_apply() -> None:
    items = _items(("rgw_bucket_policy", BUCKET, POLICY), ("openbao_policy", "chat-db-read", BAO))
    plan = policies.scoped(items, digests.policy_diff_digest({"translations": items}), DEST)
    assert plan["rgw_bucket_policy"] == [(BUCKET, POLICY)] and plan["openbao_policy"] == [("chat-db-read", BAO)]
    assert targets.openbao_policy_name("chat-db-read", DEST["secret_store"]) == "app-target-onprem-chat-db-read"
    assert targets.openbao_hcl(BAO).endswith('path "secret/data/envs/target-onprem/chat-db" { capabilities = ["read"] }\n')


@pytest.mark.parametrize(("ttype", "name", "doc", "problem"), [
    ("rgw_bucket_policy", BUCKET, {"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
                                                  "Resource": f"arn:aws:s3:::{BUCKET}/*"}]}, "anonymous_allow"),
    ("rgw_bucket_policy", BUCKET, {"Statement": [{"Effect": "Allow", "Principal": {"AWS": [APP]}, "Action": "s3:*",
                                                  "Resource": f"arn:aws:s3:::{BUCKET}/*"}]}, "wildcard_action"),
    ("rgw_bucket_policy", BUCKET, {"Statement": [{"Effect": "Deny", "Principal": "*", "Action": "s3:GetObject",
                                                  "Resource": "arn:aws:s3:::bravo-chat-db-backup/*"}]},
     "resource_out_of_scope"),
    ("openbao_policy", "chat-db-read", {"path": {"secret/data/envs/onprem/chat-db": {"capabilities": ["read"]}}},
     "path_out_of_scope"),
    ("openbao_policy", "chat-db-read", {"path": {"secret/data/envs/target-onprem/chat-db": {"capabilities": ["update"]}}},
     "capabilities_out_of_scope"),
    ("openbao_policy", "chat-db-read", {"path": {"sys/policies/acl/*": {"capabilities": ["read"]}}}, "path_out_of_scope"),
])
def test_out_of_scope_policies_are_refused_before_anything_is_applied(ttype: str, name: str, doc: dict,
                                                                      problem: str) -> None:
    items = _items((ttype, name, doc))
    with pytest.raises(Stop) as e:
        policies.scoped(items, digests.policy_diff_digest({"translations": items}), DEST)
    assert e.value.reason == "policy_out_of_scope"
    assert any(problem in p for p in e.value.detail["problems"])


def test_sigv4_matches_aws_published_example() -> None:
    """AWS SigV4 S3 example "GET Bucket Lifecycle" (host, x-amz-content-sha256, x-amz-date signed)."""
    h = targets.sign("GET", "examplebucket.s3.amazonaws.com", "/", "lifecycle=", hashlib.sha256(b"").hexdigest(),
                     "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "20130524T000000Z")
    assert h["Authorization"].endswith("Signature=fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543")


def test_diff_headers_dated_1970_still_apply(tmp_path) -> None:
    """The rehearsal snapshot has mtime 0: the sandbox's `diff -ruN src app` dates the old side 1970-01-01, which git
    apply reads as a new file. T31 drops header timestamps before applying (the bound diff digest is unchanged)."""
    import subprocess

    from bravo_apply import build

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\ny = 2\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    diff = ("diff -ruN src/src/a.py app/src/a.py\n--- src/src/a.py\t1970-01-01 00:00:00.000000000 +0000\n"
            "+++ app/src/a.py\t2026-11-02 18:19:07.457184092 +0000\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3\n")
    step = build.apply_diff(tmp_path, diff, ["src/a.py"])
    assert step.detail["files"] == ["src/a.py"] and (tmp_path / "src" / "a.py").read_text() == "x = 1\ny = 3\n"


def test_openbao_policy_in_hcl_text_is_read_and_scoped() -> None:
    hcl = {"name": "app-target-onprem-chat-db", "policy": 'path "secret/data/envs/target-onprem/chat-db" {\n'
           '  capabilities = ["read"]\n}\npath "secret/metadata/envs/target-onprem/chat-db" {\n  capabilities = ["read"]\n}\n'}
    items = _items(("openbao_policy", "app-target-onprem-chat-db", hcl), ("rgw_bucket_policy", "demo-attachments", POLICY))
    plan = policies.scoped(items, digests.policy_diff_digest({"translations": items}), DEST)
    [(name, doc)] = plan["openbao_policy"]
    assert name == "app-target-onprem-chat-db" and sorted(doc["path"]) == [
        "secret/data/envs/target-onprem/chat-db", "secret/metadata/envs/target-onprem/chat-db"]
    assert targets.openbao_policy_name(name, DEST["secret_store"]) == "app-target-onprem-chat-db"
    bad = {"policy": hcl["policy"] + 'path "sys/*" { capabilities = ["sudo"] }\n'}
    with pytest.raises(Stop):
        items = _items(("openbao_policy", "x", bad))
        policies.scoped(items, digests.policy_diff_digest({"translations": items}), DEST)
