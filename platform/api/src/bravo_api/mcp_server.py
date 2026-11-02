"""Operator MCP server (D-18, docs/architecture/08-operator-mcp.md 4-5, pilot scope).

Official ``mcp`` SDK low-level server, Streamable HTTP, ``stateless_http=True`` at ``/mcp``.
Seven tools wrap the same handlers as REST. ``rehearsal_start`` asks for one isolated
rehearsal attempt (S2); it cannot approve, deploy or start a move. No approval or shift tool exists.

Layered errors: no/invalid JWT -> HTTP 401; role cannot call the tool -> HTTP 403 with
``insufficient_scope`` (checked on the raw JSON-RPC body before the SDK runs, and again in
the handler); business rejection -> tool result ``isError: true`` + reason code;
malformed arguments -> JSON-RPC error. ``tools/list`` shows only the caller's tools.
"""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.types import Message, Receive, Scope, Send

from .auth import AccessNotConnectedError, Action, Channel, CredentialSource, Principal, Verifier, audience_for, is_allowed
from .config import Settings
from .db import Database
from .errors import AuthError, BusinessError, ForbiddenError, NotFoundError
from .models import EnvRegister, PlanCreate, RehearsalStart
from .rehearsal import RehearsalService
from .service import Services

LOG = logging.getLogger("bravo_api.mcp")
TRACER = trace.get_tracer("bravo_api.mcp")
MAX_BODY = 1 << 20
_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("bravo_principal", default=None)

# Argument models ------------------------------------------------------------------------

_KEY = Field(
    min_length=8,
    max_length=200,
    pattern=r"^[A-Za-z0-9._:-]+$",
    description="Idempotency key. Same key + same input returns the first result.",
)


class EnvRegisterArgs(EnvRegister):
    idempotency_key: str = _KEY


class EnvControlsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment_id: UUID


class PlanCreateArgs(PlanCreate):
    idempotency_key: str = _KEY


class PlanGetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: UUID


class PlanEventsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: UUID
    after: int = Field(default=0, ge=0, description="Return events with id greater than this.")
    limit: int = Field(default=50, ge=1, le=200)


class RehearsalStartArgs(RehearsalStart):
    plan_id: UUID
    idempotency_key: str = _KEY


class GateRunArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: UUID
    idempotency_key: str = _KEY


# Output schemas (structuredContent). Server-generated values only. ----------------------

_ERR = {"type": ["string", "null"]}
_STR = {"type": "string"}
_NSTR = {"type": ["string", "null"]}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


OUT_ENV = _obj(
    {"environment_id": _STR, "name": _STR, "role": _STR, "engine": _STR, "secret_status": _STR, "secret_reason": _ERR},
    ["environment_id", "role"],
)
OUT_CONTROLS = _obj(
    {
        "environment_id": _STR,
        "role": _STR,
        "status": _STR,
        "reason": _ERR,
        "collected_at": _STR,
        "controls": {"type": "array", "items": _obj({"control_id": _STR, "result": _STR}, ["control_id", "result"])},
    },
    ["environment_id", "status", "controls"],
)
OUT_PLAN_CREATE = _obj(
    {
        "plan_id": _STR,
        "state": _STR,
        "state_reason": _NSTR,
        "revision": {"type": "integer"},
        "rehearsal": _STR,
        "collect": {"type": "object"},
        "console_url": _STR,
    },
    ["plan_id", "state"],
)
OUT_PLAN = _obj(
    {
        "plan_id": _STR,
        "state": _STR,
        "state_reason": _NSTR,
        "revision": {"type": "integer"},
        "last_gate": {"type": ["object", "null"]},
        "awaiting_approval": {"type": ["object", "null"]},
        "run": {"type": ["object", "null"]},
        "last_run": {"type": ["object", "null"]},
        "last_receipt": {"type": ["object", "null"]},
        "console_url": _STR,
    },
    ["plan_id", "state"],
)
OUT_EVENTS = _obj(
    {"plan_id": _STR, "events": {"type": "array", "items": {"type": "object"}}, "next_after": _STR},
    ["plan_id", "events", "next_after"],
)
OUT_GATE = _obj(
    {
        "plan_id": _STR,
        "verdict_id": _STR,
        "verdict": {"enum": ["PASS", "BLOCK"]},
        "reasons": {"type": "array", "items": _STR},
        "controls": {"type": "array"},
        "state": _STR,
        "input_digest": _STR,
    },
    ["plan_id", "verdict_id", "verdict", "reasons", "state"],
)

OUT_REHEARSAL = _obj(
    {
        "plan_id": _STR,
        "rehearsal_id": _STR,
        "attempt": {"type": "integer"},
        "task": _STR,
        "role": _STR,
        "trial_id": _NSTR,
        "status": _STR,
        "state": _STR,
        "reason": _ERR,
    },
    ["plan_id", "rehearsal_id", "attempt", "status", "state"],
)

_RO = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
_WR = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


@dataclass(frozen=True, slots=True)
class Handlers:
    services: Services
    rehearsals: RehearsalService


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    title: str
    description: str
    action: Action
    args: type[BaseModel]
    output_schema: dict[str, Any]
    annotations: types.ToolAnnotations
    call: Callable[[Handlers, Principal, Any], Awaitable[dict[str, Any]]]

    def to_tool(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            title=self.title,
            description=self.description,
            input_schema=self.args.model_json_schema(),
            output_schema=self.output_schema,
            annotations=self.annotations,
        )


M = Channel.MCP
TOOLS: dict[str, ToolSpec] = {
    t.name: t
    for t in (
        ToolSpec(
            "env_register",
            "Register an environment",
            "Register a database environment: where the service runs today (role 'source') or where it "
            "should move (role 'target'). Give a short name and the name of the stored login, which always "
            "starts with tenants/<your tenant>/. Never send a password.",
            Action.ENV_REGISTER,
            EnvRegisterArgs,
            OUT_ENV,
            _WR,
            lambda s, p, a: s.services.env_register(
                p, M, EnvRegister(**a.model_dump(exclude={"idempotency_key"})), a.idempotency_key
            ),
        ),
        ToolSpec(
            "env_controls",
            "Check an environment's safety controls",
            "For one environment, show each safety control (for example 'can anyone read the messages "
            "without logging in?') as PASS, FAIL or UNKNOWN, and when it was checked.",
            Action.ENV_READ,
            EnvControlsArgs,
            OUT_CONTROLS,
            _RO,
            lambda s, p, a: s.services.env_controls(p, M, a.environment_id),
        ),
        ToolSpec(
            "plan_create",
            "Create a move plan",
            "Create a plan to move a service from a source environment to a target environment. The "
            "platform reads (never changes) both sides' safety controls, copy progress and dependency "
            "inventory, then says whether the safety check or a rehearsal comes next.",
            Action.PLAN_CREATE,
            PlanCreateArgs,
            OUT_PLAN_CREATE,
            _WR,
            lambda s, p, a: s.services.plan_create(
                p, M, PlanCreate(**a.model_dump(exclude={"idempotency_key"})), a.idempotency_key
            ),
        ),
        ToolSpec(
            "plan_get",
            "Show a plan",
            "Where the plan stands: current step, the last safety check result, whether it waits for a "
            "person to approve in the console (with the link), the move in progress and its integrity "
            "receipt.",
            Action.PLAN_READ,
            PlanGetArgs,
            OUT_PLAN,
            _RO,
            lambda s, p, a: s.services.plan_get(p, M, a.plan_id),
        ),
        ToolSpec(
            "plan_events",
            "List what happened",
            "The plan's history, oldest first: up to `limit` entries after entry number `after`. "
            "The console shows the same entries live.",
            Action.PLAN_READ,
            PlanEventsArgs,
            OUT_EVENTS,
            _RO,
            lambda s, p, a: s.services.plan_events(p, M, a.plan_id, a.after, a.limit),
        ),
        ToolSpec(
            "gate_run",
            "Run the safety check",
            "Check that the target keeps every safety control the source has and that the copy is up to "
            "date. The same inputs always give the same result. BLOCK lists what to fix; PASS asks a "
            "person to approve the move in the console. This tool cannot approve or start the move.",
            Action.GATE_RUN,
            GateRunArgs,
            OUT_GATE,
            _WR,
            lambda s, p, a: s.services.gate_run(p, M, a.plan_id, a.idempotency_key),
        ),
        ToolSpec(
            "rehearsal_start",
            "Rehearse a fix in isolation",
            "Ask the platform's AI agent to propose a fix for the plan's open gaps and try it in an isolated "
            "rehearsal space on the target cluster. Hidden tests decide the result; the agent cannot see "
            "them, touch production or approve anything. Allowed only before the move starts. After three "
            "failed attempts a person takes over in the console. Returns the attempt number and status.",
            Action.REHEARSAL_START,
            RehearsalStartArgs,
            OUT_REHEARSAL,
            _WR,
            lambda s, p, a: s.rehearsals.start(p, M, a.plan_id, RehearsalStart(task=a.task, role=a.role), a.idempotency_key),
        ),
    )
}


def tools_for(principal: Principal) -> list[types.Tool]:
    return [t.to_tool() for t in TOOLS.values() if is_allowed(principal, t.action, Channel.MCP)]


def _result(payload: dict[str, Any], is_error: bool = False) -> types.CallToolResult:
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], structured_content=payload, is_error=is_error
    )


def current_principal(ctx: Any) -> Principal:
    p = _principal.get()
    if p is None:
        request = getattr(ctx, "request", None)
        state = getattr(request, "scope", {}).get("state", {}) if request is not None else {}
        p = state.get("bravo_principal") if isinstance(state, dict) else None
    if p is None:  # the ASGI guard always sets it; reaching here is a wiring bug
        raise MCPError(code=types.INTERNAL_ERROR, message="unauthenticated")
    return p


def build_server(services: Services, rehearsals: RehearsalService) -> Server[Any]:
    handlers = Handlers(services, rehearsals)

    async def on_list_tools(ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools_for(current_principal(ctx)))

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        principal = current_principal(ctx)
        spec = TOOLS.get(params.name)
        if spec is None:
            raise MCPError(code=types.INVALID_PARAMS, message=f"unknown tool {params.name}")
        with TRACER.start_as_current_span(f"tools/call {spec.name}") as span:
            span.set_attribute("mcp.method.name", "tools/call")
            span.set_attribute("gen_ai.tool.name", spec.name)
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("bravo.tenant_id", principal.tenant_id)
            if not is_allowed(principal, spec.action, Channel.MCP):  # defence in depth; guard returns 403 first
                span.set_attribute("error.type", "insufficient_scope")
                return _result({"error": "insufficient_scope"}, is_error=True)
            try:
                args = spec.args.model_validate(params.arguments or {})
            except ValidationError as exc:
                span.set_attribute("error.type", "invalid_params")
                errs = [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
                raise MCPError(code=types.INVALID_PARAMS, message="invalid arguments", data=errs) from exc
            try:
                payload = await spec.call(handlers, principal, args)
            except (BusinessError, NotFoundError) as exc:
                span.set_attribute("error.type", exc.reason)
                span.set_status(Status(StatusCode.ERROR, exc.reason))
                body: dict[str, Any] = {"error": exc.reason}
                if isinstance(exc, BusinessError) and exc.detail:
                    body["detail"] = exc.detail
                return _result(body, is_error=True)
            except ForbiddenError as exc:
                span.set_attribute("error.type", exc.reason)
                return _result({"error": exc.reason}, is_error=True)
            return _result(payload)

    return Server(
        "bravo-operator-mcp",
        version="0.1.0",
        instructions=(
            "Bravo helps move a service's database to a new environment safely. With these tools you can "
            "register environments, create a move plan, run the safety check, ask for an isolated rehearsal "
            "of a fix and follow progress. A person approves and starts the move in the console; these "
            "tools cannot."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


class MCPGuard:
    """ASGI endpoint for ``/mcp``: Host/Origin checks, JWT (MCP AUD), principal, role pre-check."""

    def __init__(
        self, settings: Settings, db: Database, verifier: Verifier, manager: StreamableHTTPSessionManager
    ) -> None:
        self._settings = settings
        self._db = db
        self._verifier = verifier
        self._manager = manager

    async def _deny(
        self, send: Send, status: int, reason: str, www: str | None = None, detail: dict[str, Any] | None = None
    ) -> None:
        headers = [(b"content-type", b"application/json")]
        if www:
            headers.append((b"www-authenticate", www.encode()))
        body: dict[str, Any] = {"error": reason}
        if detail:
            body["detail"] = detail
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": json.dumps(body, ensure_ascii=False).encode()})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        raw = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]]
        headers = {k.lower(): v for k, v in raw}
        host = headers.get("host", "")
        if self._settings.mcp_allowed_hosts and host not in self._settings.mcp_allowed_hosts:
            return await self._deny(send, 403, "host_not_allowed")
        origin = headers.get("origin")
        if origin is not None and origin not in self._settings.mcp_allowed_origins:
            return await self._deny(send, 403, "origin_not_allowed")
        www_invalid = 'Bearer realm="bravo-operator-mcp", error="invalid_token"'
        audience = audience_for(scope.get("path", ""), self._settings.console_aud or "", self._settings.mcp_aud or "")
        try:
            token = await self._verifier.verify(_extract_bearer(raw), audience)
            principal = await self._db.resolve_principal(token, Channel.MCP, headers.get("x-bravo-tenant"))
        except AccessNotConnectedError as exc:
            return await self._deny(send, 503, exc.reason, detail={"message": exc.message})
        except AuthError as exc:
            return await self._deny(send, 401, exc.reason, www_invalid)
        except ForbiddenError as exc:
            return await self._deny(
                send, 403, exc.reason, 'Bearer realm="bravo-operator-mcp", error="insufficient_scope"', exc.detail
            )

        receive_fn = receive
        if scope["method"] == "POST":
            body, too_big = await _read_body(receive)
            if too_big:
                return await self._deny(send, 413, "body_too_large")
            denied = _forbidden_tool(body, principal)
            if denied is not None:
                scope_hint = " ".join(sorted(r.value for r in _roles_for(denied)))
                return await self._deny(
                    send,
                    403,
                    "insufficient_scope",
                    f'Bearer realm="bravo-operator-mcp", error="insufficient_scope", scope="{scope_hint}"',
                )
            receive_fn = _replay(body, receive)

        scope.setdefault("state", {})["bravo_principal"] = principal
        token_ctx = _principal.set(principal)
        try:
            await self._manager.handle_request(scope, receive_fn, send)
        finally:
            _principal.reset(token_ctx)


def _extract_bearer(raw: list[tuple[str, str]]) -> str:
    from .auth import extract_credential

    return extract_credential(raw, (CredentialSource.BEARER,))


def _roles_for(spec: ToolSpec) -> frozenset[Any]:
    from .auth import POLICY

    return POLICY[spec.action][1]


async def _read_body(receive: Receive) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY:
            return b"", True
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks), False


def _replay(body: bytes, receive: Receive) -> Receive:
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


def _forbidden_tool(body: bytes, principal: Principal) -> ToolSpec | None:
    """First tools/call in the body whose tool the principal may not call (HTTP 403)."""
    try:
        msg = json.loads(body)
    except ValueError:
        return None  # malformed JSON is a JSON-RPC error, produced by the SDK
    for item in msg if isinstance(msg, list) else [msg]:
        if not isinstance(item, dict) or item.get("method") != "tools/call":
            continue
        params = item.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        spec = TOOLS.get(name) if isinstance(name, str) else None
        if spec is not None and not is_allowed(principal, spec.action, Channel.MCP):
            return spec
    return None
