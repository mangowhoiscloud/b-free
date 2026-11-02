"""REST ``/v1/*`` for the console (CONTRACT.md 7, platform/api/README.md 8.9).

Authentication: one Access JWT with the console AUD, from ``Cf-Access-Jwt-Assertion`` or
``Authorization: Bearer``. Errors: 401 (credential), 403 (role/channel), 404, 409/422
(business reason codes), 400 (malformed).
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from .auth import (
    AccessNotConnectedError,
    Action,
    Channel,
    CredentialSource,
    Principal,
    Verifier,
    audience_for,
    authorize,
    extract_credential,
)
from .changes import ChangeService
from .config import Settings
from .db import Database
from .errors import AuthError, BusinessError, ForbiddenError, NotFoundError
from .events import list_events, parse_cursor, sse_payload
from .exceptions import ExceptionService
from .models import (
    ApprovalDecision,
    EnvRegister,
    ExceptionApprove,
    ExceptionReject,
    ExceptionRequest,
    PlanCreate,
    RehearsalStart,
    ShiftRequest,
)
from .rehearsal import RehearsalService
from .service import Services
from .shift import ShiftService
from .sse_ticket import TicketSigner

CONSOLE_SOURCES = (CredentialSource.CF_ASSERTION, CredentialSource.BEARER)
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


def error_response(
    status: int, reason: str, detail: dict[str, Any] | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    body: dict[str, Any] = {"error": reason}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, headers=headers)


def install_error_handlers(app: Any) -> None:
    @app.exception_handler(AuthError)
    async def _auth(_: Request, exc: AuthError) -> JSONResponse:
        return error_response(401, exc.reason, headers={"WWW-Authenticate": 'Bearer realm="bravo", error="invalid_token"'})

    @app.exception_handler(ForbiddenError)
    async def _forbidden(_: Request, exc: ForbiddenError) -> JSONResponse:
        return error_response(
            403, exc.reason, exc.detail, headers={"WWW-Authenticate": 'Bearer realm="bravo", error="insufficient_scope"'}
        )

    @app.exception_handler(AccessNotConnectedError)
    async def _access_down(_: Request, exc: AccessNotConnectedError) -> JSONResponse:
        return error_response(503, exc.reason, {"message": exc.message})

    @app.exception_handler(NotFoundError)
    async def _nf(_: Request, exc: NotFoundError) -> JSONResponse:
        return error_response(404, exc.reason)

    @app.exception_handler(BusinessError)
    async def _biz(_: Request, exc: BusinessError) -> JSONResponse:
        return error_response(exc.status, exc.reason, exc.detail)


def build_router(
    settings: Settings,
    db: Database,
    verifier: Verifier,
    services: Services,
    shift: ShiftService,
    rehearsals: RehearsalService,
    tickets: TicketSigner,
    changes: ChangeService,
    exceptions: ExceptionService,
) -> APIRouter:
    router = APIRouter(prefix="/v1")

    async def principal(request: Request) -> Principal:
        token = extract_credential(request.headers.items(), CONSOLE_SOURCES)
        audience = audience_for(request.url.path, settings.console_aud or "", settings.mcp_aud or "")
        verified = await verifier.verify(token, audience)
        return await db.resolve_principal(verified, Channel.CONSOLE, request.headers.get("x-bravo-tenant"))

    @router.post("/environments", status_code=201)
    async def env_register(
        body: EnvRegister, p: Annotated[Principal, Depends(principal)], key: IdemKey = None
    ) -> dict[str, Any]:
        return await services.env_register(p, Channel.CONSOLE, body, key)

    @router.get("/me")
    async def me(p: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
        """Who is logged in (verified Access identity -> principal, tenant, roles). Read only."""
        authorize(p, Action.PLAN_READ, Channel.CONSOLE)
        order = ("security_approver", "approver", "operator", "viewer")
        roles = sorted((r.value for r in p.roles), key=order.index)
        return {"email": p.email, "tenant_id": p.tenant_id, "roles": roles, "role": roles[0] if roles else None,
                "principal": {"id": str(p.id), "iss": p.iss, "sub": p.sub}}

    @router.get("/environments")
    async def env_list(p: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
        return await services.env_list(p, Channel.CONSOLE)

    @router.get("/plans")
    async def plan_list(
        p: Annotated[Principal, Depends(principal)], limit: Annotated[int, Query(ge=1, le=200)] = 100
    ) -> dict[str, Any]:
        return await services.plan_list(p, Channel.CONSOLE, limit)

    @router.get("/environments/{environment_id}/controls")
    async def env_controls(environment_id: UUID, p: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
        return await services.env_controls(p, Channel.CONSOLE, environment_id)

    @router.post("/plans", status_code=201)
    async def plan_create(
        body: PlanCreate, p: Annotated[Principal, Depends(principal)], key: IdemKey = None
    ) -> dict[str, Any]:
        return await services.plan_create(p, Channel.CONSOLE, body, key)

    @router.get("/cost")
    async def cost(
        p: Annotated[Principal, Depends(principal)],
        plan_id: UUID | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Cost receipt ``cost/v1`` from the collector (T1, NFR-8); with plan_id, split before / during / after."""
        return await services.cost(p, Channel.CONSOLE, plan_id, start, end)

    @router.get("/plans/{plan_id}")
    async def plan_get(plan_id: UUID, p: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
        return await services.plan_get(p, Channel.CONSOLE, plan_id)

    @router.post("/plans/{plan_id}/gate")
    async def gate_run(plan_id: UUID, p: Annotated[Principal, Depends(principal)], key: IdemKey = None) -> dict[str, Any]:
        return await services.gate_run(p, Channel.CONSOLE, plan_id, key)

    @router.post("/plans/{plan_id}/rehearsals", status_code=202)
    async def rehearsal_start(
        plan_id: UUID, body: RehearsalStart, p: Annotated[Principal, Depends(principal)], key: IdemKey = None
    ) -> dict[str, Any]:
        return await rehearsals.start(p, Channel.CONSOLE, plan_id, body, key)

    @router.get("/plans/{plan_id}/changes")
    async def change_list(plan_id: UUID, p: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
        return await changes.list(p, Channel.CONSOLE, plan_id)

    @router.get("/plans/{plan_id}/changes/{change_id}")
    async def change_get(plan_id: UUID, change_id: UUID, p: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
        return await changes.get(p, Channel.CONSOLE, plan_id, change_id)

    @router.post("/plans/{plan_id}/changes", status_code=201)
    async def change_request(
        plan_id: UUID, p: Annotated[Principal, Depends(principal)], key: IdemKey = None
    ) -> dict[str, Any]:
        return await changes.request(p, Channel.CONSOLE, plan_id, key)

    @router.post("/plans/{plan_id}/approvals", status_code=201)
    async def approve(
        plan_id: UUID, body: ApprovalDecision, p: Annotated[Principal, Depends(principal)], key: IdemKey = None
    ) -> dict[str, Any]:
        return await services.approve(p, Channel.CONSOLE, plan_id, body, key)

    # Control exceptions (spec S1 5b, contract 15 §9): operator requests, approver decides, console only.
    @router.get("/plans/{plan_id}/exceptions")
    async def exception_list(plan_id: UUID, p: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
        return await exceptions.list(p, Channel.CONSOLE, plan_id)

    @router.post("/plans/{plan_id}/exceptions", status_code=201)
    async def exception_request(
        plan_id: UUID, body: ExceptionRequest, p: Annotated[Principal, Depends(principal)], key: IdemKey = None
    ) -> dict[str, Any]:
        return await exceptions.request(p, Channel.CONSOLE, plan_id, body, key)

    @router.post("/plans/{plan_id}/exceptions/{exception_id}/approve")
    async def exception_approve(
        plan_id: UUID, exception_id: UUID, body: ExceptionApprove, p: Annotated[Principal, Depends(principal)],
        key: IdemKey = None,
    ) -> dict[str, Any]:
        return await exceptions.approve(p, Channel.CONSOLE, plan_id, exception_id, body, key)

    @router.post("/plans/{plan_id}/exceptions/{exception_id}/reject")
    async def exception_reject(
        plan_id: UUID, exception_id: UUID, body: ExceptionReject, p: Annotated[Principal, Depends(principal)],
        key: IdemKey = None,
    ) -> dict[str, Any]:
        return await exceptions.reject(p, Channel.CONSOLE, plan_id, exception_id, body, key)

    @router.post("/plans/{plan_id}/shift", status_code=202)
    async def start_shift(
        plan_id: UUID, body: ShiftRequest, p: Annotated[Principal, Depends(principal)], key: IdemKey = None
    ) -> dict[str, Any]:
        return await shift.shift(p, Channel.CONSOLE, plan_id, body, key)

    @router.post("/plans/{plan_id}/events/ticket")
    async def events_ticket(plan_id: UUID, p: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
        await services.plan_get(p, Channel.CONSOLE, plan_id)  # authorize + 404 for other tenants
        return {"ticket": tickets.issue(p.id, p.tenant_id, plan_id), "expires_in": 60}

    @router.get("/plans/{plan_id}/events")
    async def events(
        request: Request,
        plan_id: UUID,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        after: Annotated[str | None, Query()] = None,
        ticket: Annotated[str | None, Query()] = None,
    ) -> EventSourceResponse:
        if ticket is not None and not any(
            k.lower() in ("authorization", "cf-access-jwt-assertion") for k in request.headers.keys()
        ):
            t = tickets.verify(ticket, plan_id)
            p = await db.resolve_ticket_principal(t)
        else:
            p = await principal(request)
        authorize(p, Action.PLAN_READ, Channel.CONSOLE)
        async with db.tenant_tx(p.tenant_id, p.id) as conn:
            await services.require_plan(conn, plan_id)
        cursor = parse_cursor(last_event_id if last_event_id is not None else after)

        async def stream() -> AsyncIterator[dict[str, str]]:
            nonlocal cursor
            while True:
                if await request.is_disconnected():
                    return
                async with db.tenant_tx(p.tenant_id, p.id) as conn:
                    batch = await list_events(conn, plan_id, cursor, 100)
                for event in batch:
                    cursor = int(event["id"])
                    yield sse_payload(event)
                if not batch:
                    await asyncio.sleep(settings.sse_poll_interval_s)

        return EventSourceResponse(stream(), ping=15, headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return router
