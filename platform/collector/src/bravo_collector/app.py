"""HTTP API of the collector (D-05). Read-only; handlers are sync so boto3/kubernetes/psycopg run in a threadpool."""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from . import __version__
from .clusters import ClusterView
from .environments import Registry, load_registry
from .inventory import ROOT_ENV, scan
from .redact import find_credentials, guard_json
from .service import SOURCES, collect, configured_sources

CREDENTIAL_SOURCE_ENV = "BRAVO_CREDENTIAL_SOURCE"
log = logging.getLogger("bravo_collector")


def credential_source() -> dict[str, Any]:
    """How this process gets cloud credentials, for the console banner. Never the credentials themselves.

    The pilot compose mounts the operator's ~/.aws read-only (temporary, NFR-6 / D-26 exception) and sets
    BRAVO_CREDENTIAL_SOURCE to say so. On EKS the collector uses Pod Identity and the label is not set.
    """
    label = os.environ.get(CREDENTIAL_SOURCE_ENV, "").strip()
    return {"label": label or "default credential chain", "temporary_pilot_path": bool(label)}


def create_app(registry: Registry | None = None, cluster_view: ClusterView | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.registry = registry if registry is not None else load_registry()
        if os.environ.get("BRAVO_COST_CONFIG", "").strip():
            # Warm the cost caches once (a cold read of every provider takes up to a minute; the console proxy
            # waits 20 s). Read-only; a failure only leaves the cache empty for the first request.
            from .costing.receipt import warm

            threading.Thread(target=warm, name="cost-warm", daemon=True).start()
        yield

    app = FastAPI(title="bravo-collector", version=__version__, lifespan=lifespan)
    app.state.cluster_view = cluster_view if cluster_view is not None else ClusterView()

    @app.middleware("http")
    async def guard_credentials(request: Request, call_next):
        """Last line of defence on every JSON response: mask credential-shaped values field by field, and
        replace a field with a "<withheld: …>" reason only if masking could not remove the shape. The response
        itself is always returned; the header X-Bravo-Credential-Guard counts masked/withheld fields."""
        response = await call_next(request)
        if not response.headers.get("content-type", "").startswith("application/json"):
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        text = body.decode("utf-8", errors="replace")
        if not find_credentials(text):
            return Response(content=body, status_code=response.status_code, headers=headers,
                            media_type=response.media_type)
        try:
            data = json.loads(text)
        except ValueError:
            return JSONResponse(status_code=500, content={"detail": "response withheld: credential material in a "
                                                          "non-JSON body"})
        clean, actions = guard_json(data)
        masked = sum(1 for a in actions if a["action"] == "masked")
        withheld = [a for a in actions if a["action"] == "withheld"]
        log.warning("credential guard: %d field(s) masked, %d withheld on %s %s (%s)", masked, len(withheld),
                    request.method, request.url.path, ", ".join(str(a["path"]) for a in withheld)[:300])
        headers["X-Bravo-Credential-Guard"] = f"masked={masked};withheld={len(withheld)}"
        return Response(content=json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                        status_code=response.status_code, headers=headers, media_type="application/json")

    def reg(request: Request) -> Registry:
        return request.app.state.registry

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/environments")
    def environments(request: Request) -> dict[str, Any]:
        return {
            "credential_source": credential_source(),
            "environments": [
                {"environment_id": eid, "display_name": env.display_name, "sources": configured_sources(env)}
                for eid, env in sorted(reg(request).environments.items())
            ],
        }

    @app.get("/v1/environments/{environment_id}/snapshot")
    def snapshot(
        environment_id: str,
        request: Request,
        sources: str | None = Query(default=None, description="comma-separated subset of aws,kubernetes,postgres"),
    ) -> dict[str, Any]:
        env = reg(request).get(environment_id)
        if env is None:
            raise HTTPException(status_code=404, detail="unknown environment")
        wanted = {s.strip() for s in sources.split(",") if s.strip()} if sources else None
        if wanted and not wanted <= set(SOURCES):
            raise HTTPException(status_code=422, detail=f"sources must be a subset of {list(SOURCES)}")
        return collect(env, wanted).model_dump(mode="json")

    @app.get("/v1/clusters")
    def clusters(request: Request) -> dict[str, Any]:
        """Live read-only view of every registry environment with a kubernetes section (console)."""
        return {**request.app.state.cluster_view.get(reg(request)), "credential_source": credential_source()}

    @app.get("/v1/inventory/snapshot")
    def inventory_snapshot() -> Response:
        """tar.gz of exactly the files the inventory scanned; header X-Bravo-Inventory-Digest (S2)."""
        from .inventory.snapshot import SnapshotError, build_snapshot

        root = os.environ.get(ROOT_ENV, "").strip()
        if not root or not Path(root).is_dir():
            raise HTTPException(status_code=503, detail=f"{ROOT_ENV} is not set or not a directory")
        try:
            snap = build_snapshot(Path(root))
        except SnapshotError as exc:
            status = 503 if exc.reason == "inventory_root_missing" else 422
            return JSONResponse(status_code=status, content={"error": exc.reason})
        if snap.files == 0:
            # An empty root means the repository was not provided; "no files" is not a valid snapshot.
            return JSONResponse(status_code=503, content={"error": "inventory_root_empty",
                                                          "skipped": len(snap.skipped)})
        return Response(content=snap.archive, media_type="application/gzip", headers={
            "X-Bravo-Inventory-Digest": snap.input_digest,
            "X-Bravo-Snapshot-Files": str(snap.files),
            "X-Bravo-Snapshot-Skipped": str(len(snap.skipped)),
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="repo-snapshot.tar.gz"',
        })

    @app.get("/v1/cost")
    def cost(
        start: str | None = Query(default=None, description="ISO date/time UTC; default month start, or plan - 24h"),
        end: str | None = Query(default=None, description="ISO date/time UTC; default now (never later than now)"),
        plan_id: str | None = None,
        plan_created_at: str | None = None,
        plan_done_at: str | None = None,
        plan_state: str | None = None,
        workload: str | None = None,
    ) -> dict[str, Any]:
        """Cost receipt ``cost/v1`` (T1, NFR-8). Read-only; Cost Explorer cached 1 h, inventory 5 min."""
        from .costing import receipt_for_request
        from .costing.receipt import CostRequestError

        try:
            return receipt_for_request(start=start, end=end, plan_id=plan_id, plan_created_at=plan_created_at,
                                       plan_done_at=plan_done_at, plan_state=plan_state, workload=workload)
        except CostRequestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/inventory")
    def inventory() -> dict[str, Any]:
        root = os.environ.get(ROOT_ENV, "").strip()
        if not root:
            raise HTTPException(status_code=503, detail=f"{ROOT_ENV} is not set")
        path = Path(root)
        if not path.is_dir():
            raise HTTPException(status_code=503, detail=f"{ROOT_ENV} is not a directory")
        report = scan(path)
        if report.files_scanned == 0:
            # Not "no dependencies": the repository is missing (an empty emptyDir, a failed checkout).
            raise HTTPException(status_code=503, detail="inventory_root_empty: no file to scan")
        return report.model_dump(mode="json")

    return app
