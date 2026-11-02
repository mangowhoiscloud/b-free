"""Rehearsal service: ``POST /v1/trials``, ``GET /v1/trials/{id}`` and the tool layer at ``/mcp``.

One Trial per attempt = one Harbor 0.25.0 Trial (05 4, 07 3). Preconditions are checked first;
any that is not met stops the request before anything is created and is returned as the
reason (``llm_unconfigured`` first); nothing is simulated. Then, per Trial:

1. ``harbor_task.materialize``: the Harbor task folder from the template (separate verifier,
   no-network, no sandbox-to-verifier artifacts, platform verifier entry).
2. ``harbor.trial.Trial`` with ``harbor_env.RehearsalNamespaceEnvironment`` (namespace +
   baseline + sandbox Pod by the provisioner, isolation check, exec identity) and
   ``harbor_agent.RehearsalAgent`` (agent worker process with a Trial token; acts only through
   ``/mcp``; ``plan_propose`` freezes the artifact; the platform applies it).
3. Harbor's separate verifier in ``verify-<plan>-<n>``: the frozen artifact from the platform,
   the private tests, ``reward.json``.
3b. If the proposal has an ``rgw_bucket_policy``: the behavioural verification Job in
   ``verify-<plan>-<n>-job`` (``behavioral.py``) applies it to the RGW test bucket and checks that an
   unsigned GET is refused and a signed GET works (B7).
4. ``harbor_run.accept``: verdict bound to (trial, attempt, artifact digest, verifier Pod UID,
   verifier image digest, task version, and the behavioural Job when it was required). Harbor stops
   both environments, which deletes the namespaces; what is left is recorded on the Trial.

Run: ``uvicorn bravo_rehearsal.service:create_app --factory --port 8090``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
import tomllib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel, ConfigDict, Field
from starlette.routing import Route

from . import allowlist as allowlist_mod
from . import behavioral as behavioral_mod
from . import behavioral_app, drops, harbor_binding, harbor_run, harbor_task, task_generator, trial_token
from .cluster import Cluster, ClusterError, Names
from .harbor_env import DefinitionInvalid
from .mcp_tools import ToolLayer, TrialGuard, build_server
from .sdk_options import LLMUnconfigured, llm_env, vault_approle
from .sdk_options import provider as llm_provider
from .server_tools import OpenBao, ServerTools, ServerToolSettings, ToolBlocked
from .snapshot import Snapshot, SnapshotRejected, SnapshotStore
from .trials import CALL_ORIGIN, Trial, TrialStore

LOG = logging.getLogger("bravo_rehearsal.service")
PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # platform/rehearsal
ROLES_BY_TASK_KEY = "role"


def _digest_pinned(image: str) -> bool:
    return "@sha256:" in image and len(image.rsplit("@sha256:", 1)[1]) == 64


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    api_token: str | None
    token_key: bytes | None
    kubeconfig: Path | None
    kube_context: str | None
    sandbox_image: str | None
    verifier_image: str | None
    tool_layer_url: str | None
    tasks_dir: Path
    allowlists_dir: Path
    trial_deadline_s: int
    verifier_deadline_s: int
    budget_usd: float
    idle_s: int
    trials_dir: Path = PACKAGE_ROOT / "runs"
    # Verification Jobs on the destination (digest-pinned images present on its nodes). gate_probe_image: the platform
    # gate image (bravo_gate OBJ-01..06 for bucket policies). app_image / db_image: the destination's current app and
    # PostgreSQL images (app_migration). Unset -> that Job does not run and a PASS needing it is an INFRA_ERROR.
    gate_probe_image: str | None = None
    app_image: str | None = None
    db_image: str | None = None
    profiles_file: Path | None = None  # destination profiles (deploy/platform/control/config/destination-profiles.json)
    prometheus_url: str | None = None  # Hubble drop counters of the Trial namespaces (hubble_drop_total)
    eso_openbao_addr: str = "https://openbao:8200"  # what the destination's ESO controller dials (D-16 link)

    @property
    def snapshots_dir(self) -> Path:
        env = os.environ.get("BRAVO_REHEARSAL_SNAPSHOT_DIR", "").strip()
        return Path(env).expanduser() if env else self.trials_dir / "snapshots"

    @classmethod
    def from_env(cls) -> ServiceSettings:
        e = os.environ.get
        key = e("BRAVO_REHEARSAL_TOKEN_KEY", "").encode()
        kube = e("BRAVO_REHEARSAL_KUBECONFIG", "").strip()
        return cls(
            api_token=e("BRAVO_REHEARSAL_API_TOKEN") or None,
            token_key=key if len(key) >= 32 else None,
            kubeconfig=Path(kube).expanduser() if kube else None,
            kube_context=e("BRAVO_REHEARSAL_KUBE_CONTEXT") or None,
            sandbox_image=e("BRAVO_REHEARSAL_SANDBOX_IMAGE") or None,
            verifier_image=e("BRAVO_REHEARSAL_VERIFIER_IMAGE") or None,
            tool_layer_url=(e("BRAVO_REHEARSAL_TOOL_LAYER_URL") or "").rstrip("/") or None,
            tasks_dir=Path(e("BRAVO_REHEARSAL_TASKS_DIR") or PACKAGE_ROOT / "tasks" / "templates"),
            allowlists_dir=allowlist_mod.default_dir(),
            trial_deadline_s=int(e("BRAVO_REHEARSAL_TRIAL_DEADLINE_S", "900")),
            verifier_deadline_s=int(e("BRAVO_REHEARSAL_VERIFIER_DEADLINE_S", "120")),
            budget_usd=float(e("BRAVO_REHEARSAL_BUDGET_USD", "2.0")),
            idle_s=int(e("BRAVO_REHEARSAL_IDLE_S", "60")),
            trials_dir=Path(e("BRAVO_REHEARSAL_TRIALS_DIR") or PACKAGE_ROOT / "runs").expanduser(),
            gate_probe_image=e("BRAVO_REHEARSAL_GATE_PROBE_IMAGE") or None,
            app_image=e("BRAVO_REHEARSAL_APP_IMAGE") or None,
            db_image=e("BRAVO_REHEARSAL_DB_IMAGE") or None,
            profiles_file=Path(e("BRAVO_REHEARSAL_PROFILES")) if e("BRAVO_REHEARSAL_PROFILES") else None,
            prometheus_url=(e("BRAVO_REHEARSAL_PROMETHEUS_URL") or "").rstrip("/") or None,
            eso_openbao_addr=e("BRAVO_REHEARSAL_ESO_OPENBAO_ADDR") or "https://openbao:8200",
        )

    def profile(self, target_env: str | None) -> dict[str, Any] | None:
        """The destination profile for ``target_env`` (names and paths only), or None."""
        if not target_env or self.profiles_file is None:
            return None
        try:
            doc = json.loads(self.profiles_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        prof = (doc.get("profiles") or {}).get(target_env) if isinstance(doc, dict) else None
        return prof if isinstance(prof, dict) else None


class TrialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    rehearsal_id: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1, le=100)
    max_attempts: int = Field(ge=1, le=10)
    task: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    role: str = Field(pattern=r"^[a-z_]{1,32}$")
    state: str = Field(default="REHEARSE", pattern=r"^[A-Z_]{1,32}$")
    prior_failures: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=64)  # {id, kind, summary}
    source_env: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    target_env: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    analysis: dict[str, Any] | None = None
    # Collector inventory digest of the customer repo snapshot to bind (PUT /v1/snapshots first).
    inventory_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def load_task(tasks_dir: Path, task: str) -> dict[str, Any] | None:
    folder = harbor_task.find(tasks_dir, task)  # platform/agent/harbor/<task> first, then tasks_dir
    if folder is None:
        return None
    meta = folder / "task.toml"
    doc = tomllib.loads(meta.read_text(encoding="utf-8"))
    if harbor_task.is_agent_template(folder):
        doc.setdefault(ROLES_BY_TASK_KEY, folder.name)  # platform/agent/harbor/<role>/
    tests_dir = folder / "tests"
    app_dir = folder / "environment" / "app"
    return {
        "folder": folder,
        "meta": doc,
        "instruction": (folder / "instruction.md").read_text(encoding="utf-8"),
        "tests": {p.name: p.read_text(encoding="utf-8") for p in sorted(tests_dir.glob("*.py"))},
        "app_files": {f"/app/{p.relative_to(app_dir).as_posix()}": p.read_text(encoding="utf-8")
                      for p in sorted(app_dir.rglob("*")) if p.is_file()} if app_dir.is_dir() else {},
        "version": task_generator.digest(folder),
    }


def envelope_plan_id(plan_id: str) -> str:
    """The state machine's plan ID as ``bravo_agents`` binds it (``p-`` + lowercase, 13 6)."""
    pid = plan_id.lower()
    return pid if pid.startswith("p-") else f"p-{pid}"


def secret_path_prefix(target_env: str | None, profile: dict[str, Any] | None) -> str | None:
    """Where the destination's secrets live in the platform OpenBao: the profile's prefix, else the D-26 layout
    ``envs/<target_env>/`` (keys.sh). The private verifier refuses OpenBao paths and ExternalSecret keys outside it."""
    pfx = ((profile or {}).get("secret_store") or {}).get("path_prefix")
    if isinstance(pfx, str) and pfx:
        return pfx
    return f"envs/{target_env}/" if target_env else None


def build_envelope(req: TrialRequest, trial_id: str, deadline: float, budget_usd: float,
                   al: allowlist_mod.Allowlist, instruction: str | None,
                   snapshot: Snapshot | None = None, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """``bravo_agents.StateEnvelope`` as JSON. The approved scope is the role's allowlist, never wider."""
    spec = al.spec
    return {
        "schema_version": "1", "tenant_id": req.tenant_id, "plan_id": envelope_plan_id(req.plan_id),
        "trial_id": trial_id, "attempt": req.attempt, "max_attempts": req.max_attempts, "state": req.state,
        "role": req.role, "task": req.task,
        "deadline": datetime.fromtimestamp(deadline, UTC).isoformat(), "budget_usd": budget_usd,
        "approved_scope": {"mcp_tools": list(spec.mcp_tools) if spec else [],
                           "sandbox_ops": list(spec.sandbox.ops) if spec else [],
                           "checks": list(spec.sandbox.checks) if spec else []},
        "prior_failures": req.prior_failures, "evidence": req.evidence,
        "inputs": {k: v for k, v in {
            "instruction": instruction, "source_env": req.source_env, "target_env": req.target_env,
            "analysis": req.analysis, "destination_profile": profile,
            "repo_snapshot": {**{k2: snapshot.manifest.get(k2) for k2 in ("input_digest", "files", "withheld")},
                              "sandbox_path": "/src"} if snapshot else None,
            "sandbox_paths": {"read": list(spec.sandbox.paths.read), "write": list(spec.sandbox.paths.write)}
            if spec else None}.items() if v is not None},
    }


def expected_for(trial: Trial) -> dict[str, Any]:
    """What the private verifier binds the proposal to: the Trial's own envelope, never the proposal."""
    env = trial.envelope
    inputs = env.get("inputs") or {}
    profile = inputs.get("destination_profile") or {}
    return {"role": trial.role, "plan_id": env.get("plan_id"), "trial_id": trial.trial_id, "attempt": trial.attempt,
            "evidence_ids": sorted(str(e.get("id")) for e in env.get("evidence") or []),
            "secret_path_prefix": secret_path_prefix(inputs.get("target_env"), profile or None),
            # T31 applies only inside these (policies.scoped): the verifier checks the same, so PASS predicts apply.
            "app_namespace": (profile.get("app") or {}).get("namespace"),
            "bucket": (profile.get("object_store") or {}).get("bucket")}


def envelope_problem(envelope: dict[str, Any]) -> str | None:
    try:
        from bravo_agents import StateEnvelope
    except ImportError:
        return "agent_runtime_unavailable"
    try:
        StateEnvelope.model_validate(envelope)
    except ValueError:
        return "envelope_invalid"
    return None


def new_trial(req: TrialRequest, settings: ServiceSettings, idem_key: str, request_digest: str) -> Trial:
    trial_id = f"t-{uuid.uuid4().hex}"
    deadline = time.time() + settings.trial_deadline_s
    al = allowlist_mod.load(req.role, settings.allowlists_dir)
    task = load_task(settings.tasks_dir, req.task)
    snapshot = SnapshotStore(settings.snapshots_dir).get(req.tenant_id, req.plan_id, req.inventory_digest)
    return Trial(
        trial_id=trial_id, tenant_id=req.tenant_id, plan_id=req.plan_id,
        rehearsal_id=req.rehearsal_id, attempt=req.attempt, max_attempts=req.max_attempts, task=req.task,
        role=req.role, idem_key=idem_key, request_digest=request_digest, deadline=deadline, allowlist=al,
        envelope=build_envelope(req, trial_id, deadline, settings.budget_usd, al,
                                task["instruction"] if task else None, snapshot, settings.profile(req.target_env)),
        repo_snapshot=str(snapshot.root) if snapshot else None,
    )


class Orchestrator:
    def __init__(self, settings: ServiceSettings, store: TrialStore, bao: OpenBao | None = None) -> None:
        self.s = settings
        self.store = store
        self.bao = bao  # the service's OpenBao identity: LLM credential presence and one-use worker secret_ids
        self.cluster = Cluster(settings.kubeconfig, settings.kube_context) if settings.kubeconfig else None
        self._tasks: set[asyncio.Task[None]] = set()

    def preconditions(self, req: TrialRequest) -> list[str]:
        """Everything that must hold before anything is created. Order fixes the reported reason."""
        missing: list[str] = []
        al = allowlist_mod.load(req.role, self.s.allowlists_dir)
        try:
            llm_env(al.model_tier)
        except LLMUnconfigured:
            missing.append("llm_unconfigured")
        if self.cluster is None:
            missing.append("cluster_unconfigured")
        if not (self.s.sandbox_image and self.s.verifier_image and _digest_pinned(self.s.sandbox_image)
                and _digest_pinned(self.s.verifier_image)):
            missing.append("verifier_unconfigured")
        if self.s.token_key is None or not self.s.tool_layer_url:
            missing.append("tool_layer_unconfigured")
        task = load_task(self.s.tasks_dir, req.task)
        if task is None:
            missing.append("task_unknown")
        elif task["meta"].get(ROLES_BY_TASK_KEY) != req.role:
            missing.append("role_task_mismatch")
        if al.problem:
            missing.append(al.problem)
        else:
            if req.state not in al.states:
                missing.append("state_not_allowed_for_role")
            env = build_envelope(req, "t-precheck", time.time() + self.s.trial_deadline_s, self.s.budget_usd, al,
                                 task["instruction"] if task else None)
            problem = envelope_problem(env)
            if problem:
                missing.append(problem)
        return missing

    async def llm_credential(self) -> str:
        """``ok`` or why a worker could not get its model credential now. Presence from KV metadata only:
        the service never reads the token (the worker does, with a one-use AppRole secret_id)."""
        try:
            from bravo_agents.llm_secret import LLM_SECRETS

            spec = LLM_SECRETS.get(llm_provider())
        except (ImportError, LLMUnconfigured):
            return "llm_unconfigured"
        if spec is None:
            return "ok"  # gateway providers: no model credential in the worker
        if self.bao is None:
            return "openbao_unconfigured"
        return await self.bao.secret_present(spec.path)

    def start(self, trial: Trial) -> None:
        self._spawn(self.run(trial), f"trial-{trial.trial_id}")

    def _spawn(self, coro: Any, name: str) -> None:
        t = asyncio.create_task(coro, name=name)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def run(self, trial: Trial) -> None:
        """One Harbor Trial: our namespace environments, our agent, Harbor's separate verifier."""
        from harbor.trial.hooks import TrialEvent, TrialHookEvent
        from harbor.trial.trial import Trial as HarborTrial

        assert self.cluster is not None and self.s.kubeconfig is not None
        task = load_task(self.s.tasks_dir, trial.task)
        assert task is not None
        trial.names = Names.of(trial.plan_id, trial.attempt, trial.trial_id, trial.role)
        binding = harbor_binding.Binding(trial=trial, worker_env=lambda d: self._worker_env(trial, task, d),
                                         cluster=self.cluster)
        harbor_binding.register(binding)
        try:
            await self.cluster.reachable()
            trials_dir = self.s.trials_dir.resolve()
            trials_dir.mkdir(parents=True, exist_ok=True)
            task_dir = harbor_task.materialize(task["folder"], trials_dir / f"{trial.trial_id}.task",
                                               expected_for(trial),
                                               Path(trial.repo_snapshot) if trial.repo_snapshot else None)
            trial.verifier_kind = str(harbor_task.task_meta(task_dir).get("verifier_kind") or "unknown")
            cfg = harbor_run.trial_config(
                trial, task_dir, trials_dir, kubeconfig=self.s.kubeconfig, kube_context=self.s.kube_context,
                sandbox_image=str(self.s.sandbox_image), verifier_image=str(self.s.verifier_image),
                verifier_deadline_s=self.s.verifier_deadline_s)
            ht = await HarborTrial.create(cfg)
            trial.harbor_trial_dir = str(ht.paths.trial_dir)

            async def verifying(_event: TrialHookEvent) -> None:
                trial.phase = "verifying"

            ht.add_hook(TrialEvent.VERIFICATION_START, verifying)
            result = await ht.run()
            # B7: a proposed bucket policy is also applied to the RGW test bucket and probed by a separate Job.
            beh = await self.behavioral(trial) if result.exception_info is None else None
            harbor_run.accept_result(trial, binding, result, verifier_dir=ht.paths.verifier_dir,
                                     verifier_image=str(self.s.verifier_image), task_version=task["version"],
                                     behavioral=beh)
            self._spawn(self.network_drops(trial), f"drops-{trial.trial_id}")
        except ClusterError as exc:
            trial.finish("INFRA_ERROR", exc.reason)
        except (harbor_task.TaskInvalid, DefinitionInvalid) as exc:
            trial.finish("INFRA_ERROR", exc.reason)
        except asyncio.CancelledError:
            trial.finish("INFRA_ERROR", "service_stopping")
            raise
        except Exception as exc:  # recorded as infra error; never as a pass
            LOG.exception("trial failed trial_id=%s", trial.trial_id)
            trial.finish("INFRA_ERROR", f"internal:{type(exc).__name__}")
        finally:
            trial.locked = True
            harbor_binding.release(trial.trial_id)

    async def behavioral(self, trial: Trial) -> dict[str, Any] | None:
        """The verification Job the frozen proposal needs, or None when it needs none. Never raises: a Job that cannot
        run is a record with ``ran: False`` and a reason (a would-be PASS then becomes INFRA_ERROR, never static-only).

        - ``rgw_bucket_policy`` translations: the gate's own OBJ-01..06 against the RGW test bucket (``behavioral.py``).
        - ``app_migration``: the patched app in the destination's runtime with its password from OpenBao through ESO,
          serving a chat write (``behavioral_app.py``).
        Keys are read here from OpenBao and go only to the Job's namespace (deleted after)."""
        if trial.role == behavioral_app.ROLE and trial.artifact is not None:
            trial.phase = "verifying"
            return await self._app_job(trial)
        docs = behavioral_mod.proposed_policies(trial.artifact)
        if not docs:
            return None
        trial.phase = "verifying"
        env = (trial.envelope.get("inputs") or {}).get("target_env")
        if not env:
            return {"ran": False, "reason": "target_env_missing"}
        if self.bao is None or self.s.kubeconfig is None or trial.names is None:
            return {"ran": False, "reason": "openbao_unconfigured" if self.bao is None else "cluster_unconfigured"}
        if not (self.s.gate_probe_image and _digest_pinned(self.s.gate_probe_image)):
            return {"ran": False, "reason": "gate_probe_image_unconfigured"}
        try:
            record = await self.bao.read(behavioral_mod.SECRET_PATH.format(env=env))
        except ToolBlocked as exc:
            return {"ran": False, "reason": exc.reason}
        missing = [k for k in behavioral_mod.SECRET_FIELDS if not record.get(k)]
        if missing:
            return {"ran": False, "reason": "test_bucket_record_incomplete", "missing": missing}
        kubeconfig, names, image = self.s.kubeconfig, trial.names, str(self.s.gate_probe_image)

        def work() -> dict[str, Any]:
            return behavioral_mod.JobRun(kubeconfig, self.s.kube_context).run(
                names=names, trial_id=trial.trial_id, image=image, record=record, docs=docs,
                deadline_s=self.s.verifier_deadline_s)

        try:
            return await asyncio.to_thread(work)
        except Exception as exc:  # cluster unreachable, config errors: the Job did not run
            LOG.warning("behavioral job failed trial_id=%s error=%s", trial.trial_id, type(exc).__name__)
            return {"ran": False, "reason": f"job_error:{type(exc).__name__}"}

    async def _app_job(self, trial: Trial) -> dict[str, Any]:
        env = (trial.envelope.get("inputs") or {}).get("target_env")
        images = {"app": self.s.app_image, "db": self.s.db_image, "probe": self.s.verifier_image}
        if not env:
            return {"ran": False, "reason": "target_env_missing", "kind": behavioral_app.KIND}
        if not all(v and _digest_pinned(v) for v in images.values()):
            return {"ran": False, "reason": "app_job_images_unconfigured", "kind": behavioral_app.KIND}
        if self.bao is None or not self.bao.cfg.addr or self.s.kubeconfig is None or trial.names is None:
            return {"ran": False, "reason": "openbao_unconfigured", "kind": behavioral_app.KIND}
        if not trial.repo_snapshot:
            return {"ran": False, "reason": "repo_unconfigured", "kind": behavioral_app.KIND}
        try:
            original = behavioral_app.read_tree(Path(trial.repo_snapshot))
            patched, changed = behavioral_app.apply_diff(original, str((trial.artifact or {}).get("diff") or ""))
        except behavioral_app.DiffError as exc:
            # The proposal's diff does not apply to the snapshot it was made from: the proposal fails (not infra).
            return {"ran": False, "reason": f"diff_rejected:{exc.reason}", "proposal_fault": True,
                    "kind": behavioral_app.KIND,
                    "checks": [{"name": "app:diff_applies_to_snapshot", "ok": False, "detail": exc.reason}]}
        try:
            role_id, secret_id = await self.bao.mint_secret_id(behavioral_app.ESO_APPROLE.format(env=env), ttl="120s",
                                                               num_uses=behavioral_app.ESO_SECRET_ID_USES)
        except ToolBlocked as exc:
            return {"ran": False, "reason": f"eso_approle:{exc.reason}", "kind": behavioral_app.KIND}
        ca = Path(self.bao.cfg.ca_cert).read_text(encoding="utf-8") if self.bao.cfg.ca_cert else ""
        eso = {"server": self.s.eso_openbao_addr, "ca_pem": ca, "role_id": role_id, "secret_id": secret_id,
               "kv_key": behavioral_app.ESO_KV_PATH.format(env=env)}
        kubeconfig, names = self.s.kubeconfig, trial.names
        pinned = {k: str(v) for k, v in images.items()}

        def work() -> dict[str, Any]:
            return behavioral_app.AppJobRun(kubeconfig, self.s.kube_context).run(
                names=names, trial_id=trial.trial_id, images=pinned, eso=eso, original=original, patched=patched,
                changed=changed, deadline_s=max(self.s.verifier_deadline_s, 240))

        try:
            return await asyncio.to_thread(work)
        except Exception as exc:  # noqa: BLE001 - the Job did not run; recorded, never a pass
            LOG.warning("app job failed trial_id=%s error=%s", trial.trial_id, type(exc).__name__)
            return {"ran": False, "reason": f"job_error:{type(exc).__name__}", "kind": behavioral_app.KIND}
        finally:
            eso["secret_id"] = ""  # spent or not, it is not kept

    async def network_drops(self, trial: Trial) -> None:
        """After the verdict: Hubble's drop counters for the Trial's namespaces from the platform Prometheus (the
        agents' ring buffers hold seconds of flows, so ``hubble observe --last`` after a Trial reads 0; P13)."""
        if not self.s.prometheus_url or trial.names is None:
            trial.network_drops = {"source": "unconfigured"}
            return
        env = (trial.envelope.get("inputs") or {}).get("target_env") or ""
        nss = [trial.names.rehearsal_ns, trial.names.verify_ns, behavioral_mod.namespace_of(trial.names),
               behavioral_app.namespace_of(trial.names)]
        start = trial.phase_history[0]["t"] if trial.phase_history else time.time()
        await asyncio.sleep(drops.SCRAPE_WAIT_S)  # let the last drops reach Prometheus (remote write from the link)
        trial.network_drops = await drops.collect(self.s.prometheus_url, env, nss, start, time.time())

    async def _worker_env(self, trial: Trial, task: dict[str, Any], config_dir: str) -> dict[str, str]:
        """07 7 / 13 6: built from scratch, exact names only. Route settings, the Trial token and, for the
        OpenBao providers, one secret_id (60 s, 1 use) for the provider's AppRole; never the model credential."""
        assert self.s.token_key is not None and trial.names is not None
        route = llm_env(trial.allowlist.model_tier)
        delivery: dict[str, str] = {}
        approle = vault_approle(route)
        if approle is not None:
            # Where the worker logs in (the service's own OpenBao settings, not secrets) and one secret_id.
            if self.bao is None or not self.bao.cfg.addr:
                raise LLMUnconfigured("openbao_unconfigured")
            try:
                role_id, secret_id = await self.bao.mint_secret_id(approle)
            except ToolBlocked as exc:
                raise LLMUnconfigured(exc.reason) from exc
            delivery = {"BRAVO_BAO_ADDR": self.bao.cfg.addr, "BRAVO_BAO_KV_MOUNT": self.bao.cfg.kv_mount,
                        **({"BRAVO_BAO_CACERT": self.bao.cfg.ca_cert} if self.bao.cfg.ca_cert else {}),
                        "BRAVO_LLM_BAO_ROLE_ID": role_id, "BRAVO_LLM_BAO_SECRET_ID": secret_id}
        token = trial_token.issue(self.s.token_key, trial_token.TrialClaims(
            trial_id=trial.trial_id, tenant_id=trial.tenant_id, plan_id=trial.plan_id, attempt=trial.attempt,
            namespace=trial.names.rehearsal_ns, ops=tuple(sorted(trial.allowlist.ops)),
            budget_usd=self.s.budget_usd, exp=int(trial.deadline)))
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": config_dir,
            **route,
            **delivery,
            "BRAVO_TRIAL_TOKEN": token,
            "BRAVO_TOOL_LAYER_URL": f"{self.s.tool_layer_url}/mcp",
            "BRAVO_EVIDENCE_URL": f"{self.s.tool_layer_url}/v1/evidence",
            "BRAVO_TRIAL_ENVELOPE": json.dumps(trial.envelope, ensure_ascii=False),
            "BRAVO_AGENT_ROLE": trial.role,
            "BRAVO_AGENT_ALLOWLIST_DIR": str(self.s.allowlists_dir),
        }


# Demo probes (spec S2 4b scene "버킷 삭제 시도 차단"), sent by an operator through the platform, never by the agent:
# the same tool-layer decision the agent's calls get, recorded with origin=probe so no screen or evidence counts them as
# agent behaviour (B10/P13).
PROBE_OPS: tuple[Any, ...] = (
    "aws s3 rb s3://chat-attachments --force",
    {"op": "kubectl", "verb": "delete", "kind": "objectbucketclaim", "name": "chat-attachments"},
)


async def run_probes(layer: ToolLayer, trial: Trial) -> list[dict[str, Any]]:
    """Send ``PROBE_OPS`` through the tool layer for a running Trial; every record says ``origin: probe``."""
    token = CALL_ORIGIN.set("probe")
    try:
        out = []
        for op in PROBE_OPS:
            await layer.call(trial, "rehearsal_exec", {"op": op})
            ev = trial.tool_events[-1] if trial.tool_events else {}
            out.append({"op": op, **{k: ev.get(k) for k in ("seq", "allowed", "rule", "origin", "at")}})
        return out
    finally:
        CALL_ORIGIN.reset(token)


def create_app(settings: ServiceSettings | None = None) -> FastAPI:
    settings = settings or ServiceSettings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = TrialStore()
    server_tools = ServerTools(ServerToolSettings.from_env(settings.trials_dir))
    orch = Orchestrator(settings, store, server_tools.bao)

    def repo_root(trial: Trial) -> Path | None:
        """Only an uploaded snapshot of the customer repository; never the task template (B9)."""
        return Path(trial.repo_snapshot) if trial.repo_snapshot else None

    layer = ToolLayer(store, orch.cluster, repo_root, server_tools)
    manager = StreamableHTTPSessionManager(app=build_server(layer), stateless=True, json_response=True)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # AppRole secret_ids are single-use and short-lived: log in at start, then keep the token alive.
        keepalive: asyncio.Task[None] | None = None
        try:
            await server_tools.bao.login()
            if server_tools.bao.configured:
                keepalive = asyncio.create_task(server_tools.bao.keepalive(), name="openbao-keepalive")
        except ToolBlocked as exc:
            LOG.warning("openbao login failed reason=%s; server tools that need credentials are refused", exc.reason)
        try:
            async with manager.run():
                yield
        finally:
            if keepalive is not None:
                keepalive.cancel()
            await orch.close()

    app = FastAPI(title="Bravo rehearsal service", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.router.routes.append(Route("/mcp", endpoint=TrialGuard(store, lambda: settings.token_key, manager),
                                   methods=["GET", "POST", "DELETE"]))

    def caller_ok(auth: str | None) -> bool:
        return bool(settings.api_token) and auth is not None and hmac.compare_digest(
            auth.encode(), f"Bearer {settings.api_token}".encode())

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Any:
        """Preconditions for running a Trial now (audit 35). Reasons only, never values."""
        checks: dict[str, str] = {}
        try:
            llm_env("opus")
            checks["llm"] = "ok"
        except LLMUnconfigured:
            checks["llm"] = "llm_unconfigured"
        if orch.cluster is None:
            checks["cluster"] = "cluster_unconfigured"
        else:
            try:
                await asyncio.wait_for(orch.cluster.reachable(), 5)
                checks["cluster"] = "ok"
            except (ClusterError, TimeoutError) as exc:
                checks["cluster"] = getattr(exc, "reason", "cluster_unreachable")
        images_ok = all(img and _digest_pinned(img) for img in (settings.sandbox_image, settings.verifier_image))
        checks["images"] = "ok" if images_ok else "verifier_unconfigured"
        checks["tool_layer"] = "ok" if settings.token_key and settings.tool_layer_url else "tool_layer_unconfigured"
        checks["openbao"] = await server_tools.bao.check()
        checks["gate"] = "ok" if server_tools.s.gate_url else "gate_unconfigured"
        # Whether the model credential is stored in OpenBao (KV metadata only, never the value).
        checks["llm_token"] = await orch.llm_credential()
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)

    @app.post("/v1/evidence", status_code=202)
    async def evidence(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """Worker hook events (``bravo_agents.hooks``). Not authoritative: the tool layer's record is."""
        if settings.token_key is None:
            return JSONResponse({"error": "token_key_unconfigured"}, status_code=503)
        if not authorization or not authorization.startswith("Bearer "):
            return JSONResponse({"error": "token_missing"}, status_code=401)
        try:
            claims = trial_token.verify(settings.token_key, authorization[7:].strip())
        except trial_token.TokenError as exc:
            return JSONResponse({"error": exc.reason}, status_code=401)
        trial = store.get(claims.trial_id)
        if trial is None or trial.tenant_id != claims.tenant_id or trial.attempt != claims.attempt:
            return JSONResponse({"error": "token_trial_mismatch"}, status_code=403)
        try:
            event = await request.json()
        except ValueError:
            return JSONResponse({"error": "bad_request"}, status_code=422)
        if not isinstance(event, dict) or event.get("trial_id") != trial.trial_id:
            return JSONResponse({"error": "bad_request"}, status_code=422)
        if not trial.add_hook_event(event):
            return JSONResponse({"status": "duplicate"}, status_code=409)
        return {"status": "accepted"}

    @app.put("/v1/snapshots/{tenant_id}/{plan_id}")
    async def put_snapshot(tenant_id: str, plan_id: str, request: Request,
                           authorization: str | None = Header(default=None),
                           inventory_digest: str | None = Header(default=None,
                                                                 alias="X-Bravo-Inventory-Digest")) -> Any:
        """Customer repo snapshot for repo_read and /src, bound to the collector inventory digest."""
        if not caller_ok(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not inventory_digest:
            return JSONResponse({"error": "inventory_digest_missing"}, status_code=422)
        body = await request.body()
        try:
            manifest = await asyncio.to_thread(SnapshotStore(settings.snapshots_dir).ingest, tenant_id, plan_id,
                                               body, inventory_digest)
        except SnapshotRejected as exc:
            return JSONResponse({"error": exc.reason}, status_code=422)
        return manifest

    @app.post("/v1/trials", status_code=202)
    async def start(request: Request, idempotency_key: str = Header(alias="Idempotency-Key"),
                    authorization: str | None = Header(default=None)) -> Any:
        if not caller_ok(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            req = TrialRequest.model_validate(await request.json())
        except Exception:
            return JSONResponse({"error": "bad_request"}, status_code=422)
        digest = hashlib.sha256(json.dumps(req.model_dump(), sort_keys=True).encode()).hexdigest()
        existing = store.by_key(idempotency_key)
        if existing is not None:
            if existing.request_digest != digest:
                return JSONResponse({"error": "idempotency_key_reuse"}, status_code=409)
            return {"trial_id": existing.trial_id, "phase": existing.phase}
        missing = orch.preconditions(req)
        if "llm_unconfigured" not in missing:
            credential = await orch.llm_credential()
            if credential != "ok":
                missing.insert(0, credential)
        if missing:
            return JSONResponse({"error": missing[0], "missing": missing}, status_code=503)
        trial = new_trial(req, settings, idempotency_key, digest)
        store.add(trial)
        orch.start(trial)
        return {"trial_id": trial.trial_id, "phase": trial.phase}

    @app.post("/v1/trials/{trial_id}/probes")
    async def probes(trial_id: str, authorization: str | None = Header(default=None)) -> Any:
        """Operator demo probes for a running Trial (API token, never the Trial token, so the agent cannot label its
        own calls). Results and the Trial record carry ``origin: probe``."""
        if not caller_ok(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        trial = store.get(trial_id)
        if trial is None:
            return JSONResponse({"error": "trial_not_found"}, status_code=404)
        if not trial.running():
            return JSONResponse({"error": "trial_not_running", "phase": trial.phase}, status_code=409)
        return {"trial_id": trial_id, "origin": "probe", "results": await run_probes(layer, trial)}

    @app.get("/v1/trials/{trial_id}")
    async def get(trial_id: str, authorization: str | None = Header(default=None)) -> Any:
        if not caller_ok(authorization):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        trial = store.get(trial_id)
        if trial is None:
            return JSONResponse({"error": "trial_not_found"}, status_code=404)
        return trial.view()

    app.state.store = store
    app.state.orchestrator = orch
    app.state.layer = layer
    return app
