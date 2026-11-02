import json
import subprocess
import sys
import tomllib

import pytest

from bravo_agents.allowlist import ROLES
from conftest import AGENT_DIR

HARBOR = AGENT_DIR / "harbor"
SHARED = (HARBOR / "_shared" / "verify.py").read_bytes()
EXPECTED = {"role": "governance", "plan_id": "p-0012", "trial_id": "t-0012-2", "attempt": 2,
            "evidence_ids": ["gate:obj-01"]}
GOOD = {"role": "governance", "plan_id": "p-0012", "trial_id": "t-0012-2", "attempt": 2,
        "step": "translate", "rationale": "r", "risk": "low", "evidence_refs": ["gate:obj-01"],
        "translations": [{"source": {"type": "aws_s3_bucket_policy", "id": "a"},
                          "target": {"type": "rgw_bucket_policy", "name": "a"}, "control_ids": ["IAM-01"],
                          "document": {"Statement": [{"Effect": "Allow", "Principal": {"AWS": ["app"]},
                                                      "Action": ["s3:GetObject"]}]}}]}


@pytest.mark.parametrize("role", ROLES)
def test_task_boundary(role):
    t = tomllib.loads((HARBOR / role / "task.toml").read_text())
    assert t["verifier"]["environment_mode"] == "separate"
    assert t["verifier"]["network_mode"] == "no-network"
    assert t["artifacts"] == ["/out/proposal.json"]             # 검증기로 가는 것은 제안서 하나
    assert "@sha256:" in t["environment"]["docker_image"]
    assert "@sha256:" in t["verifier"]["environment"]["docker_image"]
    assert t["environment"]["docker_image"] != t["verifier"]["environment"]["docker_image"]
    assert (HARBOR / role / "tests" / "verify.py").read_bytes() == SHARED
    assert not (HARBOR / role / "environment" / "tests").exists()


@pytest.mark.parametrize("role", ROLES)
def test_task_validates_against_harbor_model(role):
    harbor = pytest.importorskip("harbor.models.task.config")
    harbor.TaskConfig.model_validate(tomllib.loads((HARBOR / role / "task.toml").read_text()))


def _verify(tmp_path, proposal, preseed=None):
    (tmp_path / "out").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    prop = tmp_path / "out" / "proposal.json"
    if proposal is not None:
        prop.write_text(json.dumps(proposal))
    exp = tmp_path / "expected.json"
    exp.write_text(json.dumps(EXPECTED))
    reward = tmp_path / "logs" / "reward.txt"
    if preseed is not None:
        reward.write_text(preseed)
    details = tmp_path / "logs" / "details.json"
    subprocess.run([sys.executable, "-I", str(HARBOR / "_shared" / "verify.py"), "--proposal", str(prop),
                    "--expected", str(exp), "--reward", str(reward), "--details", str(details)], check=True)
    return reward.read_text().strip(), json.loads(details.read_text())


def test_good_proposal_scores_one(tmp_path):
    assert _verify(tmp_path, GOOD)[0] == "1"


def test_preseeded_reward_is_ignored(tmp_path):
    """Harbor 이슈 #2551 대응: 미리 심은 reward=1은 읽지 않고, 나쁜 제안서는 0입니다."""
    bad = {**GOOD, "translations": [{**GOOD["translations"][0], "document": {"Statement": [
        {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject"}]}}]}
    reward, details = _verify(tmp_path, bad, preseed="1\n")
    assert reward == "0" and details["preseeded_reward_ignored"] is True
    assert "anonymous_allow" in details["errors"]


def test_missing_artifact_scores_zero_even_if_preseeded(tmp_path):
    reward, details = _verify(tmp_path, None, preseed="1")
    assert reward == "0" and details["errors"] == ["artifact_missing_or_not_regular"]


@pytest.mark.parametrize("mut,err", [
    (lambda p: p.update(trial_id="t-9999-1"), "binding_trial_id"),
    (lambda p: p.update(evidence_refs=["gate:other"]), "unknown_evidence_ref"),
])
def test_binding_and_refs(tmp_path, mut, err):
    p = json.loads(json.dumps(GOOD))
    mut(p)
    reward, details = _verify(tmp_path, p)
    assert reward == "0" and err in details["errors"]


def test_secret_paths_must_come_from_the_destination_profile(tmp_path):
    """B11: an OpenBao policy or ExternalSecret key outside expected.secret_path_prefix (the destination profile, e.g.
    envs/target-onprem/) is refused; the guess envs/onprem/... from the 11/2 proposal no longer passes."""
    def prop(path, key):
        return {**GOOD, "translations": [
            {"source": {"type": "aws_secretsmanager_secret", "id": "s"}, "target": {"type": "openbao_policy", "name": "a"},
             "control_ids": ["SEC-01"], "document": {"path": {path: {"capabilities": ["read"]}}}},
            {"source": {"type": "aws_secretsmanager_secret", "id": "s"}, "target": {"type": "external_secret", "name": "a"},
             "control_ids": ["SEC-01"], "document": {"kind": "ExternalSecret", "metadata": {"name": "a-db"}, "spec": {
                 "data": [{"secretKey": "password", "remoteRef": {"key": key, "property": "app_writer"}}]}}}]}
    global EXPECTED
    saved = EXPECTED
    EXPECTED = {**saved, "secret_path_prefix": "envs/target-onprem/"}
    try:
        good = _verify(tmp_path / "a", prop("secret/data/envs/target-onprem/chat-db", "envs/target-onprem/chat-db"))
        bad = _verify(tmp_path / "b", prop("secret/data/envs/onprem/chat-db", "envs/onprem/chat-db"))
    finally:
        EXPECTED = saved
    assert good[0] == "1"
    assert bad[0] == "0" and bad[1]["errors"] == ["secret_path_outside_profile"]



def test_hcl_openbao_policy_and_t31_names_are_checked_like_t31(tmp_path):
    """A PASS predicts that T31 applies it: HCL text is read (not skipped), and Kubernetes names follow T31's rule."""
    def prop(policy_text, es_name):
        return {**GOOD, "translations": [
            {"source": {"type": "aws_secretsmanager_secret", "id": "s"}, "target": {"type": "openbao_policy", "name": "a"},
             "control_ids": ["SEC-01"], "document": {"name": "a", "policy": policy_text}},
            {"source": {"type": "aws_secretsmanager_secret", "id": "s"}, "target": {"type": "external_secret", "name": "a"},
             "control_ids": ["SEC-01"], "document": {"kind": "ExternalSecret", "metadata": {"name": es_name}, "spec": {
                 "data": [{"secretKey": "p", "remoteRef": {"key": "envs/target-onprem/chat-db"}}]}}}]}
    global EXPECTED
    saved = EXPECTED
    EXPECTED = {**saved, "secret_path_prefix": "envs/target-onprem/", "app_namespace": "chat"}
    try:
        ok = _verify(tmp_path / "a", prop('path "secret/data/envs/target-onprem/chat-db" {\n capabilities = ["read"]\n}\n', "a"))
        guessed = _verify(tmp_path / "b", prop('path "secret/data/envs/onprem/chat-db" { capabilities = ["read"] }', "a"))
        named = _verify(tmp_path / "c", prop('path "secret/data/envs/target-onprem/chat-db" { capabilities = ["read"] }',
                                             "bravo-demo-db"))
    finally:
        EXPECTED = saved
    assert ok[0] == "1"
    assert guessed[1]["errors"] == ["secret_path_outside_profile"]
    assert named[1]["errors"] == ["k8s_name_out_of_scope"]


def test_external_secret_must_use_the_destinations_eso_api_version(tmp_path):
    """ESO 2.x serves only external-secrets.io/v1: a v1beta1 ExternalSecret passed the verifier and then failed T31's
    apply (measured 2026-11-03). The destination profile names the served version; anything else is refused."""
    doc = {"apiVersion": "external-secrets.io/v1beta1", "kind": "ExternalSecret", "metadata": {"name": "a"},
           "spec": {"data": [{"secretKey": "p", "remoteRef": {"key": "envs/target-onprem/chat-db"}}]}}
    prop = {**GOOD, "translations": [{"source": {"type": "aws_secretsmanager_secret", "id": "s"},
                                      "target": {"type": "external_secret", "name": "a"}, "control_ids": ["SEC-01"],
                                      "document": doc}]}
    global EXPECTED
    saved = EXPECTED
    EXPECTED = {**saved, "secret_path_prefix": "envs/target-onprem/", "eso_api_version": "external-secrets.io/v1"}
    try:
        old = _verify(tmp_path / "a", prop)
        doc["apiVersion"] = "external-secrets.io/v1"
        new = _verify(tmp_path / "b", prop)
    finally:
        EXPECTED = saved
    assert old[1]["errors"] == ["external_secret_api_version"] and new[0] == "1"
