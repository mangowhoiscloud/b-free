"""HTTP API.

GET /v1/controls?source=ID&target=ID[&level=question|control]
    CONTRACT.md §6 schema exactly: [{control_id, question, src, dst}], values PASS | FAIL | UNKNOWN.
    ``level=question`` (default) gives one row per question (OBJ-01, DB-TLS, ...); ``level=control`` gives
    one row per control (SEC-01, ENC-01, AUD-01, NET-01, IAM-01, DR-01).
GET /v1/controls/report?source=ID&target=ID
    The same run with evidence, 4-state labels, per-row verdicts, input digest, collected_at, expires_at. Read-only:
    OBJ-03/OBJ-04 write probes answer ``write_probe_not_allowed_here`` (audit 48).
POST /v1/controls/report  body {"source": ID, "target": ID}
    The run the platform API makes when it evaluates a plan. Same report, and the write probes run on stores whose
    registry entry has ``write_probes: true``: PUT on the gate's own keys under ``_gate/`` only, each deleted again
    with the cleanup result in the evidence.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import __version__
from .controls import CONTROLS, QUESTIONS, RULE_VERSION, contract_rows
from .environments import Environment, Registry, load_registry
from .service import compare


def create_app(registry: Registry | None = None) -> FastAPI:
    app = FastAPI(title="bravo-gate", version=__version__)
    app.state.registry = registry

    def reg() -> Registry:
        if app.state.registry is None:
            app.state.registry = load_registry()
        return app.state.registry

    def pair(source: str, target: str) -> tuple[Environment, Environment]:
        r = reg()
        s, t = r.get(source), r.get(target)
        missing = [x for x, e in ((source, s), (target, t)) if e is None]
        if missing:
            raise HTTPException(404, detail={"reason": "unknown_environment", "environments": missing})
        assert s is not None and t is not None
        return s, t

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/catalog")
    def catalog() -> dict[str, Any]:
        return {
            "rule_version": RULE_VERSION,
            "controls": [{"control_id": c.control_id, "aliases": list(c.aliases), "name_ko": c.name_ko,
                          "question": c.question} for c in CONTROLS],
            "questions": [{"control_id": q.qid, "parent_control": q.control_id, "question": q.question,
                           "needs": q.needs} for q in QUESTIONS],
        }

    @app.get("/v1/environments")
    def environments() -> list[dict[str, Any]]:
        return [{"environment_id": e.environment_id, "display_name": e.display_name,
                 "sections": [s for s in ("object_store", "postgres", "kubernetes", "aws", "network", "platform")
                              if getattr(e, s) is not None]}
                for e in reg().environments.values()]

    @app.get("/v1/controls")
    def controls(
        source: Annotated[str, Query(min_length=1)],
        target: Annotated[str, Query(min_length=1)],
        level: Literal["question", "control"] = "question",
    ) -> list[dict[str, str]]:
        s, t = pair(source, target)
        return contract_rows(compare(s, t), level)

    @app.get("/v1/controls/report")
    def report(
        source: Annotated[str, Query(min_length=1)],
        target: Annotated[str, Query(min_length=1)],
    ) -> dict[str, Any]:
        s, t = pair(source, target)
        return compare(s, t)

    @app.post("/v1/controls/report")
    def report_run(body: RunRequest) -> dict[str, Any]:
        s, t = pair(body.source, body.target)
        return compare(s, t, allow_writes=True)

    return app


class RunRequest(BaseModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
