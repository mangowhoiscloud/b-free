"""HTTP API of the collector (D-05). Read-only; handlers are sync so boto3/kubernetes/psycopg run in a threadpool."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from . import __version__
from .clusters import ClusterView
from .environments import Registry, load_registry
from .inventory import ROOT_ENV, scan
from .redact import find_credentials
from .service import SOURCES, collect, configured_sources

CREDENTIAL_SOURCE_ENV = "BRAVO_CREDENTIAL_SOURCE"


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
        yield

    app = FastAPI(title="bravo-collector", version=__version__, lifespan=lifespan)
    app.state.cluster_view = cluster_view if cluster_view is not None else ClusterView()

    @app.middleware("http")
    async def withhold_credentials(request: Request, call_next):
        """Last line of defence: a JSON body that contains credential material is not sent."""
        response = await call_next(request)
        if not response.headers.get("content-type", "").startswith("application/json"):
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        hits = find_credentials(body.decode("utf-8", errors="replace"))
        if hits:
            return JSONResponse(status_code=500, content={"detail": "response withheld: credential material",
                                                          "patterns": hits})
        return Response(content=body, status_code=response.status_code, headers=dict(response.headers),
                        media_type=response.media_type)

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
