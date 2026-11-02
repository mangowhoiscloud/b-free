"""Harbor 0.25.0 runtime: environment adapter, agent, task folder, verdict acceptance.

Pure tests run Harbor's real classes and the exact in-Pod transfer scripts with the local
``python3``. Cluster tests run every ``BaseEnvironment`` method against a real K3s cluster
when ``BRAVO_REHEARSAL_KUBECONFIG`` (and digest-pinned ``BRAVO_REHEARSAL_SANDBOX_IMAGE`` /
``BRAVO_REHEARSAL_VERIFIER_IMAGE``) are set; otherwise they are skipped with
``cluster_unconfigured``.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest
from harbor.environments.base import BaseEnvironment
from harbor.models.task.config import EnvironmentConfig as TaskEnvConfig
from harbor.models.task.config import NetworkMode, NetworkPolicy
from harbor.models.task.task import Task
from harbor.models.trial.paths import TrialPaths
from harbor.models.trial.result import ExceptionInfo

from bravo_rehearsal import allowlist, harbor_binding, harbor_run, harbor_task, proposal
from bravo_rehearsal.harbor_agent import AgentRefused, RehearsalAgent
from bravo_rehearsal.harbor_env import (
    RECV_SCRIPT,
    SEND_SCRIPT,
    DefinitionInvalid,
    RehearsalNamespaceEnvironment,
    TransferError,
    decode_frames,
    encode_frames,
    pack_dir,
    pack_file,
    shell_command,
    unpack_dir,
    unpack_file,
)
from bravo_rehearsal.trials import Trial

from .test_service import ENV_PLAN, bucket_policy, good_proposal, legacy_bucket_proposal

TEMPLATES = Path(__file__).resolve().parents[1] / "tasks" / "templates"
PINNED = "registry.example/bravo/sandbox@sha256:" + "a" * 64
VERIFIER = "registry.example/bravo/verifier@sha256:" + "b" * 64
PLAN = "11111111-2222-3333-4444-555555555555"


AGENT_HARBOR = Path(__file__).resolve().parents[2] / "agent" / "harbor"
TRIAL = "t-harbortest01"


def good_artifact(trial_id: str = TRIAL) -> dict[str, Any]:
    p, problems = proposal.check(good_proposal(trial_id), "governance",
                                 proposal.Binding(plan_id=ENV_PLAN, trial_id=trial_id, attempt=1))
    assert p is not None and not problems
    return p.model_dump(mode="json")


def expected(trial_id: str = TRIAL) -> dict[str, Any]:
    return {"role": "governance", "plan_id": ENV_PLAN, "trial_id": trial_id, "attempt": 1, "evidence_ids": []}


def make_trial(tmp_path: Path, trial_id: str = "t-harbortest01", **kw: Any) -> Trial:
    base = dict(trial_id=trial_id, tenant_id="t1", plan_id=PLAN, rehearsal_id="r1", attempt=1, max_attempts=3,
                task="fix-bucket-policy", role="governance", idem_key=f"k-{trial_id}", request_digest="d",
                deadline=time.time() + 60, allowlist=allowlist.load("governance", tmp_path), envelope={})
    return Trial(**{**base, **kw})


def kubeconfig(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "kubeconfig"
    path.write_text("apiVersion: v1\nkind: Config\nclusters:\n- cluster: {server: 'https://127.0.0.1:1'}\n"
                    "  name: c\ncontexts:\n- context: {cluster: c, user: u}\n  name: c\ncurrent-context: c\n"
                    "users:\n- name: u\n  user: {token: x}\n")
    return path


def make_env(tmp_path: Path, session: str = "t__env", **kw: Any) -> RehearsalNamespaceEnvironment:
    env_dir = tmp_path / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    tp = TrialPaths(tmp_path / "trial")
    tp.mkdir()
    args: dict[str, Any] = dict(kubeconfig=str(kubeconfig(tmp_path)), trial_id="t-harbortest01", plan_id=PLAN,
                                attempt=1, sandbox_image=PINNED, verifier_image=VERIFIER)
    args.update(kw)
    return RehearsalNamespaceEnvironment(
        environment_dir=env_dir, environment_name="fix-bucket-policy", session_id=session, trial_paths=tp,
        task_env_config=TaskEnvConfig(network_mode="no-network"),
        network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK), **args)


# BaseEnvironment contract and definition validation ------------------------------------------------


def test_environment_implements_every_abstract_method() -> None:
    abstract = {n for n, v in vars(BaseEnvironment).items() if getattr(v, "__isabstractmethod__", False)}
    assert abstract == {"type", "_validate_definition", "start", "stop", "upload_file", "upload_dir",
                        "download_file", "download_dir", "exec"}
    assert not RehearsalNamespaceEnvironment.__abstractmethods__


def test_definition_valid_maps_roles_to_namespaces(tmp_path: Path) -> None:
    agent = make_env(tmp_path)
    expected = ("rehearsal-11111111-1", "rehearsal-sandbox-t-harbortest", PINNED)
    assert (agent.namespace, agent.pod_name, agent.image) == expected
    assert agent.capabilities.disable_internet and not agent.capabilities.network_allowlist
    verifier = make_env(tmp_path / "v", session="t__verifier__grade")
    expected = ("verify-11111111-1", "verify-runner-t-harbortest", VERIFIER)
    assert (verifier.namespace, verifier.pod_name, verifier.image) == expected
    with pytest.raises(Exception, match="sandbox_unavailable"):
        _ = agent.sandbox  # not started: the tool layer has nothing to reach


@pytest.mark.parametrize(("kw", "reason"), [
    ({"kubeconfig": None}, "cluster_unconfigured"),
    ({"kubeconfig": "/nonexistent/kubeconfig"}, "cluster_unconfigured"),
    ({"sandbox_image": "python:3.12"}, "sandbox_image_unpinned"),
    ({"trial_id": None}, "trial_binding_kwargs_missing"),
    ({"attempt": 0}, "trial_binding_kwargs_missing"),
])
def test_definition_invalid(tmp_path: Path, kw: dict[str, Any], reason: str) -> None:
    with pytest.raises(DefinitionInvalid, match=reason):
        make_env(tmp_path, **kw)


def test_definition_invalid_verifier_image_and_compose(tmp_path: Path) -> None:
    with pytest.raises(DefinitionInvalid, match="verifier_unconfigured"):
        make_env(tmp_path, session="t__verifier__grade", verifier_image="verifier:latest")
    (tmp_path / "c" / "environment").mkdir(parents=True)
    (tmp_path / "c" / "environment" / "docker-compose.yaml").write_text("services: {}\n")
    with pytest.raises(DefinitionInvalid, match="compose_unsupported"):
        make_env(tmp_path / "c")
    with pytest.raises(DefinitionInvalid, match="host_mounts_unsupported"):
        make_env(tmp_path / "m", mounts=[{"type": "bind", "source": str(tmp_path), "target": "/host"}])


def test_public_network_is_rejected_by_harbor_capability_check(tmp_path: Path) -> None:
    env_dir = tmp_path / "environment"
    env_dir.mkdir()
    with pytest.raises(ValueError, match="allowlist"):
        RehearsalNamespaceEnvironment(
            environment_dir=env_dir, environment_name="x", session_id="t__env", trial_paths=TrialPaths(tmp_path),
            task_env_config=TaskEnvConfig(), network_policy=NetworkPolicy(network_mode=NetworkMode.ALLOWLIST,
                                                                           allowed_hosts=["example.com"]),
            kubeconfig=str(kubeconfig(tmp_path)), trial_id="t-x", plan_id=PLAN, attempt=1, sandbox_image=PINNED,
            verifier_image=VERIFIER)


def test_shell_command_applies_cwd_env_and_quotes(tmp_path: Path) -> None:
    argv = shell_command("echo \"$A\" && pwd", str(tmp_path), {"A": "x y; rm -rf /"})
    r = subprocess.run(argv, capture_output=True, text=True, check=True)
    assert r.stdout.splitlines() == ["x y; rm -rf /", str(tmp_path.resolve())] or \
        r.stdout.splitlines() == ["x y; rm -rf /", str(tmp_path)]
    with pytest.raises(ValueError):
        shell_command("true", None, {"A B": "1"})


# Tar over exec: the exact in-Pod scripts, run with the local python3 -------------------------------


def _recv(target: Path, frames: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", "-c", RECV_SCRIPT, str(target)], input="".join(frames),
                          capture_output=True, text=True, check=False)


def _send(source: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", "-c", SEND_SCRIPT, str(source), mode], capture_output=True,
                          text=True, check=False)


def test_upload_dir_roundtrip_keeps_tree_modes_and_empty_dirs(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "sub" / "empty").mkdir(parents=True)
    (src / "a.txt").write_text("alpha")
    (src / "sub" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (src / "sub" / "run.sh").chmod(0o755)
    (src / "bin.dat").write_bytes(bytes(range(256)) * 600)  # > one stdin frame
    archive = pack_dir(src)
    frames = encode_frames(archive)
    assert frames[-1] == "EOF\n" and len(frames) > 2
    r = _recv(tmp_path / "dst", frames)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(len(archive))  # the host checks this count
    dst = tmp_path / "dst"
    assert (dst / "a.txt").read_text() == "alpha"
    assert (dst / "bin.dat").read_bytes() == (src / "bin.dat").read_bytes()
    assert os.access(dst / "sub" / "run.sh", os.X_OK) and (dst / "sub" / "empty").is_dir()


def test_download_dir_and_file_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "logs"
    (src / "nested").mkdir(parents=True)
    (src / "reward.json").write_text('{"reward": 1}')
    (src / "nested" / "x.log").write_text("x" * 200_000)
    r = _send(src, "dir")
    assert r.returncode == 0, r.stderr
    assert unpack_dir(decode_frames(r.stdout), tmp_path / "out") == []
    assert (tmp_path / "out" / "nested" / "x.log").read_text() == "x" * 200_000
    r = _send(src / "reward.json", "file")
    unpack_file(decode_frames(r.stdout), tmp_path / "one" / "renamed.json")
    assert json.loads((tmp_path / "one" / "renamed.json").read_text()) == {"reward": 1}


def test_upload_file_uses_target_name(tmp_path: Path) -> None:
    f = tmp_path / "artifact.json"
    f.write_text("{}")
    r = _recv(tmp_path / "input", encode_frames(pack_file(f, "renamed.json")))
    assert r.returncode == 0 and (tmp_path / "input" / "renamed.json").read_text() == "{}"


def test_transfer_failures_are_loud(tmp_path: Path) -> None:
    archive = pack_dir(Path(__file__).parent)
    r = _recv(tmp_path / "dst", encode_frames(archive)[:-1])  # stream cut before EOF
    assert r.returncode != 0 and "transfer_truncated" in r.stderr
    with pytest.raises(TransferError, match="transfer_truncated"):
        decode_frames("QUJD\n")
    with pytest.raises(TransferError, match="transfer_corrupt"):
        decode_frames("not*base64\nEOF\n")
    r = _send(tmp_path / "missing", "dir")
    assert r.returncode != 0 and "transfer_source_missing" in r.stderr


def test_recv_refuses_path_traversal(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"owned"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    r = _recv(tmp_path / "dst", encode_frames(buf.getvalue()))
    assert r.returncode != 0 and not (tmp_path / "escape.txt").exists()


# Task folder -----------------------------------------------------------------------------------


def test_materialize_bucket_policy_is_a_valid_harbor_task(tmp_path: Path) -> None:
    dest = harbor_task.materialize(TEMPLATES / "fix-bucket-policy", tmp_path / "task")
    task = Task(dest)  # Harbor's own loader
    cfg = task.config
    assert getattr(cfg.verifier.environment_mode, "value", None) == "separate"
    assert cfg.artifacts == []  # nothing is copied from the sandbox to the verifier
    assert {cfg.agent.network_mode, cfg.verifier.network_mode, cfg.environment.network_mode} == {NetworkMode.NO_NETWORK}
    assert os.access(dest / "tests" / "test.sh", os.X_OK) and (dest / "tests" / "verify.py").is_file()


def test_materialize_drops_oracle_solution(tmp_path: Path) -> None:
    dest = harbor_task.materialize(TEMPLATES / "fix-secret-injection", tmp_path / "task")
    assert not (dest / "solution").exists() and (dest / "tests" / "test.sh").is_file()


@pytest.mark.parametrize(("toml", "reason"), [
    ('[agent]\nnetwork_mode = "no-network"\n[verifier]\nnetwork_mode = "no-network"\n', "verifier_not_separate"),
    ('[agent]\nnetwork_mode = "public"\n[verifier]\nenvironment_mode = "separate"\nnetwork_mode = "no-network"\n',
     "network_not_disabled"),
    ('[environment]\nnetwork_mode = "public"\n[agent]\nnetwork_mode = "no-network"\n'
     '[verifier]\nenvironment_mode = "separate"\nnetwork_mode = "no-network"\n', "network_not_disabled"),
])
def test_materialize_refuses_relaxed_templates(tmp_path: Path, toml: str, reason: str) -> None:
    src = tmp_path / "tpl"
    (src / "tests").mkdir(parents=True)
    (src / "tests" / "verify.py").write_text("")
    (src / "instruction.md").write_text("x")
    (src / "task.toml").write_text(toml)
    with pytest.raises(harbor_task.TaskInvalid, match=reason):
        harbor_task.materialize(src, tmp_path / "out")


def _grade_agent_template(tmp_path: Path, artifact: dict[str, Any]) -> tuple[str, dict[str, Any], Path]:
    """The verify-namespace steps for a platform/agent template, run locally with its private verify.py."""
    dest = harbor_task.materialize(AGENT_HARBOR / "governance", tmp_path / "task", expected())
    out, vdir = tmp_path / "out", tmp_path / "verifier"
    out.mkdir()
    vdir.mkdir()
    (out / "proposal.json").write_bytes(harbor_binding.canonical(artifact))  # seeded by the platform
    (vdir / "reward.txt").write_text("1")  # pre-seeded (#2551): verify.py must ignore and replace it
    subprocess.run([sys.executable, "-I", str(dest / "tests" / "verify.py"), "--proposal", str(out / "proposal.json"),
                    "--expected", str(dest / "tests" / "expected.json"), "--reward", str(vdir / "reward.txt"),
                    "--details", str(vdir / "details.json")], check=True)
    subprocess.run([sys.executable, "-I", str(dest / "tests" / "bravo_binding.py"), str(out / "proposal.json"),
                    str(vdir)], check=True)
    return (vdir / "reward.txt").read_text().strip(), json.loads((vdir / "binding.json").read_text()), vdir


def test_agent_template_verifier_grades_frozen_artifact_and_binds_digest(tmp_path: Path) -> None:
    dest = harbor_task.materialize(AGENT_HARBOR / "governance", tmp_path / "t", expected())
    cfg = Task(dest).config
    assert cfg.verifier.environment is None and cfg.artifacts == []  # tests are uploaded; nothing from the sandbox
    assert json.loads((dest / "tests" / "expected.json").read_text())["trial_id"] == TRIAL
    assert "/out/proposal.json" in (dest / "tests" / "test.sh").read_text()
    assert harbor_task.task_meta(dest)["verifier_kind"] == "static_rules"
    assert not (dest / "environment" / "source").exists()  # B9: no template sample stands in for the customer repo
    art = good_artifact()
    reward, binding, _ = _grade_agent_template(tmp_path / "good", art)
    assert reward == "1" and binding["artifact_digest"] == proposal.digest(art)
    bad = good_artifact()
    bad["translations"][0]["document"] = bucket_policy(principal="*")
    reward, _, vdir = _grade_agent_template(tmp_path / "bad", bad)
    assert reward == "0" and json.loads((vdir / "checks.json").read_text())
    with pytest.raises(harbor_task.TaskInvalid, match="task_expected_missing"):
        harbor_task.materialize(AGENT_HARBOR / "governance", tmp_path / "x")


def test_every_agent_template_materializes_and_task_names_map_to_roles(tmp_path: Path) -> None:
    for role in ("governance", "app_migration", "ci", "db", "discovery", "report"):
        exp = {**expected(), "role": role}
        cfg = Task(harbor_task.materialize(AGENT_HARBOR / role, tmp_path / role, exp)).config
        assert getattr(cfg.verifier.environment_mode, "value", None) == "separate" and cfg.artifacts == []
    assert harbor_task.find(TEMPLATES, "app-migration") == AGENT_HARBOR / "app_migration"
    assert harbor_task.find(TEMPLATES, "fix-bucket-policy") == TEMPLATES / "fix-bucket-policy"


def test_legacy_template_verifier_entry_writes_reward_and_binding(tmp_path: Path) -> None:
    """tasks/templates/fix-bucket-policy keeps its pre-schema private tests (AGENTS.md: changing them needs a human)."""
    dest = harbor_task.materialize(TEMPLATES / "fix-bucket-policy", tmp_path / "task")
    art = legacy_bucket_proposal()
    (tmp_path / "artifact.json").write_bytes(harbor_binding.canonical(art))
    vdir = tmp_path / "verifier"
    vdir.mkdir()
    out = subprocess.run([sys.executable, "-I", str(dest / "tests" / "verify.py"), str(tmp_path / "artifact.json")],
                         capture_output=True, text=True, check=True).stdout
    (vdir / "verify.out").write_text(out)
    subprocess.run([sys.executable, "-I", str(dest / "tests" / "bravo_reward.py"), str(vdir / "verify.out"),
                    str(tmp_path / "artifact.json"), str(vdir)], check=True)
    assert json.loads((vdir / "reward.json").read_text()) == {"reward": 1}
    assert json.loads((vdir / "binding.json").read_text())["artifact_digest"] == proposal.digest(art)


# Verdict acceptance (V5, Harbor issue #2551) -----------------------------------------------------


def bound_trial(tmp_path: Path) -> tuple[Trial, harbor_binding.Binding, Path]:
    trial = make_trial(tmp_path)
    trial.artifact = good_artifact()
    trial.artifact_digest = proposal.digest(trial.artifact)
    binding = harbor_binding.Binding(trial=trial, worker_env=lambda _d: {})
    binding.verifier = {"namespace": "verify-11111111-1", "pod": "verify-runner-x", "pod_uid": "uid-1",
                        "image_id": "docker.io/bravo/verifier@sha256:" + "b" * 64, "refused_uploads": []}
    vdir = tmp_path / "verifier"
    vdir.mkdir(parents=True)
    (vdir / "binding.json").write_text(json.dumps({"artifact_digest": trial.artifact_digest}))
    (vdir / "checks.json").write_text(json.dumps([{"name": "anonymous_denied", "ok": True}]))
    return trial, binding, vdir


def accept(trial: Trial, binding: harbor_binding.Binding, vdir: Path, **kw: Any) -> None:
    args: dict[str, Any] = dict(exception=None, verifier_mode="separate", rewards={"reward": 1}, verifier_dir=vdir,
                                verifier_image=VERIFIER, task_version="v1")
    harbor_run.accept(trial, binding, **{**args, **kw})


def test_reward_preseeded_by_agent_is_ignored(tmp_path: Path) -> None:
    """Harbor #2551: a reward file in the agent's reach never becomes a verdict."""
    trial, binding, vdir = bound_trial(tmp_path)
    (vdir / "reward.txt").write_text("1")  # pre-seeded, e.g. synced from the sandbox's /logs/verifier
    (vdir / "reward.json").write_text('{"reward": 1}')
    accept(trial, binding, vdir, rewards=None)  # the separate verifier produced no reward
    assert (trial.verdict, trial.reason) == ("INFRA_ERROR", "verifier_no_reward")

    trial, binding, vdir = bound_trial(tmp_path / "b")
    binding.verifier = None  # a reward without our verify-namespace observation
    accept(trial, binding, vdir)
    assert (trial.verdict, trial.reason) == ("INFRA_ERROR", "verifier_unbound")

    trial, binding, vdir = bound_trial(tmp_path / "c")
    accept(trial, binding, vdir, verifier_mode="shared")  # verifier in the agent's environment
    assert (trial.verdict, trial.reason) == ("INFRA_ERROR", "verifier_not_separate")


def test_reward_bound_to_artifact_digest_and_verifier_image(tmp_path: Path) -> None:
    trial, binding, vdir = bound_trial(tmp_path)
    (vdir / "binding.json").write_text(json.dumps({"artifact_digest": "sha256:" + "0" * 64}))
    accept(trial, binding, vdir)
    assert (trial.verdict, trial.reason) == ("INFRA_ERROR", "artifact_digest_mismatch")

    trial, binding, vdir = bound_trial(tmp_path / "b")
    binding.verifier = {**(binding.verifier or {}), "image_id": "docker.io/other@sha256:" + "c" * 64}
    accept(trial, binding, vdir)
    assert (trial.verdict, trial.reason) == ("INFRA_ERROR", "verifier_image_mismatch")

    # Side-loaded image: the status shows another repo digest; the node's record for that image names the pin.
    trial, binding, vdir = bound_trial(tmp_path / "c")
    side = "docker.io/library/import-2026-11-02@sha256:" + "d" * 64
    binding.verifier = {**(binding.verifier or {}), "image_id": side,
                        "image_names": [side, "docker.io/library/python@sha256:" + "b" * 64]}
    accept(trial, binding, vdir)
    assert trial.verdict == "PASS"
    trial, binding, vdir = bound_trial(tmp_path / "d")
    binding.verifier = {**(binding.verifier or {}), "image_id": side, "image_names": [side]}
    accept(trial, binding, vdir)
    assert trial.reason == "verifier_image_mismatch"


def test_accept_pass_fail_and_failures(tmp_path: Path) -> None:
    trial, binding, vdir = bound_trial(tmp_path)
    trial.verifier_kind = harbor_task.STATIC_RULES
    accept(trial, binding, vdir)
    assert trial.verdict == "PASS" and trial.verifier and trial.verifier["pod_uid"] == "uid-1"
    assert trial.verifier["verifier_kind"] == "static_rules"  # B7: a PASS says what ran
    accept(trial, binding, vdir, rewards={"reward": 0})  # verdict is set once (CAS)
    assert trial.verdict == "PASS"

    trial, binding, vdir = bound_trial(tmp_path / "b")
    (vdir / "checks.json").write_text(json.dumps([{"name": "anonymous_denied", "ok": False}]))
    accept(trial, binding, vdir, rewards={"reward": 0})
    assert (trial.verdict, trial.reason) == ("FAIL", "verifier:anonymous_denied")

    def exc(kind: str, msg: str) -> ExceptionInfo:
        return ExceptionInfo(exception_type=kind, exception_message=msg, exception_traceback="",
                             occurred_at="2026-11-02T00:00:00Z")

    for kind, msg, verdict, reason in [
        ("NoProposal", "no_valid_proposal", "FAIL", "no_valid_proposal"),
        ("AgentRefused", "llm_unconfigured", "INFRA_ERROR", "llm_unconfigured"),
        ("IsolationCheckFailed", "isolation_check_failed:egress_open:1.1.1.1:443", "INFRA_ERROR",
         "isolation_check_failed:egress_open:1.1.1.1:443"),
        ("AgentTimeoutError", "Agent execution timed out after 5 seconds", "INFRA_ERROR", "agent_timeout"),
        ("KeyError", "secret detail", "INFRA_ERROR", "harbor:KeyError"),
    ]:
        trial, binding, vdir = bound_trial(tmp_path / kind)
        accept(trial, binding, vdir, exception=exc(kind, msg))
        assert (trial.verdict, trial.reason) == (verdict, reason)


# Agent: fails closed, never fakes output ---------------------------------------------------------


async def test_agent_fails_closed_without_model_or_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("BRAVO_LLM_PROVIDER", "BRAVO_LLM_GATEWAY_URL", "BRAVO_MODEL_OPUS", "BRAVO_MODEL_HAIKU"):
        monkeypatch.delenv(k, raising=False)
    env = make_env(tmp_path)
    trial = make_trial(tmp_path, trial_id="t-agentclosed1")
    agent = RehearsalAgent(logs_dir=tmp_path / "agent", trial_id=trial.trial_id)
    with pytest.raises(AgentRefused, match="trial_binding_missing"):
        await agent.run("x", env, None)  # type: ignore[arg-type]
    harbor_binding.register(harbor_binding.Binding(trial=trial, worker_env=lambda _d: {}))
    try:
        with pytest.raises(AgentRefused, match="llm_unconfigured"):
            await agent.run("x", env, None)  # type: ignore[arg-type]
        for k, v in {"BRAVO_LLM_PROVIDER": "anthropic_gateway", "BRAVO_LLM_GATEWAY_URL": "https://gw.invalid",
                     "BRAVO_MODEL_OPUS": "configured-for-ordering-only"}.items():
            monkeypatch.setenv(k, v)  # no call is made below: the allowlist check stops first
        with pytest.raises(AgentRefused, match="role_allowlist_missing"):
            await agent.run("x", env, None)  # type: ignore[arg-type]
        verifier_env = make_env(tmp_path / "v", session="t__verifier__grade")
        with pytest.raises(AgentRefused, match="sandbox_not_harbor_namespace"):
            await agent.setup(verifier_env)
    finally:
        harbor_binding.release(trial.trial_id)
    assert trial.phase == "provisioning"  # no worker was started


# A real Harbor Trial with our classes ----------------------------------------------------------


async def test_harbor_trial_runs_our_environment_and_fails_closed_on_unreachable_cluster(tmp_path: Path) -> None:
    from harbor.trial.trial import Trial as HarborTrial

    task_dir = harbor_task.materialize(TEMPLATES / "fix-bucket-policy", tmp_path / "task")
    trial = make_trial(tmp_path, trial_id="t-harborwire01")
    binding = harbor_binding.Binding(trial=trial, worker_env=lambda _d: {})
    harbor_binding.register(binding)
    try:
        cfg = harbor_run.trial_config(trial, task_dir, tmp_path / "trials", kubeconfig=kubeconfig(tmp_path),
                                      kube_context=None, sandbox_image=PINNED, verifier_image=VERIFIER,
                                      verifier_deadline_s=30)
        ht = await HarborTrial.create(cfg)
        assert isinstance(ht.agent_environment, RehearsalNamespaceEnvironment)
        # One plan runs its tasks side by side, so the role is part of the name (rehearsal-<plan>-<role>-<n>).
        assert ht.agent_environment.namespace == "rehearsal-11111111-gov-1"
        result = await asyncio.wait_for(ht.run(), 120)
        harbor_run.accept_result(trial, binding, result, verifier_dir=ht.paths.verifier_dir,
                                 verifier_image=VERIFIER, task_version="v1")
    finally:
        harbor_binding.release(trial.trial_id)
    assert result.verifier_result is None  # the verifier never ran
    assert (trial.verdict, trial.reason) == ("INFRA_ERROR", "cluster_unreachable")


# Real cluster ------------------------------------------------------------------------------------


def cluster_reason() -> str | None:
    path = os.environ.get("BRAVO_REHEARSAL_KUBECONFIG", "").strip()
    if not path:
        return "cluster_unconfigured: BRAVO_REHEARSAL_KUBECONFIG not set"
    if not Path(path).expanduser().is_file():
        return f"cluster_unconfigured: {path} missing"
    for var in ("BRAVO_REHEARSAL_SANDBOX_IMAGE", "BRAVO_REHEARSAL_VERIFIER_IMAGE"):
        if "@sha256:" not in os.environ.get(var, ""):
            return f"cluster_unconfigured: {var} must be digest-pinned"
    return None


RUN = int(time.time())


def cluster_env(tmp_path: Path, trial_id: str, session: str = "t__env") -> RehearsalNamespaceEnvironment:
    env_dir = tmp_path / "environment"
    (env_dir / "app").mkdir(parents=True, exist_ok=True)
    (env_dir / "app" / "policy.json").write_text("{}")
    tp = TrialPaths(tmp_path / "trial")
    tp.mkdir()
    return RehearsalNamespaceEnvironment(
        environment_dir=env_dir, environment_name="fix-bucket-policy", session_id=session, trial_paths=tp,
        task_env_config=TaskEnvConfig(network_mode="no-network"),
        network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK),
        kubeconfig=os.environ["BRAVO_REHEARSAL_KUBECONFIG"],
        kube_context=os.environ.get("BRAVO_REHEARSAL_KUBE_CONTEXT"),
        trial_id=trial_id, plan_id=f"pytest{RUN % 100:02d}", attempt=int(trial_id.rsplit("-", 1)[1]) % 100000 + 1,
        sandbox_image=os.environ["BRAVO_REHEARSAL_SANDBOX_IMAGE"],
        verifier_image=os.environ["BRAVO_REHEARSAL_VERIFIER_IMAGE"])


@pytest.mark.cluster
@pytest.mark.skipif(cluster_reason() is not None, reason=str(cluster_reason()))
async def test_every_environment_method_on_real_cluster(tmp_path: Path) -> None:  # pragma: no cover - needs K3s
    trial_id = f"t-{time.time_ns()}"
    env = cluster_env(tmp_path, trial_id)
    trial = make_trial(tmp_path, trial_id=trial_id)
    harbor_binding.register(harbor_binding.Binding(trial=trial, worker_env=lambda _d: {}))
    try:
        await env.start(force_build=False)  # namespace, baseline, sandbox, isolation check, /app seed
        iso = trial.isolation[env.namespace]
        egress = [c for c in iso["checks"] if c["check"] == "egress"]
        assert len(egress) == 2 and all(c["blocked"] and c["error"] for c in egress), iso  # recorded, not assumed
        assert any(c["check"] == "privileged_pod_admission" and not c["admitted"] for c in iso["checks"])
        out = Path(os.environ.get("BRAVO_REHEARSAL_EVIDENCE_OUT", str(tmp_path))) / f"isolation-{trial_id}.json"
        out.write_text(json.dumps({"namespace": env.namespace, **iso}, indent=2))
        r = await env.exec("cat policy.json && echo \"$X\"", cwd="/app", env={"X": "y"})
        assert (r.return_code, r.stdout) == (0, "{}y\n")  # policy.json has no trailing newline
        assert (await env.exec("sleep 5", timeout_sec=1)).return_code == 124
        assert (await env.exec("id -u")).stdout.strip() == "65532"  # user=root is not honoured
        src = tmp_path / "up"
        (src / "d").mkdir(parents=True)
        (src / "d" / "f.txt").write_text("z" * 100_000)
        await env.upload_dir(src, "/tmp/up")
        await env.upload_file(src / "d" / "f.txt", "/logs/agent/one.txt")
        await env.download_dir("/tmp/up", tmp_path / "down")
        await env.download_file("/logs/agent/one.txt", tmp_path / "one.txt")
        assert (tmp_path / "down" / "d" / "f.txt").read_text() == "z" * 100_000
        assert (tmp_path / "one.txt").read_text() == "z" * 100_000
        assert await env.is_dir("/tmp/up") and await env.is_file("/logs/agent/one.txt")
        assert env.sandbox.pod_uid
    finally:
        await env.stop(delete=True)
        harbor_binding.release(trial_id)
    from kubernetes import client as k8s
    from kubernetes import config as k8s_config
    from kubernetes.client.rest import ApiException

    core = k8s.CoreV1Api(k8s_config.new_client_from_config(config_file=os.environ["BRAVO_REHEARSAL_KUBECONFIG"],
                                                           context=os.environ.get("BRAVO_REHEARSAL_KUBE_CONTEXT")))
    with pytest.raises(ApiException):
        core.read_namespace(env.namespace)  # V6: nothing left


@pytest.mark.cluster
@pytest.mark.skipif(cluster_reason() is not None, reason=str(cluster_reason()))
async def test_separate_verifier_ignores_preseeded_reward_on_real_cluster(tmp_path: Path) -> None:  # pragma: no cover
    """#2551 end to end: a reward planted in the sandbox's /logs/verifier never reaches the verdict."""
    from harbor.models.task.task import Task as HarborTask
    from harbor.verifier.verifier import Verifier

    trial_id = f"t-{time.time_ns()}"
    trial = make_trial(tmp_path, trial_id=trial_id)
    bad = good_artifact(trial_id)
    bad["translations"][0]["document"] = bucket_policy(principal="*")  # the private tests must fail this
    trial.artifact, trial.artifact_digest = bad, proposal.digest(bad)
    binding = harbor_binding.Binding(trial=trial, worker_env=lambda _d: {})
    harbor_binding.register(binding)
    agent_env = cluster_env(tmp_path / "a", trial_id)
    verifier_env = cluster_env(tmp_path / "v", trial_id, session=f"{trial_id}__verifier__grade")
    task = HarborTask(harbor_task.materialize(AGENT_HARBOR / "governance", tmp_path / "task", expected(trial_id)))
    try:
        await agent_env.start(force_build=False)
        assert (await agent_env.exec("mkdir -p /logs/verifier && echo 1 > /logs/verifier/reward.txt")).return_code == 0
        await verifier_env.start(force_build=False)
        await verifier_env.empty_dirs(["/logs/verifier"], chmod=True)  # as Harbor's Trial does
        await verifier_env.upload_dir(tmp_path / "a" / "environment", "/logs/artifacts")  # refused: not /tests
        result = await Verifier(task=task, trial_paths=verifier_env.trial_paths, environment=verifier_env).verify()
        assert result.rewards == {"reward": 0}
        # V1: the rehearsal exec identity has no rights in the verify namespace
        from kubernetes import client as k8s

        core = k8s.CoreV1Api(agent_env._exec_client(agent_env._target))  # type: ignore[arg-type]
        with pytest.raises(Exception):
            core.read_namespaced_pod(verifier_env.pod_name, verifier_env.namespace)
    finally:
        await verifier_env.stop(delete=True)
        await agent_env.stop(delete=True)
        harbor_binding.release(trial_id)
    assert binding.verifier and binding.verifier["refused_uploads"] == ["/logs/artifacts"]
    harbor_run.accept(trial, binding, exception=None, verifier_mode="separate", rewards=result.rewards,
                      verifier_dir=verifier_env.trial_paths.verifier_dir,
                      verifier_image=os.environ["BRAVO_REHEARSAL_VERIFIER_IMAGE"], task_version="v1")
    assert trial.verdict == "FAIL", (trial.reason, binding.verifier)


async def test_harbor_builds_separate_verifier_in_verify_namespace(tmp_path: Path) -> None:
    """Harbor 0.25.0's own separate-verifier construction (trial.py) yields our verify-namespace environment."""
    from harbor.environments.factory import EnvironmentFactory
    from harbor.models.task.verifier_mode import resolve_verifier_environment_definition
    from harbor.trial.trial import Trial as HarborTrial

    task_dir = harbor_task.materialize(TEMPLATES / "fix-bucket-policy", tmp_path / "task")
    trial = make_trial(tmp_path, trial_id="t-harborverif1")
    cfg = harbor_run.trial_config(trial, task_dir, tmp_path / "trials", kubeconfig=kubeconfig(tmp_path),
                                  kube_context=None, sandbox_image=PINNED, verifier_image=VERIFIER,
                                  verifier_deadline_s=30)
    ht = await HarborTrial.create(cfg)
    definition = resolve_verifier_environment_definition(ht.task.config, ht.task.paths, None)
    assert definition is not None
    plan = ht._network_plan(None, env_config=definition.config)
    env = EnvironmentFactory.create_environment_from_config(
        config=ht.config.environment, stream=False, environment_dir=definition.directory,
        enable_environment_dir_upload=not definition.bundled_tests, environment_name=ht.task.short_name,
        session_id=ht._separate_verifier_session_id("grade"), trial_paths=ht.paths, task_env_config=definition.config,
        logger=ht.logger, auto_mounts=ht._verifier_env_mounts(definition.config),
        network_policy=plan.verifier_env_baseline, phase_network_policies=[plan.verifier_phase])
    ht._validate_separate_verifier_env_policies(env, plan=plan)
    assert isinstance(env, RehearsalNamespaceEnvironment) and env.is_verifier
    assert (env.namespace, env.image) == ("verify-11111111-gov-1", VERIFIER)
    assert plan.verifier_phase.network_mode == NetworkMode.NO_NETWORK
    assert not env._upload_allowed("/logs/artifacts") and env._upload_allowed("/tests/verify.py")
