"""Executor HTTP API on :8081 (CONTRACT.md 6)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import front_api
from .approvals import ApprovalRejected
from .config import Settings
from .cutover import CUTOVER, FAILBACK
from .ledger import IdempotencyConflict
from .service import CutoverInput, ExecutorService, ObserveOnly, RunInProgress
from .topology import FileSecrets, settings_from_topology
from .vault import SecretBroker


class CutoverBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: UUID
    epoch: int = Field(ge=1)
    approval_id: str = Field(min_length=1, max_length=200)
    write_pause_budget_ms: int = Field(ge=1, le=600_000)


class CutoverAccepted(BaseModel):
    run_id: UUID


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        broker: SecretBroker | None = None
        resolved = settings
        topology = os.environ.get("BRAVO_TOPOLOGY_FILE", "").strip()
        if os.environ.get("BRAVO_BAO_ROLE_ID"):
            # secret_id is single-use and short-lived: log in at start-up, not on first request.
            broker = SecretBroker.from_env()
            await broker.read("platform/cloudflare")
        if resolved is None and topology:
            platform_dsn = os.environ.get("PLATFORM_DB_DSN", "").strip()
            if not platform_dsn:
                raise RuntimeError("PLATFORM_DB_DSN is required")
            secrets_dir = os.environ.get("BRAVO_SECRETS_DIR", "").strip()
            if secrets_dir:  # in-cluster: fields delivered from OpenBao into a mounted Secret
                files = FileSecrets(Path(secrets_dir))
                lookup = files.field
                if broker is None:  # the front switch reads platform/cloudflare the same way
                    app.state.secret_broker = files
            elif broker is not None:  # operator laptop: AppRole login (executor-bao.sh with-executor)
                lookup = broker.field
            else:
                raise RuntimeError("BRAVO_TOPOLOGY_FILE needs BRAVO_SECRETS_DIR or OpenBao AppRole credentials")
            resolved = await settings_from_topology(Path(topology), lookup, platform_dsn)
        service = ExecutorService(resolved or Settings.from_env())
        if broker is not None:
            app.state.secret_broker = broker
        await service.start()
        app.state.service = service
        try:
            yield
        finally:
            await service.stop()
            if broker is not None:
                await broker.close()

    app = FastAPI(title="Bravo Cutover Executor", version="0.1.0", lifespan=lifespan)
    app.include_router(front_api.router)

    def svc(request: Request) -> ExecutorService:
        return request.app.state.service

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        s = svc(request)
        return {"ok": True, "observe_only": s.observe_only, "observe_reason": s.observe_reason}

    @app.get("/v1/readiness")
    async def readiness(request: Request) -> dict[str, Any]:
        return await svc(request).readiness()

    @app.get("/v1/controls")
    async def controls(request: Request) -> list[dict[str, str]]:
        return await svc(request).controls()

    async def _submit(
        kind: str, body: CutoverBody, request: Request, response: Response, idempotency_key: str | None
    ) -> CutoverAccepted:
        if idempotency_key is None or not (1 <= len(idempotency_key) <= 200):
            raise HTTPException(status_code=400, detail={"code": "IDEMPOTENCY_KEY_REQUIRED"})
        try:
            run_id, created = await svc(request).submit(
                CutoverInput(
                    plan_id=body.plan_id,
                    epoch=body.epoch,
                    approval_id=body.approval_id,
                    write_pause_budget_ms=body.write_pause_budget_ms,
                    kind=kind,
                ),
                idempotency_key,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(
                status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSED", "run_id": str(exc.run_id)}
            ) from exc
        except RunInProgress as exc:
            raise HTTPException(
                status_code=409, detail={"code": "RUN_IN_PROGRESS", "run_id": str(exc.run_id)}
            ) from exc
        except ObserveOnly as exc:
            raise HTTPException(status_code=503, detail={"code": "OBSERVE_ONLY"}) from exc
        except ApprovalRejected as exc:
            raise HTTPException(status_code=403, detail={"code": exc.code}) from exc
        response.headers["Idempotent-Replayed"] = "false" if created else "true"
        return CutoverAccepted(run_id=run_id)

    @app.post("/v1/cutover", status_code=202, response_model=CutoverAccepted)
    async def cutover(
        body: CutoverBody,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CutoverAccepted:
        return await _submit(CUTOVER, body, request, response, idempotency_key)

    @app.post("/v1/failback", status_code=202, response_model=CutoverAccepted)
    async def failback(
        body: CutoverBody,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CutoverAccepted:
        """FR-11, 03-migration.md 4.3. Same request and response shape as /v1/cutover."""
        return await _submit(FAILBACK, body, request, response, idempotency_key)

    @app.get("/v1/runs")
    async def list_runs(request: Request, kind: str | None = None, limit: int = 20) -> JSONResponse:
        """Recent runs, newest first (operator CLI: find the run the console started)."""
        rows = await svc(request).ledger.list_runs(kind, max(1, min(limit, 200)))
        return JSONResponse(_jsonable(rows))

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: UUID, request: Request) -> JSONResponse:
        data = await svc(request).ledger.get_run(run_id)
        if data is None:
            raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"})
        run = data["run"]
        receipt = data["receipt"]
        return JSONResponse(
            _jsonable(
                {
                    "run_id": run["run_id"],
                    "kind": run["kind"],
                    "plan_id": run["plan_id"],
                    "epoch": run["epoch"],
                    "status": run["status"],
                    "write_pause_ms": run["write_pause_ms"],
                    "started_at": run["started_at"],
                    "ended_at": run["ended_at"],
                    "stages": data["stages"],
                    "receipt": None if receipt is None else receipt["body"],
                    "effects": data["effects"],
                }
            )
        )

    return app
