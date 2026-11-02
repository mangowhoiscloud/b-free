"""Tool layer as an MCP server (Streamable HTTP, stateless) for the agent worker (§8.3, 05 2, 07 3, 13 4).

The agent worker holds one Trial token and nothing else. Every request: Bearer Trial token →
signature + expiry → the Trial exists, is running and is not locked → per-call checks, each
denial recorded on the Trial (the authoritative record, 07 3 "증거"):
  1. role allowlist (``platform/agent/allowlists/<role>.yaml`` via ``bravo_agents.load_allowlist``,
     deny by default; ``plan_propose`` is worker-only and never listed to the model),
  2. ``rehearsal_exec``: ``tool_layer.authorize_sandbox`` (role ops, read/write roots, fixed check
     argv; no kubectl for any role) against this Trial's sandbox Pod with the namespace-bound
     exec identity,
  3. server tools (``controls_describe``, ``evidence_read``, ``db_inspect``, ``db_rehearse``):
     platform credentials from OpenBao stay in this process; results are redacted (D-26),
  4. ``policy_evaluate`` / ``plan_propose``: per-role schema from ``bravo_agents.schemas``, binding
     to the Trial and the policy lint (``proposal.check``).
Tool names use ``_`` because model tool names do not accept ``.``; they map 1:1 to §8.3.
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from starlette.types import Receive, Scope, Send

from . import proposal as proposal_mod
from . import tool_layer, trial_token
from .cluster import Cluster, ClusterError
from .server_tools import ServerTools, ToolBlocked, redact
from .trials import Trial, TrialStore

MAX_RESULT_CHARS = 48_000  # below the 50,000-char spill threshold (07 7 "도구 결과 크기")
_trial: contextvars.ContextVar[Trial | None] = contextvars.ContextVar("bravo_trial", default=None)

_OBJ = {"type": "object"}
TOOL_DEFS: dict[str, tuple[str, dict[str, Any], bool]] = {
    "inventory_describe": (
        "Show the collected inventory and analysis for this plan (read only).",
        {"type": "object", "properties": {}, "additionalProperties": False},
        True,
    ),
    "repo_read": (
        "Read one file of the application's read-only source copy. Path is relative to the repository root.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
        True,
    ),
    "rehearsal_exec": (
        "Run one structured operation in this Trial's sandbox. Free-text commands are refused. "
        "op is one of: {op: read_file|list_dir, path}, {op: write_file, path, content}, "
        "{op: run_check, name, args}. Paths and checks are limited by your role's allowlist.",
        {"type": "object", "properties": {"op": _OBJ}, "required": ["op"], "additionalProperties": False},
        False,
    ),
    "controls_describe": (
        "Show the gate's control results (SEC-01 ... DR-01) for this plan's source and target environments.",
        {"type": "object", "properties": {"level": {"type": "string", "enum": ["question", "control"]}},
         "additionalProperties": False},
        True,
    ),
    "evidence_read": (
        "Read one stored evidence document by its id. Only ids listed in the platform state are readable.",
        {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
        True,
    ),
    "db_inspect": (
        "Run one fixed read-only catalog query (by name) on the source or destination database.",
        {"type": "object", "properties": {"query": {"type": "string"},
                                          "target": {"type": "string", "enum": ["source", "destination"]}},
         "required": ["query"], "additionalProperties": False},
        True,
    ),
    "db_rehearse": (
        "Run proposed SQL on this Trial's synthetic database in one transaction that is always rolled back. "
        "Returns per-statement status and error codes only.",
        {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"], "additionalProperties": False},
        False,
    ),
    "policy_evaluate": (
        "Check a proposal against your role's schema, the Trial binding and the policy lint without submitting it.",
        {"type": "object", "properties": {"proposal": _OBJ}, "required": ["proposal"], "additionalProperties": False},
        True,
    ),
    "plan_propose": (
        "Submit the final proposal (worker only). It is checked, frozen and bound by digest; the Trial then locks.",
        {"type": "object", "properties": {"proposal": _OBJ}, "required": ["proposal"], "additionalProperties": False},
        False,
    ),
}
HIDDEN_FROM_MODEL = frozenset({"plan_propose"})  # the worker calls it; tools/list never shows it
SERVER_TOOLS = frozenset({"controls_describe", "evidence_read", "db_inspect", "db_rehearse"})


def _clip(payload: dict[str, Any]) -> types.CallToolResult:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    truncated = len(text) > MAX_RESULT_CHARS
    if truncated:
        text = json.dumps({"truncated": True, "chars": len(text), "head": text[:MAX_RESULT_CHARS]}, ensure_ascii=False)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=bool(payload.get("blocked")))


def _binding(trial: Trial) -> proposal_mod.Binding:
    env = trial.envelope
    return proposal_mod.Binding(plan_id=str(env.get("plan_id")), trial_id=trial.trial_id, attempt=trial.attempt,
                                evidence_ids=frozenset(str(e.get("id")) for e in env.get("evidence") or []))


class ToolLayer:
    def __init__(self, store: TrialStore, cluster: Cluster | None, repo_root: Callable[[Trial], Path | None],
                 server_tools: ServerTools | None = None) -> None:
        self._store = store
        self._cluster = cluster
        self._repo_root = repo_root
        self._server = server_tools

    async def call(self, trial: Trial, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        denied = trial.allowlist.tool_denied(tool)
        if denied:
            trial.record(tool, args, False, denied)
            return {"blocked": denied}
        handler: dict[str, Callable[[Trial, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "inventory_describe": self._inventory,
            "repo_read": self._repo_read,
            "rehearsal_exec": self._exec,
            "policy_evaluate": self._policy,
            "plan_propose": self._propose,
        }
        if tool in SERVER_TOOLS:
            return await self._server_tool(trial, args, tool)
        return await handler[tool](trial, args)

    async def _inventory(self, trial: Trial, args: dict[str, Any]) -> dict[str, Any]:
        trial.record("inventory_describe", args, True, "ok")
        inputs = trial.envelope.get("inputs") or {}
        analysis = inputs.get("analysis")
        return {"analysis": analysis if analysis is not None else {"status": "unconfigured"},
                "prior_failures": trial.envelope.get("prior_failures", [])}

    async def _repo_read(self, trial: Trial, args: dict[str, Any]) -> dict[str, Any]:
        root = self._repo_root(trial)
        rel = args.get("path")
        if root is None:
            trial.record("repo_read", args, False, "repo_unconfigured")
            return {"blocked": "repo_unconfigured"}
        target = (root / str(rel)).resolve() if isinstance(rel, str) else None
        if target is None or not target.is_relative_to(root.resolve()) or not target.is_file() or target.is_symlink():
            trial.record("repo_read", args, False, "path_outside_repo")
            return {"blocked": "path_outside_repo"}
        trial.record("repo_read", args, True, "ok")
        return {"path": rel, "content": redact(target.read_text(encoding="utf-8", errors="replace"))}

    async def _exec(self, trial: Trial, args: dict[str, Any]) -> dict[str, Any]:
        op = args.get("op")
        decision = tool_layer.authorize_sandbox(op, trial.allowlist)
        if not decision.allowed:
            trial.record("rehearsal_exec", args, False, decision.rule)
            out: dict[str, Any] = {"blocked": decision.rule, "op": op.get("op") if isinstance(op, dict) else None}
            spec = trial.allowlist.spec
            if decision.rule == "path_outside_workspace" and spec is not None:
                out["allowed_read"] = list(spec.sandbox.paths.read)  # roots only; no file names, no values
                out["allowed_write"] = list(spec.sandbox.paths.write)
            return out
        if self._cluster is None or trial.box is None:
            trial.record("rehearsal_exec", args, False, "sandbox_unavailable")
            return {"blocked": "sandbox_unavailable"}
        try:
            rc, stdout, stderr = await self._cluster.exec(trial.box, ["sh", "-c", decision.command],
                                                          timeout_s=decision.timeout_s)
        except ClusterError as exc:
            trial.record("rehearsal_exec", args, False, exc.reason)
            return {"blocked": exc.reason}
        trial.record("rehearsal_exec", args, True, "ok", result="done" if rc == 0 else f"exit_{rc}")
        return redact({"exit_code": rc, "stdout": stdout, "stderr": stderr})

    async def _server_tool(self, trial: Trial, args: dict[str, Any], tool: str) -> dict[str, Any]:
        if self._server is None:
            trial.record(tool, args, False, "server_tools_unconfigured")
            return {"blocked": "server_tools_unconfigured"}
        inputs = trial.envelope.get("inputs") or {}
        try:
            if tool == "controls_describe":
                out = await self._server.controls_describe(inputs, args)
            elif tool == "evidence_read":
                out = await self._server.evidence_read(set(_binding(trial).evidence_ids), args,
                                                       tenant_id=trial.tenant_id, plan_id=trial.plan_id.lower())
            elif tool == "db_inspect":
                out = await self._server.db_inspect(inputs, trial.allowlist.db_queries, args)
            else:
                out = await self._server.db_rehearse(args)
        except ToolBlocked as exc:
            trial.record(tool, args, False, exc.reason)
            return {"blocked": exc.reason}
        trial.record(tool, args, True, "ok", result="error" if out.get("error") else "done")
        return out

    async def _policy(self, trial: Trial, args: dict[str, Any]) -> dict[str, Any]:
        p, problems = proposal_mod.check(args.get("proposal"), trial.role, _binding(trial))
        if p is not None and not problems:
            _, problem = await self._diff_check(trial, p)
            problems = [problem] if problem else problems
        trial.record("policy_evaluate", args, True, "ok")
        return {"valid": p is not None and not problems, "problems": problems}

    async def _diff_check(self, trial: Trial, p: Any) -> tuple[str | None, str | None]:
        """(the workspace's real diff, problem). The model sees tool output through redact() (D-26), which also masks
        code that merely looks like a secret (measured 2026-11-03 04:2x KST: `if "password=" in db.dsn...` became
        `if "[redacted]" ...`, so the proposal's diff no longer applied to its own snapshot). The proposal's diff must
        be the redacted view of the sandbox's own diff; the artifact then records the real one."""
        diff = getattr(p, "diff", None)
        if not isinstance(diff, str) or not diff:
            return None, None
        real = await self._workspace_diff(trial)
        if real is None:
            return None, None if trial.allowlist.spec is None or "make_diff" not in trial.allowlist.spec.sandbox.checks \
                else "diff_workspace_unreadable"
        if redact(real).strip() != diff.strip():
            return None, "diff_not_from_workspace"
        return real, None

    async def _workspace_diff(self, trial: Trial) -> str | None:
        """The sandbox's own ``make_diff`` (the role's declared check, platform side, never shown to the model).
        None when the role has no such check or the sandbox is gone."""
        spec = trial.allowlist.spec
        if spec is None or "make_diff" not in spec.sandbox.checks or self._cluster is None or trial.box is None:
            return None
        decision = tool_layer.authorize_sandbox({"op": "run_check", "name": "make_diff", "args": {}}, trial.allowlist)
        if not decision.allowed:
            return None
        try:
            rc, stdout, _ = await self._cluster.exec(trial.box, ["sh", "-c", decision.command],
                                                     timeout_s=decision.timeout_s)
        except ClusterError:
            return None
        return stdout if rc in (0, 1) else None  # diff exits 1 when there are differences

    async def _propose(self, trial: Trial, args: dict[str, Any]) -> dict[str, Any]:
        p, problems = proposal_mod.check(args.get("proposal"), trial.role, _binding(trial))
        real, problem = await self._diff_check(trial, p) if p is not None and not problems else (None, None)
        if problem:
            problems = [*problems, problem]
        elif real is not None and p is not None:
            p = p.model_copy(update={"diff": real})
        if p is None or problems:
            # The problems (schema paths, lint rule names) go into the record: a rejected final proposal is otherwise
            # only "no_valid_proposal" in the evidence (measured 2026-11-03 03:26 KST).
            trial.record("plan_propose", args, False, "proposal_rejected", result=";".join(map(str, problems))[:300])
            return {"blocked": "proposal_rejected", "accepted": False, "problems": problems}
        artifact = p.model_dump(mode="json")
        trial.artifact, trial.artifact_digest = artifact, proposal_mod.digest(artifact)
        trial.locked = True  # 07 3 "Trial 종료" 1: no new calls after this
        trial.phase = "proposed"
        trial.record("plan_propose", args, True, "ok")
        trial.proposed.set()
        return {"accepted": True, "artifact_digest": trial.artifact_digest}


def build_server(layer: ToolLayer) -> Server[Any]:
    async def on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        trial = _trial.get()
        allowed = set(TOOL_DEFS) if trial is None else {t for t in TOOL_DEFS if not trial.allowlist.tool_denied(t)}
        allowed -= HIDDEN_FROM_MODEL
        return types.ListToolsResult(tools=[
            types.Tool(name=n, description=d, input_schema=s,
                       annotations=types.ToolAnnotations(read_only_hint=ro, open_world_hint=False))
            for n, (d, s, ro) in TOOL_DEFS.items() if n in allowed
        ])

    async def on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        trial = _trial.get()
        if trial is None:
            raise MCPError(code=types.INTERNAL_ERROR, message="unauthenticated")
        if params.name not in TOOL_DEFS:
            trial.record(str(params.name), params.arguments or {}, False, "tool_unknown")
            return _clip({"blocked": "tool_unknown"})
        if not trial.running():
            trial.record(params.name, params.arguments or {}, False, "trial_locked")
            return _clip({"blocked": "trial_locked"})
        return _clip(await layer.call(trial, params.name, dict(params.arguments or {})))

    return Server("bravo-tool-layer", version="0.1.0",
                  instructions="Platform tool layer for one rehearsal Trial. Structured operations only.",
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool)


class TrialGuard:
    """ASGI endpoint for ``/mcp``: Trial token → running Trial → MCP handler."""

    def __init__(self, store: TrialStore, key: Callable[[], bytes | None], manager: StreamableHTTPSessionManager) -> None:
        self._store = store
        self._key = key
        self._manager = manager

    async def _deny(self, send: Send, status: int, reason: str) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"www-authenticate", f'Bearer error="{reason}"'.encode())]})
        await send({"type": "http.response.body", "body": json.dumps({"error": reason}).encode()})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        key = self._key()
        if key is None:
            return await self._deny(send, 503, "token_key_unconfigured")
        if not auth.startswith("Bearer "):
            return await self._deny(send, 401, "token_missing")
        try:
            claims = trial_token.verify(key, auth[7:].strip())
        except trial_token.TokenError as exc:
            return await self._deny(send, 401, exc.reason)
        trial = self._store.get(claims.trial_id)
        if trial is None or trial.tenant_id != claims.tenant_id or trial.plan_id != claims.plan_id \
                or trial.attempt != claims.attempt or (trial.names and trial.names.rehearsal_ns != claims.namespace):
            return await self._deny(send, 403, "token_trial_mismatch")
        if trial.phase != "agent_running":
            return await self._deny(send, 403, "trial_not_running")
        ctx = _trial.set(trial)
        try:
            await self._manager.handle_request(scope, receive, send)
        finally:
            _trial.reset(ctx)
