"""One rehearsal Trial = one Harbor 0.25.0 Trial (05 4), and the rule for accepting its result.

``trial_config`` wires Harbor to our classes: ``harbor_env.RehearsalNamespaceEnvironment`` for
both the agent environment (rehearsal namespace) and the separate verifier environment (verify
namespace), and ``harbor_agent.RehearsalAgent``. ``accept`` turns Harbor's ``TrialResult`` into
the Trial verdict. A reward counts only when all of these hold (05 3 분리와 전달, V5):

1. Harbor ran the verifier in ``separate`` mode and parsed a numeric ``reward`` from it.
2. Our verify-namespace environment observed the verifier Pod (UID, image ID) from the
   Kubernetes API, and the image ID carries the pinned verifier digest.
3. The verifier's ``binding.json`` names the digest of the frozen ``plan_propose`` artifact.

A reward file the agent pre-seeds in its sandbox (Harbor issue #2551) is never read: the
separate verifier runs in another namespace, Harbor clears the host reward files before it
runs, and without (1)-(3) there is no verdict but ``INFRA_ERROR``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig, VerifierConfig
from harbor.models.trial.result import ExceptionInfo, TrialResult

from .harbor_binding import Binding
from .trials import Trial, now_iso

ENV_IMPORT = "bravo_rehearsal.harbor_env:RehearsalNamespaceEnvironment"
AGENT_IMPORT = "bravo_rehearsal.harbor_agent:RehearsalAgent"
AGENT_GRACE_S = 30.0
# Our exceptions carry the reason as their message. Anything else is reported by type only.
REASON_TYPES = frozenset({"AgentRefused", "AgentWorkerFailed", "IsolationCheckFailed", "TransferError",
                          "EnvironmentError_", "TaskInvalid"})
HARBOR_TYPES = {"AgentTimeoutError": "agent_timeout", "EnvironmentStartTimeoutError": "environment_start_timeout",
                "VerifierTimeoutError": "verifier_timeout", "RewardFileNotFoundError": "verifier_no_reward",
                "RewardFileEmptyError": "verifier_no_reward", "VerifierOutputParseError": "verifier_no_reward"}


def trial_config(trial: Trial, task_dir: Path, trials_dir: Path, *, kubeconfig: Path, kube_context: str | None,
                 sandbox_image: str, verifier_image: str, verifier_deadline_s: int) -> TrialConfig:
    return TrialConfig(
        task=TaskConfig(path=task_dir),
        trial_name=trial.trial_id,
        trials_dir=trials_dir,
        # The agent enforces the Trial deadline itself; Harbor's timeout is a backstop with room to
        # stop the worker and apply the frozen manifest.
        agent=AgentConfig(import_path=AGENT_IMPORT, kwargs={"trial_id": trial.trial_id},
                          override_timeout_sec=max(1.0, trial.deadline - time.time()) + AGENT_GRACE_S),
        environment=EnvironmentConfig(
            import_path=ENV_IMPORT, delete=True,
            kwargs={"kubeconfig": str(kubeconfig), "kube_context": kube_context, "trial_id": trial.trial_id,
                    "plan_id": trial.plan_id, "attempt": trial.attempt, "sandbox_image": sandbox_image,
                    "verifier_image": verifier_image},
        ),
        verifier=VerifierConfig(override_timeout_sec=float(verifier_deadline_s)),
    )


def reason_of(exc: ExceptionInfo) -> str:
    if exc.exception_type in REASON_TYPES:
        return (exc.exception_message.strip().splitlines() or ["unknown"])[0][:200]
    return HARBOR_TYPES.get(exc.exception_type, f"harbor:{exc.exception_type}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def accept(trial: Trial, binding: Binding, *, exception: ExceptionInfo | None, verifier_mode: str | None,
           rewards: dict[str, float | int] | None, verifier_dir: Path, verifier_image: str,
           task_version: str) -> None:
    """Set the Trial verdict once from what Harbor and our verify environment observed."""
    if trial.verdict is not None:
        return
    if exception is not None:
        if exception.exception_type == "NoProposal":
            trial.finish("FAIL", "no_valid_proposal")
        else:
            trial.finish("INFRA_ERROR", reason_of(exception))
        return
    if trial.artifact is None or trial.artifact_digest is None:
        trial.finish("FAIL", "no_valid_proposal")
        return
    if verifier_mode != "separate":
        trial.finish("INFRA_ERROR", "verifier_not_separate")
        return
    reward = (rewards or {}).get("reward")
    if not isinstance(reward, (int, float)) or isinstance(reward, bool):
        trial.finish("INFRA_ERROR", "verifier_no_reward")
        return
    record = binding.verifier
    if not record or not record.get("pod_uid"):
        trial.finish("INFRA_ERROR", "verifier_unbound")
        return
    expected = verifier_image.rsplit("@", 1)[1]
    # The container status gives one repo digest; the node's image record lists every name of that same image
    # (a side-loaded image can show another digest first). Bound when the pinned digest names that image.
    names = [str(record.get("image_id", "")), *(str(n) for n in record.get("image_names") or [])]
    if not any(n.endswith("@" + expected) or n.endswith(expected) for n in names if n):
        trial.finish("INFRA_ERROR", "verifier_image_mismatch")
        return
    bound = _read_json(verifier_dir / "binding.json")
    if not isinstance(bound, dict) or bound.get("artifact_digest") != trial.artifact_digest:
        trial.finish("INFRA_ERROR", "artifact_digest_mismatch")
        return
    checks = _read_json(verifier_dir / "checks.json")
    checks = checks if isinstance(checks, list) else []
    trial.verifier = {
        "run_id": f"{record['namespace']}/{record['pod']}",
        "pod_uid": record["pod_uid"],
        "image_digest": expected,
        "image_id": record.get("image_id"),
        "task_version": task_version,
        "artifact_digest": trial.artifact_digest,
        "reward": float(reward),
        "checks": checks,
        "refused_uploads": record.get("refused_uploads", []),
        "verifier_kind": trial.verifier_kind or "unknown",  # e.g. static_rules: no redeploy, no live probe
        "collected_at": now_iso(),
    }
    if reward >= 1:
        trial.finish("PASS", None)
    else:
        failed = [str(c.get("name")) for c in checks if isinstance(c, dict) and not c.get("ok")]
        trial.finish("FAIL", "verifier:" + ",".join(failed))


def accept_result(trial: Trial, binding: Binding, result: TrialResult, *, verifier_dir: Path,
                  verifier_image: str, task_version: str) -> None:
    mode = result.verifier_environment_mode
    accept(trial, binding, exception=result.exception_info,
           verifier_mode=getattr(mode, "value", mode) if mode is not None else None,
           rewards=result.verifier_result.rewards if result.verifier_result else None,
           verifier_dir=verifier_dir, verifier_image=verifier_image, task_version=task_version)
