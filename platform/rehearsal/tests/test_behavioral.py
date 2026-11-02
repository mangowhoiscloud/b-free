"""B7: behavioural verification Job for proposed RGW bucket policies (rendering, SigV4, verdict rule).

The Job itself runs on the destination cluster against RGW; that run is recorded in
docs/evidence/s1-s2-bucket (repository root). Here: the pure parts and how its record changes the verdict.
"""

from __future__ import annotations

import hashlib
from typing import Any

from bravo_rehearsal import behavioral, harbor_task
from bravo_rehearsal.verify_job_s3 import sign

from .test_harbor import accept, bound_trial

TEST_OWNER = "arn:aws:iam:::user/bravo-rehearsal-verify"
APP = "arn:aws:iam:::user/obc-chat-bravo-demo-attachments-1"
PIN = "sha256:" + "b" * 64  # test_harbor.VERIFIER digest


def test_render_points_policy_at_test_bucket_and_test_owner() -> None:
    doc = {"Version": "2012-10-17", "Statement": [
        {"Sid": "DenyInsecure", "Effect": "Deny", "Principal": {"AWS": ["*"]}, "Action": "s3:*",
         "Resource": ["arn:aws:s3:::bravo-demo-attachments", "arn:aws:s3:::bravo-demo-attachments/*"],
         "Condition": {"BoolIfExists": {"aws:SecureTransport": "false"}}},
        {"Sid": "AppOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:GetObject",
         "Resource": "arn:aws:s3:::chat-attachments/att/*", "Condition": {"ArnNotLike": {"aws:PrincipalArn": [APP]}}},
        {"Sid": "Open", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::x/*"}]}
    out = behavioral.render(doc, test_bucket="bravo-rehearsal-verify", principal_map={APP: TEST_OWNER})
    st = out["Statement"]
    assert st[0]["Resource"] == ["arn:aws:s3:::bravo-rehearsal-verify", "arn:aws:s3:::bravo-rehearsal-verify/*"]
    assert st[1]["Resource"] == "arn:aws:s3:::bravo-rehearsal-verify/att/*"
    assert st[1]["Condition"]["ArnNotLike"]["aws:PrincipalArn"] == [TEST_OWNER]
    assert st[2]["Principal"] == "*" and st[2]["Effect"] == "Allow"  # an open grant is tested as written
    assert doc["Statement"][0]["Resource"][0] == "arn:aws:s3:::bravo-demo-attachments"  # input unchanged
    art = {"translations": [{"target": {"type": "k8s_rbac"}, "document": {}},
                            {"target": {"type": "rgw_bucket_policy"}, "document": doc}]}
    assert behavioral.proposed_policies(art) == [doc] and behavioral.proposed_policies(None) == []


def test_sigv4_matches_aws_published_example() -> None:
    """AWS SigV4 S3 example "GET Bucket Lifecycle" (host, x-amz-content-sha256, x-amz-date signed)."""
    h = sign("GET", "examplebucket.s3.amazonaws.com", "/", "lifecycle=", hashlib.sha256(b"").hexdigest(),
             "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "20130524T000000Z")
    assert h["Authorization"].endswith(
        "Signature=fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543")


def job_record(**kw: Any) -> dict[str, Any]:
    rec = {"ran": True, "reason": None, "namespace": "verify-11111111-1-job", "pod": "verify-job-x", "pod_uid": "u2",
           "image_id": "docker.io/library/python@" + PIN, "control_ok": True, "egress_blocked": True,
           "checks": [{"name": "behavioral:policy_applied", "ok": True, "detail": "204"},
                      {"name": "behavioral:anonymous_get_denied", "ok": True, "detail": "403 AccessDenied"},
                      {"name": "behavioral:authenticated_get_allowed", "ok": True, "detail": "200"}]}
    rec.update(kw)
    return rec


def test_verdict_with_behavioral_job(tmp_path) -> None:  # noqa: ANN001
    trial, binding, vdir = bound_trial(tmp_path / "pass")
    trial.verifier_kind = harbor_task.STATIC_RULES
    accept(trial, binding, vdir, behavioral=job_record())
    assert trial.verdict == "PASS" and trial.verifier_kind == behavioral.KIND
    assert trial.verifier["verifier_kind"] == "behavioral+static_rules"
    assert [c["name"] for c in trial.verifier["checks"]][-1] == "behavioral:authenticated_get_allowed"

    trial, binding, vdir = bound_trial(tmp_path / "open")
    checks = job_record()["checks"]
    checks[1] = {"name": "behavioral:anonymous_get_denied", "ok": False, "detail": "200"}
    accept(trial, binding, vdir, behavioral=job_record(checks=checks))
    assert (trial.verdict, trial.reason) == ("FAIL", "verifier:behavioral:anonymous_get_denied")

    # Required but not run, or not trustworthy: never a static-only PASS.
    for kw, reason in [({"ran": False, "reason": "secret_missing"}, "behavioral_job:secret_missing"),
                       ({"image_id": "docker.io/other@sha256:" + "c" * 64}, "behavioral_image_mismatch"),
                       ({"control_ok": False}, "behavioral_control_failed"),
                       ({"egress_blocked": False}, "behavioral_job_egress_open")]:
        trial, binding, vdir = bound_trial(tmp_path / reason)
        trial.verifier_kind = harbor_task.STATIC_RULES
        accept(trial, binding, vdir, behavioral=job_record(**kw))
        assert (trial.verdict, trial.reason) == ("INFRA_ERROR", reason)
        assert trial.verifier_kind == "static_rules"

    # No bucket policy in the proposal: no Job, static rules only (unchanged).
    trial, binding, vdir = bound_trial(tmp_path / "none")
    trial.verifier_kind = harbor_task.STATIC_RULES
    accept(trial, binding, vdir, behavioral=None)
    assert trial.verdict == "PASS" and trial.verifier["verifier_kind"] == "static_rules"
