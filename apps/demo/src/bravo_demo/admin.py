"""Write-block switch on a separate admin port (spec r2 §8.10, deploy/CLOUD-E2E-CONTRACT.md §8).

  GET /admin/writes                      -> {"blocked": bool, "since": ts}
  PUT /admin/writes {"blocked": bool}    -> the new state; returns once writes in flight have ended

The Executor calls it at cutover step 2 ("block new business writes at every entry point") and
when it opens writes again. The state is a row per environment in the write database
(table write_block), so every replica behind the Service sees the same switch, and the check
runs inside each write transaction (db.py `_guard_writes`).

Authentication: `Authorization: Bearer <token>`. The token is read from ADMIN_TOKEN_FILE (Secret
chat-admin, key token, delivered by the platform from OpenBao) on every request, so a rotated
Secret applies without a restart. Fail closed: without a usable token every request is refused.
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .config import Settings
from .db import ChatDb, WriteTimeout
from .telemetry import Instruments

log = logging.getLogger(__name__)

MIN_TOKEN_CHARS = 32


def _configured_token(path: str | None) -> str | None:
    if not path:
        return None
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if len(token) >= MIN_TOKEN_CHARS else None


def create_admin_app(settings: Settings, state: dict[str, Any], instruments: Instruments) -> FastAPI:
    admin = FastAPI(title="Bravo demo admin", docs_url=None, redoc_url=None, openapi_url=None)
    switch_counter = instruments.write_block_changes

    @admin.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Response:
        token = _configured_token(settings.admin.token_file)
        if token is None:
            log.warning("admin request refused: no admin token configured", extra={"path": request.url.path})
            return JSONResponse({"error": "admin_token_not_configured"}, status_code=503)
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip().encode(), token.encode()):
            log.warning("admin request refused: bad bearer token", extra={"path": request.url.path})
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def _db() -> ChatDb | None:
        return state.get("db") if state.get("ready") else None

    @admin.get("/admin/writes")
    async def get_writes() -> Response:
        db = _db()
        if db is None:
            return JSONResponse({"error": "starting"}, status_code=503)
        try:
            current = await db.write_block_state(settings.admin.switch_wait_s)
        except WriteTimeout:
            return JSONResponse({"error": "db_unavailable"}, status_code=503)
        return JSONResponse(current.public())

    @admin.put("/admin/writes")
    async def put_writes(request: Request) -> Response:
        db = _db()
        if db is None:
            return JSONResponse({"error": "starting"}, status_code=503)
        try:
            doc = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(doc, dict) or set(doc) != {"blocked"} or not isinstance(doc["blocked"], bool):
            return JSONResponse({"error": "body_must_be_{\"blocked\": bool}"}, status_code=400)
        try:
            current = await db.set_write_block(doc["blocked"], settings.instance_id, settings.admin.switch_wait_s)
        except WriteTimeout:
            # State unknown to the caller: it must read GET /admin/writes before acting (NFR-3).
            return JSONResponse({"error": "db_unavailable"}, status_code=503)
        switch_counter.add(1, {"blocked": str(current.blocked).lower()})
        log.info(
            "write-block switch set",
            extra={"blocked": current.blocked, "since": current.since, "env_id": settings.env_id,
                   "client": request.client.host if request.client else None},
        )
        return JSONResponse(current.public())

    return admin
