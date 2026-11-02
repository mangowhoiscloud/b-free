"""codex_chatgpt 경로(파일럿 · 데모 전용, 팀장 결정 2026-11-02 "일단 OAI SDK + 내 구독 계정 사용해").

OpenAI Codex Python SDK(openai-codex==0.157.0)로 운영자 기기의 기존 ChatGPT 로그인을 씁니다.
- 자격은 Codex 자체 저장소(운영자 기기)에 있습니다. 이 모듈은 그 파일을 열거나 읽거나 옮기지 않습니다.
  로그인 여부는 app-server의 account/read로만 확인합니다(없으면 codex_login_missing).
- 모델의 도구는 플랫폼 MCP 도구 계층 하나뿐입니다. Codex 내장 도구(shell, apply_patch, 웹 검색, 이미지 보기,
  플러그인 · 앱 · 스킬 · 메모리 · 하위 에이전트 등)는 설정 덮어쓰기로 끕니다(ISOLATION_OVERRIDES).
- 실행 전 격리 사전 검사(preflight)가 같은 설정으로 모델에게 갈 도구 목록을 로컬 가짜 공급자로 받아 확인합니다.
  플랫폼 MCP의 승인된 도구 말고 하나라도 있으면 codex_isolation_failed로 멈춥니다(fail-closed).
- cwd는 실행마다 새 빈 임시 디렉터리, 샌드박스는 read_only, 승인 정책은 never(deny_all), 스레드는 ephemeral.
ChatGPT 플랜은 개인 구독입니다. 고객 서비스에 쓰는 운영 경로가 아닙니다(13-agents.md, D-26 차이).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from bravo_agents.errors import AgentRunError

PROVIDER = "codex_chatgpt"
MODEL_ENV = "BRAVO_CODEX_MODEL"
EFFORT_ENV = "BRAVO_CODEX_EFFORT"          # 선택: minimal|low|medium|high
BIN_ENV = "BRAVO_CODEX_BIN"                # 선택: 기본은 SDK가 고정한 런타임(openai-codex-cli-bin==0.157.0)
MCP_SERVER = "platform"
TOKEN_ENV_FOR_CODEX = "BRAVO_TRIAL_TOKEN"  # MCP 헤더는 이 환경 변수에서 Codex가 직접 읽음(설정 문자열에 토큰 없음)

# 0.157.0 `codex features list`에서 확인한 키. 모델 도구 · 외부 맥락을 늘리는 것은 모두 끕니다.
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "unified_exec_tty", "shell_snapshot", "view_image",
    "web_search_request", "web_search_cached", "standalone_web_search",
    "apps", "plugins", "remote_plugin", "plugin_sharing", "skill_mcp_dependency_install", "skill_search",
    "tool_suggest", "browser_use", "browser_use_external", "browser_use_full_cdp_access", "in_app_browser",
    "computer_use", "image_generation", "multi_agent", "multi_agent_v2", "memories", "hooks", "goals",
    "sleep_tool", "code_mode", "code_mode_host", "request_permissions_tool", "workspace_dependencies",
    "worktrees", "realtime_conversation", "guardian_approval", "tool_call_mcp_elicitation",
)

# 모델에게 보이는 내장 도구가 이 이름이면 실패. preflight는 허용 목록(플랫폼 MCP)만 통과시키므로 참고용입니다.
BUILTIN_TOOL_HINTS = ("shell", "exec_command", "write_stdin", "apply_patch", "web_search", "view_image",
                      "update_plan", "request_user_input", "spawn_agent", "read_file", "list_dir", "grep_files")
# 실행 기록에서 이 항목 형식이 나오면 내장 도구가 실행된 것입니다.
FORBIDDEN_ITEM_TYPES = frozenset({"commandExecution", "fileChange", "webSearch", "imageView", "imageGeneration",
                                  "collabAgentToolCall", "dynamicToolCall", "browserUse", "computerUse"})
ALLOWED_ITEM_TYPES = frozenset({"userMessage", "agentMessage", "reasoning", "mcpToolCall", "plan",
                                "contextCompaction", "hookPrompt"})


@dataclass(frozen=True)
class CodexRoute:
    provider: str
    model: str
    effort: str | None = None
    codex_bin: str | None = None


def resolve_codex(environ: Mapping[str, str] | None = None) -> CodexRoute:
    env = os.environ if environ is None else environ
    model = env.get(MODEL_ENV, "").strip()
    if not model:
        raise AgentRunError("llm_unconfigured", f"{MODEL_ENV} not set")
    effort = env.get(EFFORT_ENV, "").strip() or None
    if effort and effort not in ("minimal", "low", "medium", "high"):
        raise AgentRunError("llm_unconfigured", f"{EFFORT_ENV} must be minimal|low|medium|high")
    codex_bin = env.get(BIN_ENV, "").strip() or None
    if codex_bin and not os.path.isfile(codex_bin):
        raise AgentRunError("llm_unconfigured", f"{BIN_ENV} is not a file")
    return CodexRoute(PROVIDER, model, effort, codex_bin)


def _toml(v: Any) -> str:
    return json.dumps(v)  # JSON 문자열 · 배열 · 불리언은 TOML 값으로도 유효


def isolation_overrides(mcp_url: str, granted: tuple[str, ...]) -> tuple[str, ...]:
    """app-server에 넘기는 `--config key=value`. 사용자 config.toml의 플러그인 · MCP · 알림을 덮어씁니다."""
    tools = ",".join(_toml(t) for t in granted)
    mcp = ("{" f"url={_toml(mcp_url)}, bearer_token_env_var={_toml(TOKEN_ENV_FOR_CODEX)}, "
           f"enabled_tools=[{tools}], default_tools_approval_mode=\"approve\", required=true, "
           "startup_timeout_sec=20, tool_timeout_sec=120}")
    return (
        *(f"features.{f}=false" for f in DISABLED_FEATURES),
        'web_search="disabled"',
        "tools.update_plan.enabled=false",
        "tools.experimental_request_user_input.enabled=false",
        "agents.enabled=false",
        f"mcp_servers={{{MCP_SERVER}={mcp}}}",     # 사용자 MCP 서버 표를 통째로 바꿈
        "plugins={}",
        "apps._default.enabled=false",
        "skills.include_instructions=false",
        "skills.bundled.enabled=false",
        "memories.use_memories=false",
        "memories.generate_memories=false",
        "notify=[]",
        'history.persistence="none"',
        "project_doc_max_bytes=0",
        "include_apps_instructions=false",
        "include_environment_context=false",
        "include_collaboration_mode_instructions=false",
        'sandbox_mode="read-only"',
        'approval_policy="never"',
        'shell_environment_policy.inherit="none"',
        "analytics.enabled=false",
        "feedback.enabled=false",
        "check_for_update_on_startup=false",
    )


# 상위 환경에서 새어 들어올 수 있는 자격을 빈 값으로 덮습니다(SDK는 os.environ을 복사한 뒤 이 값으로 갱신).
_SCRUB = ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
          "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK", "GOOGLE_APPLICATION_CREDENTIALS",
          "KUBECONFIG", "BRAVO_REHEARSAL_TOKEN_KEY", "VAULT_TOKEN", "BAO_TOKEN")


def codex_env(trial_token: str) -> dict[str, str]:
    env = {k: "" for k in _SCRUB}
    env[TOKEN_ENV_FOR_CODEX] = trial_token
    return env


def _flatten_tool_names(tools: list, prefix: str = "") -> list[str]:
    """Responses 도구 정의(이름공간 포함)를 `ns.name` 모양으로 폅니다."""
    names: list[str] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or t.get("type") or "?")
        inner = t.get("tools")
        if isinstance(inner, list):
            names.extend(_flatten_tool_names(inner, f"{prefix}{name}."))
        else:
            names.append(prefix + name)
    return names


def request_tools(body: dict) -> list:
    """요청의 도구 정의 전부: 최상위 `tools`와 responses-lite의 `additional_tools` 입력 항목."""
    tools = list(body.get("tools") or [])
    for item in body.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            tools.extend(item.get("tools") or [])
    return tools


# Codex가 MCP 서버마다 붙이는 리소스 조회 도구. 켜진 MCP 서버(platform 하나)에만 갑니다.
MCP_RESOURCE_TOOLS = frozenset({"functions.list_mcp_resources", "functions.list_mcp_resource_templates",
                                "functions.read_mcp_resource"})


def is_platform_tool(name: str, granted: tuple[str, ...]) -> bool:
    return any(name in (f"mcp__{MCP_SERVER}__{g}", f"mcp__{MCP_SERVER}.{g}", f"mcp__{MCP_SERVER}__.{g}")
               for g in granted)


class _ProbeHandler(BaseHTTPRequestHandler):
    captured: list[dict] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = {"_unparsed": True}
        # 인증 헤더는 읽지도 저장하지도 않습니다(가짜 공급자에는 자격이 붙지 않음). 도구 정의만 남깁니다.
        type(self).captured.append({"tools": request_tools(data), "model": data.get("model"),
                                    "parsed": "_unparsed" not in data})
        msg = b'{"error":{"message":"bravo isolation probe","type":"invalid_request_error"}}'
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def log_message(self, *a):  # 조용히
        return


@dataclass
class PreflightReport:
    tool_names: list[str]
    extra: list[str]
    missing: list[str]
    ok: bool
    overrides: tuple[str, ...] = field(default=())


async def isolation_preflight(route: CodexRoute, mcp_url: str, trial_token: str, granted: tuple[str, ...],
                              developer_instructions: str = "probe", timeout_s: float = 60) -> PreflightReport:
    """같은 격리 설정 + 로컬 가짜 공급자(인증 없음)로 한 턴을 보내 모델에게 갈 도구 목록을 받습니다.

    가짜 공급자는 requires_openai_auth=false라 ChatGPT 자격이 붙지 않습니다. 400으로 답해 턴은 실패합니다.
    """
    from openai_codex import AsyncCodex, CodexConfig
    from openai_codex._approval_mode import ApprovalMode
    from openai_codex._sandbox import Sandbox

    class Handler(_ProbeHandler):
        captured: list[dict] = []

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    cwd = tempfile.mkdtemp(prefix="bravo-codex-probe-")
    cat_dir = tempfile.mkdtemp(prefix="bravo-codex-cat-")   # cwd 밖
    probe = (
        'model_provider="bravo_probe"',
        "features.enable_request_compression=false",   # 요청 본문을 그대로 받아 도구 목록을 읽기 위함
        "model_providers.bravo_probe={name=\"bravo-probe\", base_url=\"http://127.0.0.1:%d/v1\", "
        "wire_api=\"responses\", requires_openai_auth=false, request_max_retries=0, stream_max_retries=0}" % port,
    )
    base = run_overrides(route, mcp_url, granted, cat_dir)
    overrides = base + probe
    try:
        cfg = CodexConfig(codex_bin=route.codex_bin, config_overrides=overrides, cwd=cwd,
                          env=codex_env(trial_token))
        async with asyncio.timeout(timeout_s):
            async with AsyncCodex(config=cfg) as codex:
                thread = await codex.thread_start(approval_mode=ApprovalMode.deny_all, cwd=cwd, ephemeral=True,
                                                  model=route.model, sandbox=Sandbox.read_only,
                                                  developer_instructions=developer_instructions)
                try:
                    await thread.run("isolation probe", sandbox=Sandbox.read_only)
                except Exception:  # noqa: BLE001 - 400을 받아 실패하는 것이 정상
                    pass
    finally:
        srv.shutdown()
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(cat_dir, ignore_errors=True)
    if not Handler.captured or not Handler.captured[0]["parsed"]:
        raise AgentRunError("codex_isolation_failed", "probe received no readable model request")
    names = sorted(set(_flatten_tool_names(Handler.captured[0]["tools"])))
    extra = [n for n in names if not is_platform_tool(n, granted) and n not in MCP_RESOURCE_TOOLS]
    missing = [g for g in granted if not any(is_platform_tool(n, (g,)) for n in names)]
    return PreflightReport(names, extra, missing, ok=not extra and not missing, overrides=base)


def item_dict(item: Any) -> dict:
    root = getattr(item, "root", item)
    try:
        return root.model_dump(mode="json", by_alias=True)
    except AttributeError:
        return dict(root) if isinstance(root, dict) else {"type": "unknown", "repr": repr(root)[:500]}


def check_items(items: list[dict], granted: tuple[str, ...]) -> list[str]:
    """실행 기록 검사. 플랫폼 MCP의 승인된 도구 말고 실행된 도구가 있으면 그 목록을 돌려줍니다."""
    bad: list[str] = []
    for it in items:
        kind = it.get("type")
        if kind == "mcpToolCall":
            if it.get("server") != MCP_SERVER or it.get("tool") not in granted:
                bad.append(f"mcp:{it.get('server')}/{it.get('tool')}")
        elif kind in FORBIDDEN_ITEM_TYPES or kind not in ALLOWED_ITEM_TYPES:
            bad.append(str(kind))
    return bad


@dataclass
class CodexTurn:
    final_response: str | None
    items: list[dict]
    usage: dict | None
    thread_id: str
    status: str
    error: str | None
    account_plan: str | None


async def check_login(codex: Any) -> str:
    """account/read로 ChatGPT 로그인만 확인합니다. 자격 파일은 열지 않습니다."""
    try:
        acct = await codex.account()
    except Exception as e:  # noqa: BLE001
        raise AgentRunError("codex_login_missing", f"account/read failed: {type(e).__name__}") from e
    root = getattr(acct.account, "root", None)
    if root is None or getattr(root, "type", None) != "chatgpt":
        raise AgentRunError("codex_login_missing", "Codex is not logged in with ChatGPT (run `codex login`)")
    plan = getattr(root, "plan_type", None)
    return str(getattr(plan, "value", plan))


async def run_codex_turn(route: CodexRoute, mcp_url: str, trial_token: str, granted: tuple[str, ...],
                         developer_instructions: str, prompt: str, output_schema: dict | None,
                         max_tool_calls: int, timeout_s: float) -> CodexTurn:
    from openai_codex import AsyncCodex, CodexConfig
    from openai_codex._approval_mode import ApprovalMode
    from openai_codex._run import _collect_async_turn_result
    from openai_codex._sandbox import Sandbox

    cwd = tempfile.mkdtemp(prefix="bravo-codex-")  # 실행마다 새 빈 디렉터리
    cat_dir = tempfile.mkdtemp(prefix="bravo-codex-cat-")
    try:
        base = run_overrides(route, mcp_url, granted, cat_dir)
    except BaseException:
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(cat_dir, ignore_errors=True)
        raise
    cfg = CodexConfig(codex_bin=route.codex_bin, config_overrides=base, cwd=cwd, env=codex_env(trial_token))
    calls = 0
    try:
        async with asyncio.timeout(timeout_s):
            async with AsyncCodex(config=cfg) as codex:
                plan = await check_login(codex)
                thread = await codex.thread_start(approval_mode=ApprovalMode.deny_all, cwd=cwd, ephemeral=True,
                                                  model=route.model, sandbox=Sandbox.read_only,
                                                  developer_instructions=developer_instructions)
                handle = await thread.turn(prompt, output_schema=output_schema, sandbox=Sandbox.read_only,
                                           effort=route.effort)

                async def guarded():
                    nonlocal calls
                    async for ev in handle.stream():
                        if ev.method == "item/started":
                            d = item_dict(getattr(ev.payload, "item", None))
                            if d.get("type") in FORBIDDEN_ITEM_TYPES:
                                await handle.interrupt()
                                raise AgentRunError("unexpected_tools", str(d.get("type")))
                            if d.get("type") == "mcpToolCall":
                                calls += 1
                                if calls > max_tool_calls:
                                    await handle.interrupt()
                                    raise AgentRunError("max_turns", f"{calls} tool calls")
                        yield ev

                try:
                    result = await _collect_async_turn_result(guarded(), turn_id=handle.id)
                except AgentRunError:
                    raise
                except Exception as e:  # noqa: BLE001 - 실패한 턴
                    raise AgentRunError("sdk_error", f"{type(e).__name__}: {str(e)[:300]}") from e
    except TimeoutError as e:
        raise AgentRunError("timeout", f"{timeout_s}s") from e
    except FileNotFoundError as e:
        raise AgentRunError("sdk_unavailable", str(e)[:300]) from e
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(cat_dir, ignore_errors=True)
    items = [item_dict(i) for i in result.items]
    usage = result.usage.model_dump(mode="json") if result.usage is not None else None
    status = str(getattr(result.status, "value", result.status))
    err = result.error.model_dump_json() if result.error is not None else None
    return CodexTurn(result.final_response, items, usage, thread.id, status, err, plan)


def strict_schema(schema: dict) -> dict:
    """OpenAI strict 구조화 출력 규칙: 모든 객체의 속성을 required로. 기본값 있는 필드도 모델이 채웁니다(더 엄격)."""
    import copy

    s = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and isinstance(node.get("properties"), dict):
                node["required"] = list(node["properties"])
                node.setdefault("additionalProperties", False)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


def _codex_bin(route: CodexRoute) -> str:
    if route.codex_bin:
        return route.codex_bin
    from openai_codex import CodexConfig
    from openai_codex.client import _resolve_codex_bin

    return str(_resolve_codex_bin(CodexConfig()))


def foreign_mcp_overrides(route: CodexRoute, base: tuple[str, ...]) -> tuple[str, ...]:
    """사용자 · 시스템 설정 층의 MCP 서버는 `mcp_servers={...}` 덮어쓰기로 지워지지 않습니다(병합됨).

    같은 설정으로 `codex mcp list --json`을 돌려 platform 말고 모든 서버를 enabled=false로 끄고, 다시 확인합니다.
    """
    import subprocess

    def listing(ov: tuple[str, ...]) -> list[dict]:
        args = [_codex_bin(route)]
        for kv in ov:
            args += ["--config", kv]
        cwd = tempfile.mkdtemp(prefix="bravo-codex-mcpls-")
        try:
            out = subprocess.run(args + ["mcp", "list", "--json"], capture_output=True, text=True, timeout=30,
                                 cwd=cwd, check=True).stdout
        except (subprocess.SubprocessError, OSError) as e:
            raise AgentRunError("codex_isolation_failed", f"mcp list failed: {type(e).__name__}") from e
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        return json.loads(out)

    def stub(s: dict) -> str:  # 다른 층의 서버를 꺼진 가짜 전송으로 덮음(설정 층마다 전송 검증이 있어 enabled만으로는 안 됨)
        t = (s.get("transport") or {}).get("type")
        body = '{command="/usr/bin/false", enabled=false}' if t == "stdio" else \
            '{url="http://127.0.0.1:9/disabled", enabled=false}'
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s["name"]):   # -c 경로는 따옴표 키를 풀지 않음
            raise AgentRunError("codex_isolation_failed", f"cannot override MCP server name {s['name']!r}")
        return f"mcp_servers.{s['name']}={body}"

    extra = tuple(stub(s) for s in listing(base) if s.get("name") != MCP_SERVER and s.get("enabled"))
    still = [s["name"] for s in listing(base + extra) if s.get("name") != MCP_SERVER and s.get("enabled")]
    if still:
        raise AgentRunError("codex_isolation_failed", f"foreign MCP servers still enabled: {still}")
    return extra


# 모델 카탈로그가 켜는 내장 도구. 설정 키로는 꺼지지 않아(실측: code mode `exec`/`wait`, `apply_patch`가 남음)
# 실행마다 카탈로그 사본을 만들어 이 모델 항목만 바꾸고 `model_catalog_json`으로 넘깁니다.
CATALOG_PATCH = {
    "tool_mode": "direct",              # code_mode_only면 MCP 도구가 `exec`(JS) 안으로만 들어감
    "shell_type": "disabled",
    "apply_patch_tool_type": None,
    "experimental_supported_tools": [],
    "supports_search_tool": False,
    "node_repl_disabled": True,
}


def sanitized_catalog(route: CodexRoute, directory: str) -> str:
    """`codex debug models`(Codex가 자기 자격으로 받은 공개 카탈로그)에서 이 모델 항목을 고쳐 파일로 씁니다."""
    import subprocess

    cwd = tempfile.mkdtemp(prefix="bravo-codex-models-")
    try:
        out = subprocess.run([_codex_bin(route), "debug", "models"], capture_output=True, text=True, timeout=60,
                             cwd=cwd, check=True).stdout
        catalog = json.loads(out)
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        raise AgentRunError("codex_isolation_failed", f"model catalog unavailable: {type(e).__name__}") from e
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    models = [m for m in catalog.get("models", []) if m.get("slug") == route.model]
    if not models:
        raise AgentRunError("llm_unconfigured", f"{MODEL_ENV}={route.model} not in the Codex model catalog")
    m = dict(models[0])
    m.update(CATALOG_PATCH)
    path = os.path.join(directory, "models.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"models": [m]}, f)
    return path


def run_overrides(route: CodexRoute, mcp_url: str, granted: tuple[str, ...], directory: str) -> tuple[str, ...]:
    """격리 덮어쓰기 전체: 설정 키 + 다른 층 MCP 서버 끄기 + 고친 모델 카탈로그."""
    base = isolation_overrides(mcp_url, granted)
    base = base + foreign_mcp_overrides(route, base)
    return base + (f"model_catalog_json={_toml(sanitized_catalog(route, directory))}",)
