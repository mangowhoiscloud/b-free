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
    (tmp_path / "out").mkdir()
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
