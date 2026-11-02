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
4. ``harbor_run.accept``: verdict bound to (trial, attempt, artifact digest, verifier Pod UID,
   verifier image digest, task version). Harbor stops both environments, which deletes the
   namespaces; what is left is recorded on the Trial.

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
from . import harbor_binding, harbor_run, harbor_task, task_generator, trial_token
from .cluster import Cluster, ClusterError, Names
from .harbor_env import DefinitionInvalid
from .mcp_tools import ToolLayer, TrialGuard, build_server
from .sdk_options import LLMUnconfigured, llm_env, vault_approle
from .sdk_options import provider as llm_provider
from .server_tools import OpenBao, ServerTools, ServerToolSettings, ToolBlocked
from .snapshot import Snapshot, SnapshotRejected, SnapshotStore
from .trials import Trial, TrialStore

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
        )


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


def build_envelope(req: TrialRequest, trial_id: str, deadline: float, budget_usd: float,
                   al: allowlist_mod.Allowlist, instruction: str | None,
                   snapshot: Snapshot | None = None) -> dict[str, Any]:
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
            "analysis": req.analysis,
            "repo_snapshot": {k2: snapshot.manifest.get(k2) for k2 in ("input_digest", "files", "withheld")}
            if snapshot else None}.items() if v is not None},
    }


def expected_for(trial: Trial) -> dict[str, Any]:
    """What the private verifier binds the proposal to: the Trial's own envelope, never the proposal."""
    env = trial.envelope
    return {"role": trial.role, "plan_id": env.get("plan_id"), "trial_id": trial.trial_id, "attempt": trial.attempt,
            "evidence_ids": sorted(str(e.get("id")) for e in env.get("evidence") or [])}


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
                                task["instruction"] if task else None, snapshot),
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
        t = asyncio.create_task(self.run(trial), name=f"trial-{trial.trial_id}")
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
        trial.names = Names.of(trial.plan_id, trial.attempt, trial.trial_id)
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
            harbor_run.accept_result(trial, binding, result, verifier_dir=ht.paths.verifier_dir,
                                     verifier_image=str(self.s.verifier_image), task_version=task["version"])
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
        bao = server_tools.bao
        checks["openbao"] = ("ok" if bao.logged_in else "openbao_unconfigured" if not bao.configured
                             else "openbao_not_logged_in")
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
    return app
