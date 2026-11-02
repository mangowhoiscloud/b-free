"""Per-Trial Harbor task folder, built from a task template (05 4, 07 3).

Sources, first match wins: ``platform/agent/harbor/<task>`` (owned by ``platform/agent``;
``BRAVO_AGENT_HARBOR_DIR`` overrides) and then this package's ``tasks/templates/<task>``.
The template is copied without ``solution/`` (the oracle answer never enters a Trial) and its
``task.toml`` is re-emitted through Harbor 0.25.0's own ``TaskConfig`` with these rules:

- ``[verifier] environment_mode = "separate"`` and ``no-network`` for agent and verifier are
  required; a template that relaxes them is refused (``task_policy_violation``).
- ``artifacts`` is emptied: nothing is copied from the sandbox to the verifier. The verifier
  gets the frozen ``plan_propose`` artifact from the platform at ``/input/artifact.json``.
- ``[verifier.environment]`` is dropped: the verifier image is the service's pinned one and the
  tests are uploaded from this folder (a bundled image would make Harbor skip the upload).
- ``platform/agent/harbor/<role>`` templates (role = folder name): ``tests/expected.json`` (role,
  plan_id, trial_id, attempt, evidence_ids) is written for the Trial, and ``tests/test.sh`` runs
  their private ``verify.py`` on ``/out/proposal.json`` (seeded by the platform in the verifier
  Pod) and then writes ``binding.json`` (artifact digest) and ``checks.json``.
- Other templates without ``tests/test.sh`` get the platform verifier entry: it runs the private
  ``tests/verify.py`` on the frozen artifact and turns its last JSON line into Harbor's
  ``/logs/verifier/reward.json`` plus ``checks.json`` and ``binding.json``. The legacy
  ``tasks/templates/fix-bucket-policy`` verifier still grades the pre-schema proposal shape, so a
  per-role governance proposal scores 0 there; use ``governance`` (changing private tests needs a
  person, AGENTS.md).
"""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

from harbor.models.task.config import NetworkMode, TaskConfig

PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # platform/rehearsal

VERIFIER_ENTRY = """#!/bin/sh
# Platform verifier entry (bravo_rehearsal.harbor_task). Runs in the verify namespace only.
# The agent never sees this file, the private tests, or /input/artifact.json.
set -eu
python3 -I /tests/verify.py /input/artifact.json > /logs/verifier/verify.out
exec python3 -I /tests/bravo_reward.py /logs/verifier/verify.out /input/artifact.json /logs/verifier
"""

REWARD_SCRIPT = '''"""verify.py's last JSON line -> Harbor's reward.json, plus checks and the artifact binding."""
import hashlib
import json
import math
import sys

out_path, artifact_path, verifier_dir = sys.argv[1:4]
with open(out_path, encoding="utf-8") as f:
    lines = [ln for ln in f.read().splitlines() if ln.strip()]
result = json.loads(lines[-1]) if lines else {}
reward = result.get("reward") if isinstance(result, dict) else None
if not isinstance(reward, (int, float)) or isinstance(reward, bool) or not math.isfinite(reward):
    sys.exit("verifier_no_reward")
with open(artifact_path, "rb") as f:
    digest = "sha256:" + hashlib.sha256(f.read()).hexdigest()
with open(f"{verifier_dir}/checks.json", "w", encoding="utf-8") as f:
    json.dump(result.get("checks", []), f, sort_keys=True)
with open(f"{verifier_dir}/binding.json", "w", encoding="utf-8") as f:
    json.dump({"artifact_digest": digest}, f, sort_keys=True)
with open(f"{verifier_dir}/reward.json", "w", encoding="utf-8") as f:
    json.dump({"reward": reward}, f)
'''


# For platform/agent/harbor/<role>/ templates: their private verify.py grades /out/proposal.json against
# tests/expected.json. The platform seeds the frozen artifact at /out/proposal.json in the verifier Pod.
AGENT_VERIFIER_ENTRY = """#!/bin/sh
# Platform verifier entry for platform/agent/harbor templates (bravo_rehearsal.harbor_task). Verify namespace only.
# /out/proposal.json is the frozen plan_propose artifact from the platform, not a copy from the sandbox.
set -eu
python3 -I /tests/verify.py --proposal /out/proposal.json --expected /tests/expected.json \\
  --reward /logs/verifier/reward.txt --details /logs/verifier/details.json
exec python3 -I /tests/bravo_binding.py /out/proposal.json /logs/verifier
"""

BINDING_SCRIPT = '''"""Write binding.json (sha256 of the graded artifact) and checks.json from verify.py's details."""
import hashlib
import json
import sys

artifact_path, verifier_dir = sys.argv[1:3]
with open(artifact_path, "rb") as f:
    digest = "sha256:" + hashlib.sha256(f.read()).hexdigest()
try:
    with open(f"{verifier_dir}/details.json", encoding="utf-8") as f:
        errors = json.load(f).get("errors") or []
except (OSError, ValueError):
    errors = ["details_unreadable"]
with open(f"{verifier_dir}/checks.json", "w", encoding="utf-8") as f:
    json.dump([{"name": str(e), "ok": False} for e in errors], f, sort_keys=True)
with open(f"{verifier_dir}/binding.json", "w", encoding="utf-8") as f:
    json.dump({"artifact_digest": digest}, f, sort_keys=True)
'''


class TaskInvalid(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def agent_harbor_dir() -> Path:
    env = os.environ.get("BRAVO_AGENT_HARBOR_DIR", "").strip()
    return Path(env) if env else PACKAGE_ROOT.parent / "agent" / "harbor"


def search_dirs(tasks_dir: Path) -> list[Path]:
    return [agent_harbor_dir(), tasks_dir]


def is_agent_template(folder: Path) -> bool:
    return folder.resolve().parent == agent_harbor_dir().resolve()


def find(tasks_dir: Path, task: str) -> Path | None:
    """Task names use ``-`` (API and envelope pattern); agent template folders are role names (``app_migration``)."""
    for base in search_dirs(tasks_dir):
        for name in dict.fromkeys((task, task.replace("-", "_"))):
            folder = base / name
            if (folder / "task.toml").is_file():
                return folder
    return None


def checked_config(raw: dict[str, Any]) -> TaskConfig:
    cfg = TaskConfig.model_validate(raw)
    mode = cfg.verifier.environment_mode
    if mode is None or getattr(mode, "value", mode) != "separate":
        raise TaskInvalid("task_policy_violation:verifier_not_separate")
    for phase in (cfg.agent, cfg.verifier):
        if getattr(phase.network_mode, "value", phase.network_mode) != "no-network":
            raise TaskInvalid("task_policy_violation:network_not_disabled")
    # The namespace is no-network from creation (images are prebuilt, nothing is fetched at setup),
    # so the environment baseline is no-network too. A template that asks for more is refused.
    if "network_mode" in cfg.environment.model_fields_set and \
            getattr(cfg.environment.network_mode, "value", cfg.environment.network_mode) != "no-network":
        raise TaskInvalid("task_policy_violation:network_not_disabled")
    environment = cfg.environment.model_copy(update={"network_mode": NetworkMode.NO_NETWORK, "allowed_hosts": None})
    # The verifier image is the service's digest-pinned one and the tests are uploaded from this folder,
    # so a template's [verifier.environment] image (which would make Harbor skip the test upload) is dropped.
    verifier = cfg.verifier.model_copy(update={"environment": None})
    return cfg.model_copy(update={"artifacts": [], "environment": environment, "verifier": verifier})


def materialize(template: Path, dest: Path, expected: dict[str, Any] | None = None) -> Path:
    """Copy ``template`` to ``dest`` as a Harbor 0.25.0 task folder and return ``dest``.

    ``expected`` (role, plan_id, trial_id, attempt, evidence_ids) is required for
    ``platform/agent/harbor`` templates; their verifier grades the binding against it.
    """
    raw = tomllib.loads((template / "task.toml").read_text(encoding="utf-8"))
    cfg = checked_config(raw)
    if not (template / "tests" / "verify.py").is_file() and not (template / "tests" / "test.sh").is_file():
        raise TaskInvalid("task_tests_missing")
    agent_template = is_agent_template(template)
    if agent_template and expected is None:
        raise TaskInvalid("task_expected_missing")
    shutil.copytree(template, dest, ignore=shutil.ignore_patterns("solution", "__pycache__", "*.pyc"))
    (dest / "task.toml").write_text(cfg.model_dump_toml(), encoding="utf-8")
    tests = dest / "tests"
    if agent_template:
        (tests / "expected.json").write_text(json.dumps(expected, sort_keys=True), encoding="utf-8")
        (tests / "test.sh").write_text(AGENT_VERIFIER_ENTRY, encoding="utf-8")
        (tests / "bravo_binding.py").write_text(BINDING_SCRIPT, encoding="utf-8")
        (tests / "test.sh").chmod(0o755)
    elif not (tests / "test.sh").is_file():
        (tests / "test.sh").write_text(VERIFIER_ENTRY, encoding="utf-8")
        (tests / "test.sh").chmod(0o755)
        (tests / "bravo_reward.py").write_text(REWARD_SCRIPT, encoding="utf-8")
    (dest / "environment").mkdir(exist_ok=True)
    return dest
