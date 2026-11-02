"""The private verifier of fix-bucket-policy, run as its own process the way the verifier Job runs it
(``python3 -I /tests/verify.py /input/artifact.json``). It must not import the artifact and must
print the reward as the last line."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from .test_service import legacy_bucket_proposal

VERIFY = Path(__file__).resolve().parents[1] / "tasks" / "templates" / "fix-bucket-policy" / "tests" / "verify.py"


def run(tmp_path: Path, artifact: Any) -> dict[str, Any]:
    f = tmp_path / "artifact.json"
    f.write_text(json.dumps(artifact) if not isinstance(artifact, str) else artifact)
    r = subprocess.run([sys.executable, "-I", str(VERIFY), str(f)], capture_output=True, text=True, timeout=30, check=False)
    assert r.returncode == 0
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_frozen_good_proposal_scores_one(tmp_path: Path) -> None:
    out = run(tmp_path, legacy_bucket_proposal())
    assert out["reward"] == 1 and all(c["ok"] for c in out["checks"])


@pytest.mark.parametrize(("mutate", "check"), [
    (lambda a: a["proposal"].update(anonymous="allow"), "anonymous_denied"),
    (lambda a: a["proposal"].update(actions=["s3:GetObject"]), "read_and_write_kept"),
    (lambda a: a["proposal"].update(actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]),
     "actions_least_privilege"),
    (lambda a: a["proposal"].update(principal="other-app"), "principal_is_app"),
    (lambda a: a["target"].update(type="seaweedfs_bucket_policy"), "target_is_rgw"),
    (lambda a: a["manifest"]["data"].update({"policy.json": "{}"}), "manifest_matches_proposal"),
])
def test_wrong_proposals_score_zero(tmp_path: Path, mutate: Any, check: str) -> None:
    a = legacy_bucket_proposal()
    mutate(a)
    out = run(tmp_path, a)
    assert out["reward"] == 0 and {c["name"]: c["ok"] for c in out["checks"]}[check] is False


def test_unreadable_artifact_scores_zero(tmp_path: Path) -> None:
    assert run(tmp_path, "not json")["reward"] == 0
