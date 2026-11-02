"""역할별 허용 목록(allowlists/<role>.yaml) 로더. 목록에 없는 것은 모두 거부합니다(기본 거부).

같은 파일을 두 곳이 읽습니다.
- 에이전트 워커(이 패키지): SDK에 줄 MCP 도구, 한도, 모델 등급, 프롬프트의 책임 경계
- 플랫폼 도구 계층(서버, 집행 주체): rehearsal_exec의 작업 · 경로 · 검사 argv · 바이너리. Pod 쪽 검사는 보안 경계가 아닙니다.
"""
import os
import posixpath
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bravo_agents.errors import AgentRunError

ROLES = ("discovery", "governance", "app_migration", "ci", "db", "report")
# 플랫폼 도구 계층이 내놓는 전체 도구. 역할마다 이 중 일부만 받습니다. 이름에 점을 쓰지 않습니다(모델 API 도구 이름 규칙).
PLATFORM_TOOLS = ("inventory_describe", "repo_read", "rehearsal_exec", "db_inspect", "db_rehearse",
                  "controls_describe", "policy_evaluate", "evidence_read", "plan_propose")
# plan_propose는 워커(결정적 코드)가 검증한 제안서를 서버에 낼 때만 씁니다. 모델에게는 주지 않습니다.
WORKER_ONLY_TOOLS = frozenset({"plan_propose"})
# D-26: 자격이 필요한 도구(db_inspect, db_rehearse, policy_evaluate 등)는 도구 계층이 서버 쪽에서 OpenBao의 자격으로
# 실행하고 결과만 돌려줍니다. 샌드박스 · 봉투 · 프롬프트 · 도구 결과에 비밀 값이 들어가지 않습니다.
# 실행 구간과 판정 · 승인 상태에는 어떤 에이전트도 배치하지 않습니다(§8.2, NFR-5).
FORBIDDEN_STATES = frozenset({"SHIFT", "CUTOVER", "FAILBACK", "SAFE_HOLD"})
STATES = frozenset({"COLLECT", "ANALYZE", "PROPOSE", "REHEARSE", "GATE", "AWAIT_APPROVAL", "WATCH",
                    "DONE", "DONE_ROLLED_BACK", "NEEDS_HUMAN"}) | FORBIDDEN_STATES
SANDBOX_OPS = ("read_file", "list_dir", "write_file", "run_check")
SANDBOX_ROOTS = ("/src", "/work", "/out", "/app")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
ALLOWLIST_DIR_ENV = "BRAVO_AGENT_ALLOWLIST_DIR"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Param(_Strict):
    kind: Literal["path", "pattern", "jq_filter"]
    under: tuple[str, ...] = ()          # kind=path: 이 루트 아래만
    max_len: int = Field(default=256, ge=1, le=2048)


class Check(_Strict):
    argv: tuple[str, ...] = Field(min_length=1)
    params: dict[str, Param] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = "/work"
    timeout_s: int = Field(default=60, ge=1, le=900)
    executes_agent_code: bool = False    # true면 샌드박스 안 임의 코드 실행을 인정하는 검사(문서와 위협 모델에 표시)

    @field_validator("env")
    @classmethod
    def _no_secret_env(cls, v):
        # D-26: 검사 환경 변수에 비밀을 두지 않습니다.
        bad = [k for k in v if re.search(r"(PASS|SECRET|TOKEN|KEY|CREDENTIAL|PGPASSFILE)", k)]
        if bad:
            raise ValueError(f"secret-like env not allowed in checks: {bad}")
        return v

    @model_validator(mode="after")
    def _placeholders_declared(self):
        used = {m for a in self.argv for m in _PLACEHOLDER.findall(a)}
        if used != set(self.params):
            raise ValueError(f"argv placeholders {sorted(used)} != params {sorted(self.params)}")
        return self


class Limits(_Strict):
    max_turns: int = Field(ge=2, le=60)
    max_budget_usd: float = Field(gt=0, le=10)
    timeout_s: int = Field(ge=30, le=1800)
    max_attempts: int = Field(default=3, ge=1, le=3)


class Responsibility(_Strict):
    does: tuple[str, ...] = Field(min_length=1)
    never: tuple[str, ...] = Field(min_length=1)


class Paths(_Strict):
    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()


class Kubectl(_Strict):
    verbs: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()


class Sandbox(_Strict):
    image: str = Field(pattern=r"^[a-z0-9./-]+$")
    user: int = Field(ge=1000)
    egress: tuple[Literal["llm-gateway", "tool-layer"], ...]
    binaries: tuple[str, ...] = Field(min_length=1)
    ops: tuple[Literal["read_file", "list_dir", "write_file", "run_check"], ...]
    paths: Paths
    write_max_bytes: int = Field(default=0, ge=0, le=1_048_576)
    checks: dict[str, Check] = Field(default_factory=dict)
    kubectl: Kubectl = Kubectl()


class Allowlist(_Strict):
    apiVersion: Literal["bravo.agent/v1"]
    kind: Literal["AgentAllowlist"]
    role: Literal[ROLES]  # type: ignore[valid-type]
    description: str
    responsibility: Responsibility
    states: tuple[str, ...] = Field(min_length=1)
    model_tier: Literal["opus", "haiku"]
    limits: Limits
    mcp_tools: tuple[str, ...] = Field(min_length=1)
    db_queries: tuple[str, ...] = ()
    sandbox: Sandbox

    @field_validator("states")
    @classmethod
    def _states(cls, v):
        bad = [s for s in v if s not in STATES or s in FORBIDDEN_STATES]
        if bad:
            raise ValueError(f"agents may not run in {bad}")
        return v

    @field_validator("mcp_tools")
    @classmethod
    def _tools(cls, v):
        unknown = sorted(set(v) - set(PLATFORM_TOOLS))
        if unknown:
            raise ValueError(f"unknown platform tools {unknown}")
        if set(v) & WORKER_ONLY_TOOLS:
            raise ValueError("plan_propose is submitted by the worker, never granted to the model")
        return v

    @model_validator(mode="after")
    def _consistent(self):
        sb = self.sandbox
        for root in sb.paths.read + sb.paths.write:
            if root not in SANDBOX_ROOTS:
                raise ValueError(f"path root {root} not in {SANDBOX_ROOTS}")
        if ("write_file" in sb.ops) != bool(sb.paths.write):
            raise ValueError("write_file op and write paths must come together")
        if "write_file" in sb.ops and sb.write_max_bytes == 0:
            raise ValueError("write_file needs write_max_bytes")
        if ("run_check" in sb.ops) != bool(sb.checks):
            raise ValueError("run_check op and checks must come together")
        if ("rehearsal_exec" in self.mcp_tools) != bool(sb.ops):
            raise ValueError("rehearsal_exec tool and sandbox ops must come together")
        if ("db_inspect" in self.mcp_tools) != bool(self.db_queries):
            raise ValueError("db_inspect tool and db_queries must come together")
        if sb.kubectl.verbs or sb.kubectl.kinds:
            raise ValueError("agents get no kubectl operations (fixed templates apply workloads)")
        for name, chk in sb.checks.items():
            if chk.argv[0] not in sb.binaries:
                raise ValueError(f"check {name}: {chk.argv[0]} not in sandbox binaries")
            for p in chk.params.values():
                if p.kind == "path" and (not p.under or not set(p.under) <= set(sb.paths.read)):
                    raise ValueError(f"check {name}: path param must be under readable roots")
        return self

    def check_argv(self, name: str, args: dict[str, str]) -> list[str]:
        """검사 이름과 인자로 실행할 argv를 만듭니다. 도구 계층이 쓰는 참조 구현입니다."""
        chk = self.sandbox.checks.get(name)
        if chk is None:
            raise ValueError("check_not_allowed")
        if set(args) != set(chk.params):
            raise ValueError("bad_check_args")
        values = {}
        for key, p in chk.params.items():
            v = args[key]
            if not isinstance(v, str) or not v or len(v) > p.max_len or "\0" in v or "\n" in v:
                raise ValueError("bad_check_args")
            if p.kind == "path":
                norm = posixpath.normpath(v)
                if not v.startswith("/") or not any(norm == r or norm.startswith(r + "/") for r in p.under):
                    raise ValueError("path_outside_workspace")
                v = norm
            elif v.startswith("-"):
                raise ValueError("bad_check_args")  # 플래그 주입 방지
            values[key] = v
        return [_PLACEHOLDER.sub(lambda m: values[m.group(1)], a) for a in chk.argv]


def allowlist_dir() -> Path:
    env = os.environ.get(ALLOWLIST_DIR_ENV)
    return Path(env) if env else Path(__file__).resolve().parents[2] / "allowlists"


@lru_cache(maxsize=None)
def _load(path: str) -> Allowlist:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Allowlist.model_validate(data)


def load_allowlist(role: str, directory: Path | None = None) -> Allowlist:
    if role not in ROLES:
        raise AgentRunError("allowlist_invalid", f"unknown role {role}")
    path = (directory or allowlist_dir()) / f"{role}.yaml"
    try:
        al = _load(str(path))
    except FileNotFoundError as e:
        raise AgentRunError("allowlist_invalid", f"missing {path}") from e
    except ValueError as e:
        raise AgentRunError("allowlist_invalid", str(e)) from e
    if al.role != role:
        raise AgentRunError("allowlist_invalid", f"{path} declares role {al.role}")
    return al
