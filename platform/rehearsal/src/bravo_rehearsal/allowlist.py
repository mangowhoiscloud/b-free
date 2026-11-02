"""Per-role allowlists, enforced server-side by the tool layer (deny by default).

The files are ``platform/agent/allowlists/<role>.yaml`` and the loader is
``bravo_agents.load_allowlist`` (``platform/agent``), so the worker and the tool layer read the
same file with the same validation. This module only adapts it for enforcement:

- ``tools``: the role's ``mcp_tools``, plus ``plan_propose``, which the loader never grants to the
  model and which only the worker calls after its own validation (13 6 "출력").
- ``ops`` / ``paths`` / ``write_max_bytes`` / ``check_argv``: the sandbox side of ``rehearsal_exec``.
- ``kubectl_verbs`` / ``kinds``: always empty; the loader refuses any kubectl grant (13 4).
- ``db_queries``: the fixed catalog queries ``db_inspect`` may run.

Fail closed: a missing directory or file yields ``role_allowlist_missing``; unreadable YAML, a
schema violation, a role mismatch or a missing ``bravo_agents`` package yields
``role_allowlist_invalid``. Either way every tool call is denied with that reason.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROLES = frozenset({"discovery", "db", "governance", "app_migration", "ci", "report"})
WORKER_ONLY_TOOLS = frozenset({"plan_propose"})
ENV_DIRS = ("BRAVO_AGENT_ALLOWLISTS_DIR", "BRAVO_AGENT_ALLOWLIST_DIR")  # ours, then bravo_agents' name


def default_dir() -> Path:
    for name in ENV_DIRS:
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value)
    # src/bravo_rehearsal/allowlist.py -> platform/ -> platform/agent/allowlists
    return Path(__file__).resolve().parents[3] / "agent" / "allowlists"


@dataclass(frozen=True, slots=True)
class Allowlist:
    role: str
    spec: Any = None  # bravo_agents.allowlist.Allowlist when valid
    problem: str | None = None  # set when the file could not be used: everything is denied

    @property
    def tools(self) -> frozenset[str]:
        return frozenset(self.spec.mcp_tools) | WORKER_ONLY_TOOLS if self.spec is not None else frozenset()

    @property
    def ops(self) -> frozenset[str]:
        return frozenset(self.spec.sandbox.ops) if self.spec is not None else frozenset()

    @property
    def kubectl_verbs(self) -> frozenset[str]:
        return frozenset(self.spec.sandbox.kubectl.verbs) if self.spec is not None else frozenset()

    @property
    def kinds(self) -> frozenset[tuple[str, str]]:
        if self.spec is None:
            return frozenset()
        return frozenset(tuple(k.rsplit("/", 1)) for k in self.spec.sandbox.kubectl.kinds)  # type: ignore[misc]

    @property
    def checks(self) -> tuple[str, ...]:
        return tuple(self.spec.sandbox.checks) if self.spec is not None else ()

    @property
    def db_queries(self) -> frozenset[str]:
        return frozenset(self.spec.db_queries) if self.spec is not None else frozenset()

    @property
    def model_tier(self) -> str:
        return str(self.spec.model_tier) if self.spec is not None else "opus"

    @property
    def states(self) -> frozenset[str]:
        return frozenset(self.spec.states) if self.spec is not None else frozenset()

    def tool_denied(self, tool: str) -> str | None:
        if self.problem:
            return self.problem
        return None if tool in self.tools else "tool_not_in_role_allowlist"

    def op_denied(self, op: object) -> str | None:
        if self.problem:
            return self.problem
        if not isinstance(op, dict):
            return None  # free text: the tool layer names the rule (free_text_command)
        name = op.get("op")
        if name == "kubectl":
            return "kubectl_not_allowed"  # 13 4: no role gets kubectl; fixed templates apply workloads
        if name not in self.ops:
            return "op_not_in_role_allowlist"
        return None

    def path_denied(self, path: object, write: bool) -> tuple[str | None, str]:
        """(rule, normalized path). Paths must be absolute and under the role's read/write roots."""
        if self.spec is None:
            return self.problem or "role_allowlist_missing", ""
        if not isinstance(path, str) or not path.startswith("/") or "\0" in path or "\n" in path:
            return "path_outside_workspace", ""
        norm = posixpath.normpath(path)
        roots = self.spec.sandbox.paths.write if write else self.spec.sandbox.paths.read
        if not any(norm == r or norm.startswith(r + "/") for r in roots):
            return "path_outside_workspace", ""
        return None, norm

    @property
    def write_max_bytes(self) -> int:
        return int(self.spec.sandbox.write_max_bytes) if self.spec is not None else 0

    def check(self, name: object, args: object) -> tuple[str | None, list[str], Any]:
        """(rule, argv, check spec) for ``run_check`` via ``bravo_agents``' reference ``check_argv``."""
        if self.spec is None:
            return self.problem or "role_allowlist_missing", [], None
        if not isinstance(name, str) or name not in self.spec.sandbox.checks:
            return "check_not_allowed", [], None
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return "bad_check_args", [], None
        try:
            argv = self.spec.check_argv(name, args)
        except ValueError as exc:
            return str(exc) or "bad_check_args", [], None
        return None, argv, self.spec.sandbox.checks[name]


def load(role: str, directory: Path | None = None) -> Allowlist:
    if role not in ROLES:
        return Allowlist(role, problem="role_unknown")
    folder = directory or default_dir()
    if not (folder / f"{role}.yaml").is_file():
        return Allowlist(role, problem="role_allowlist_missing")
    try:
        from bravo_agents import load_allowlist
        from bravo_agents.errors import AgentRunError
    except ImportError:
        return Allowlist(role, problem="role_allowlist_invalid")
    try:
        spec = load_allowlist(role, folder)
    except (AgentRunError, OSError):
        return Allowlist(role, problem="role_allowlist_invalid")
    return Allowlist(role, spec=spec)
