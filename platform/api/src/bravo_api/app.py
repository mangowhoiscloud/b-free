"""One image, two modes. ``BRAVO_MODE=api`` serves ``/v1/*``; ``BRAVO_MODE=mcp`` serves ``/mcp``.

Run: ``uvicorn bravo_api.app:create_app --factory --host 0.0.0.0 --port 8080``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from starlette.routing import Route

from . import __version__, telemetry
from .auth import JWKSSource, NotConnectedVerifier, RemoteJWKS, TokenVerifier, Verifier
from .changes import ChangeService, ChangeWatcher
from .config import Mode, Settings
from .db import Bootstrap, Database
from .exceptions import ExceptionService
from .executor_client import ExecutorClient
from .mcp_server import MCPGuard, build_server
from .rehearsal import RehearsalService, RehearsalWatcher
from .rest import build_router, install_error_handlers
from .service import Services
from .shift import RunWatcher, ShiftService
from .sse_ticket import TicketSigner
from .upstreams import CollectorClient, GateControlsClient, RehearsalClient

LOG = logging.getLogger("bravo_api")


def create_app(settings: Settings | None = None, *, jwks: JWKSSource | None = None) -> FastAPI:
    """``jwks`` replaces the certs-URL key source (tests only); the product path never passes it."""
    settings = settings or Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    telemetry.setup("operator-mcp" if settings.mode is Mode.MCP else "api")

    bootstrap = (
        Bootstrap(settings.bootstrap_email, settings.bootstrap_tenant, settings.bootstrap_roles)
        if settings.bootstrap_email and settings.bootstrap_tenant
        else None
    )
    db = Database(settings.database_url, settings.db_pool_min, settings.db_pool_max, bootstrap, settings.access_members)
    executor = ExecutorClient(settings.executor_url, settings.executor_timeout_s)
    verifier: Verifier
    if settings.access_configured:
        assert settings.jwt_issuer is not None and settings.jwks_url is not None
        verifier = TokenVerifier(settings.jwt_issuer, jwks or RemoteJWKS(settings.jwks_url, ttl_s=settings.jwks_ttl_s))
    else:
        LOG.error("Cloudflare Access not configured (BRAVO_ACCESS_TEAM_DOMAIN / BRAVO_CONSOLE_AUD / BRAVO_MCP_AUD): "
                  "every authenticated request is refused with 503 access_not_connected")
        verifier = NotConnectedVerifier()
    timeout = settings.executor_timeout_s
    gate_controls = (
        GateControlsClient(settings.gate_controls_url, settings.gate_timeout_s) if settings.gate_controls_url else None
    )
    collector = CollectorClient(settings.collector_url, timeout) if settings.collector_url else None
    rehearsal_client = (
        RehearsalClient(settings.rehearsal_url, settings.rehearsal_timeout_s, os.environ.get("BRAVO_REHEARSAL_API_TOKEN"))
        if settings.rehearsal_url
        else None
    )
    upstreams = [c for c in (gate_controls, collector, rehearsal_client) if c is not None]
    services = Services(db, executor, settings, gate_controls=gate_controls, collector=collector)
    rehearsals = RehearsalService(db, rehearsal_client, settings, collector=collector)
    watcher = RunWatcher(db, settings.run_poll_interval_s, settings.approval_ttl_s)
    rehearsal_watcher = RehearsalWatcher(db, rehearsal_client, settings)
    change_watcher = ChangeWatcher(db, services, settings)
    manager: StreamableHTTPSessionManager | None = None
    if settings.mode is Mode.MCP:
        manager = StreamableHTTPSessionManager(app=build_server(services, rehearsals), stateless=True, json_response=True)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await db.open()
        try:
            if manager is not None:
                async with manager.run():
                    yield
            else:
                resumed = await watcher.resume()
                rehearsal_watcher.start()
                change_watcher.start()
                LOG.info("api ready; resumed %d in-flight run watchers", resumed)
                yield
                await watcher.close()
                await rehearsal_watcher.close()
                await change_watcher.close()
        finally:
            await verifier.aclose()
            await executor.aclose()
            await services.secret_refs.aclose()
            for client in upstreams:
                await client.aclose()
            await db.close()

    app = FastAPI(
        title="Bravo platform API" if settings.mode is Mode.API else "Bravo operator MCP",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.mode is Mode.API else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.mode is Mode.API else None,
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "mode": settings.mode.value}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, Any]:
        await db.ping()
        return {"status": "ready", "mode": settings.mode.value}

    if settings.mode is Mode.API:
        install_error_handlers(app)
        app.include_router(
            build_router(
                settings,
                db,
                verifier,
                services,
                ShiftService(db, executor, watcher, settings.approval_ttl_s),
                rehearsals,
                TicketSigner(settings.sse_ticket_secret),
                ChangeService(db),
                ExceptionService(db),
            )
        )
        if settings.cors_origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=list(settings.cors_origins),
                allow_credentials=False,
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=[
                    "Authorization",
                    "Cf-Access-Jwt-Assertion",
                    "Content-Type",
                    "Idempotency-Key",
                    "Last-Event-ID",
                    "X-Bravo-Tenant",
                ],
                expose_headers=["WWW-Authenticate"],
                max_age=600,
            )
    else:
        assert manager is not None
        app.router.routes.append(
            Route("/mcp", endpoint=MCPGuard(settings, db, verifier, manager), methods=["GET", "POST", "DELETE"])
        )

    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")
    return app
