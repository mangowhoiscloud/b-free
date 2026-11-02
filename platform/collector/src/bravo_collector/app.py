"""HTTP API of the collector (D-05). Read-only; handlers are sync so boto3/kubernetes/psycopg run in a threadpool."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from . import __version__
from .clusters import ClusterView
from .environments import Registry, load_registry
from .inventory import ROOT_ENV, scan
from .service import SOURCES, collect, configured_sources


def create_app(registry: Registry | None = None, cluster_view: ClusterView | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.registry = registry if registry is not None else load_registry()
        yield

    app = FastAPI(title="bravo-collector", version=__version__, lifespan=lifespan)
    app.state.cluster_view = cluster_view if cluster_view is not None else ClusterView()

    def reg(request: Request) -> Registry:
        return request.app.state.registry

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/environments")
    def environments(request: Request) -> dict[str, Any]:
        return {
            "environments": [
                {"environment_id": eid, "display_name": env.display_name, "sources": configured_sources(env)}
                for eid, env in sorted(reg(request).environments.items())
            ]
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
        return request.app.state.cluster_view.get(reg(request))

    @app.get("/v1/inventory")
    def inventory() -> dict[str, Any]:
        root = os.environ.get(ROOT_ENV, "").strip()
        if not root:
            raise HTTPException(status_code=503, detail=f"{ROOT_ENV} is not set")
        path = Path(root)
        if not path.is_dir():
            raise HTTPException(status_code=503, detail=f"{ROOT_ENV} is not a directory")
        return scan(path).model_dump(mode="json")

    return app
