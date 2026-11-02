"""Verification Jobs: bucket policies through the gate's own OBJ-01..06 (B7, B11/P14) and the app fix (B05/P11).

The Jobs run on the destination cluster; those runs are recorded in docs/evidence/s2-full (repository root). Here: the
pure parts (rendering, Job objects, diff application) and how a Job record changes the verdict.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from bravo_rehearsal import behavioral, behavioral_app, harbor_task
from bravo_rehearsal.trials import CALL_ORIGIN

from .test_harbor import accept, bound_trial, make_trial

TEST_OWNER = "arn:aws:iam:::user/bravo-rehearsal-verify"
APP = "arn:aws:iam:::user/obc-chat-bravo-demo-attachments-1"
GATE = "registry.example/bravo-platform/gate@sha256:" + "d" * 64


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


def test_gate_job_runs_the_gate_questions_on_the_gate_image() -> None:
    record = {"endpoint": "https://rook-ceph-rgw-bravo.rook-ceph.svc", "bucket": "bravo-rehearsal-verify",
              "owner_arn": TEST_OWNER, "target_app_arn": APP}
    prm = behavioral.params("t-1", record, [{"Statement": []}])
    assert prm["plain_endpoint"] == "http://rook-ceph-rgw-bravo.rook-ceph.svc:80"  # OBJ-06 plain listener
    objs = dict(behavioral.job_objects("ns", "t-1", GATE, prm, "CA", "ak", "sk", 60, other=("oak", "osk")))
    container = objs["job"]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == GATE and container["command"][-1] == "/cfg/job.py"
    assert "bravo_gate.probes.objstore" in objs["configmap"]["data"]["job.py"]
    assert {e["name"] for e in container["env"]} == {"S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "OTHER_ACCESS_KEY_ID",
                                                     "OTHER_SECRET_ACCESS_KEY"}
    assert [p["port"] for p in objs["np"]["spec"]["egress"][0]["ports"]] == [443, 8080]


def job_record(**kw: Any) -> dict[str, Any]:
    rec = {"ran": True, "reason": None, "namespace": "verify-11111111-gov-1-job", "pod": "verify-job-x",
           "pod_uid": "u2", "kind": behavioral.KIND, "pinned_image_digest": GATE.rsplit("@", 1)[1],
           "image_id": GATE, "control_ok": True, "egress_blocked": True,
           "checks": [{"name": "policy_applied", "ok": True, "detail": "204"},
                      {"name": "gate_OBJ-01", "ok": True, "detail": "PASS anonymous_read_denied 403 AccessDenied"},
                      {"name": "gate_OBJ-06", "ok": True, "detail": "PASS plain_http_denied_tls_only"}]}
    rec.update(kw)
    return rec


def test_verdict_with_gate_job(tmp_path: Path) -> None:
    trial, binding, vdir = bound_trial(tmp_path / "pass")
    trial.verifier_kind = harbor_task.STATIC_RULES
    accept(trial, binding, vdir, behavioral=job_record())
    assert trial.verdict == "PASS" and trial.verifier["verifier_kind"] == "behavioral:gate_obj01_06+static_rules"
    assert [c["name"] for c in trial.verifier["checks"]][-1] == "gate_OBJ-06"

    trial, binding, vdir = bound_trial(tmp_path / "open")
    checks = job_record()["checks"]
    checks[1] = {"name": "gate_OBJ-01", "ok": False, "detail": "FAIL anonymous_read_allowed 200"}
    accept(trial, binding, vdir, behavioral=job_record(checks=checks))
    assert (trial.verdict, trial.reason) == ("FAIL", "verifier:gate_OBJ-01")

    # Required but not run, or not trustworthy: never a static-only PASS. The Pod must have run the pinned GATE image.
    for kw, reason in [({"ran": False, "reason": "gate_probe_image_unconfigured"},
                        "behavioral_job:gate_probe_image_unconfigured"),
                       ({"image_id": "docker.io/library/python@sha256:" + "b" * 64}, "behavioral_image_mismatch"),
                       ({"control_ok": False}, "behavioral_control_failed"),
                       ({"egress_blocked": False}, "behavioral_job_egress_open")]:
        trial, binding, vdir = bound_trial(tmp_path / reason)
        trial.verifier_kind = harbor_task.STATIC_RULES
        accept(trial, binding, vdir, behavioral=job_record(**kw))
        assert (trial.verdict, trial.reason) == ("INFRA_ERROR", reason)
        assert trial.verifier_kind == "static_rules"


def test_app_diff_from_the_sandbox_check_applies_exactly(tmp_path: Path) -> None:
    """The sandbox's make_diff (``cd / && diff -ruN src app``) applies with -p1, exactly, or the proposal fails."""
    for side, text in (("src", "a\nsm = boto3.client('secretsmanager')\nc\n"), ("app", "a\npw = read_file()\nc\n")):
        (tmp_path / side / "src" / "pkg").mkdir(parents=True)
        (tmp_path / side / "src" / "pkg" / "s.py").write_text(text)
    diff = subprocess.run(["diff", "-ruN", "src", "app"], cwd=tmp_path, capture_output=True, text=True).stdout
    original = behavioral_app.read_tree(tmp_path / "src")
    patched, changed = behavioral_app.apply_diff(original, diff)
    assert changed == ["src/pkg/s.py"] and patched["src/pkg/s.py"] == "a\npw = read_file()\nc\n"
    with pytest.raises(behavioral_app.DiffError, match="diff_context_mismatch"):
        behavioral_app.apply_diff({"src/pkg/s.py": "other\n"}, diff)
    with pytest.raises(behavioral_app.DiffError, match="diff_path_rejected"):
        behavioral_app.apply_diff(original, diff.replace("--- src/", "--- /src/"))
    tree = {"src/bravo_demo/secrets.py": "client('secretsmanager')", "src/bravo_demo/app.py": "ok"}
    assert behavioral_app.sdk_calls(tree) == ["src/bravo_demo/secrets.py"]


def test_app_job_gives_the_apps_only_the_landing_zone_interface() -> None:
    env = {e["name"]: e["value"] for e in behavioral_app.app_env(8000, 8081)}
    assert env["DB_PASSWORD_FILE"] == "/var/run/bravo/db/password" and "password" not in env["DB_DSN"]
    assert not {k for k in env if k.startswith(("DB_PASSWORD_SOURCE", "DB_PASSWORD_SECRET", "AWS_"))}
    objs = dict(behavioral_app.eso_objects("t-1", "https://openbao:8200", "CA", "role", "sid", "envs/x/rehearsal-app-db"))
    assert objs["externalsecret"]["spec"]["data"][0]["remoteRef"] == {"key": "envs/x/rehearsal-app-db",
                                                                      "property": "password"}
    assert objs["secretstore"]["spec"]["provider"]["vault"]["auth"]["appRole"]["secretRef"]["name"] == "eso-approle"
    # one store name per Trial: ESO's client cache never hands a rerun an old token
    assert objs["externalsecret"]["spec"]["secretStoreRef"]["name"] == objs["secretstore"]["metadata"]["name"] == \
        "openbao-1"


def test_app_proposal_whose_diff_does_not_apply_fails(tmp_path: Path) -> None:
    trial, binding, vdir = bound_trial(tmp_path)
    rec = {"ran": False, "reason": "diff_rejected:diff_context_mismatch", "proposal_fault": True,
           "kind": behavioral_app.KIND,
           "checks": [{"name": "app:diff_applies_to_snapshot", "ok": False, "detail": "diff_context_mismatch"}]}
    accept(trial, binding, vdir, behavioral=rec)
    assert (trial.verdict, trial.reason) == ("FAIL", "verifier:app:diff_applies_to_snapshot")


def test_probe_calls_are_labelled_and_never_counted_as_the_agents(tmp_path: Path) -> None:
    trial = make_trial(tmp_path)
    trial.record("rehearsal_exec", {"op": {"op": "read_file"}}, True, "ok")
    token = CALL_ORIGIN.set("probe")
    try:
        trial.record("rehearsal_exec", {"op": "aws s3 rb s3://chat-attachments --force"}, False, "free_text_command")
    finally:
        CALL_ORIGIN.reset(token)
    assert [e["origin"] for e in trial.tool_events] == ["agent", "probe"]
    assert trial.tool_summary() == {"agent": {"allowed": 1, "denied": 0}, "probe": {"allowed": 0, "denied": 1}}
    trial.phase = "agent_running"
    trial.finish("PASS", None)
    t = trial.timings()
    assert [h["phase"] for h in trial.phase_history] == ["provisioning", "agent_running", "done"]
    assert t["elapsed_s"] is not None and t["finished_at"] is not None
