"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Mode(StrEnum):
    API = "api"
    MCP = "mcp"


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in os.environ.get(name, default).split(",") if v.strip())


def _opt_url(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value.rstrip("/") or None


@dataclass(frozen=True, slots=True)
class Settings:
    mode: Mode
    database_url: str
    jwks_path: Path
    jwt_issuer: str
    console_aud: str
    mcp_aud: str
    executor_url: str
    executor_timeout_s: float
    console_base_url: str
    cors_origins: tuple[str, ...]
    mcp_allowed_origins: tuple[str, ...]
    mcp_allowed_hosts: tuple[str, ...]
    dev_auth: bool
    dev_tokens_dir: Path | None
    approval_ttl_s: int
    gate_max_lag_bytes: int
    gate_required_controls: tuple[str, ...]
    run_poll_interval_s: float
    sse_poll_interval_s: float
    sse_ticket_secret: bytes | None
    db_pool_min: int = 1
    db_pool_max: int = 10
    # Optional upstreams. Unset means "not configured": the code says so, it never invents data.
    rehearsal_url: str | None = None  # platform/rehearsal service (S2)
    rehearsal_timeout_s: float = 5.0
    rehearsal_poll_interval_s: float = 1.0
    rehearsal_max_attempts: int = 3  # FR-5
    rehearsal_max_infra_errors: int = 2  # 07 3
    collector_url: str | None = None  # platform/collector inventory (FR-1, FR-2)
    gate_controls_url: str | None = None  # platform/gate control results (FR-12)

    @classmethod
    def from_env(cls) -> Settings:
        mode = Mode(_env("BRAVO_MODE"))
        dev_dir = os.environ.get("BRAVO_DEV_TOKENS_DIR")
        return cls(
            mode=mode,
            database_url=_env("BRAVO_DATABASE_URL"),
            jwks_path=Path(_env("BRAVO_JWKS_PATH")),
            jwt_issuer=_env("BRAVO_JWT_ISSUER"),
            console_aud=_env("BRAVO_CONSOLE_AUD"),
            mcp_aud=_env("BRAVO_MCP_AUD"),
            executor_url=_env("BRAVO_EXECUTOR_URL").rstrip("/"),
            executor_timeout_s=float(_env("BRAVO_EXECUTOR_TIMEOUT_S", "3")),
            console_base_url=_env("BRAVO_CONSOLE_BASE_URL").rstrip("/"),
            cors_origins=_csv("BRAVO_CORS_ORIGINS", ""),
            mcp_allowed_origins=_csv("BRAVO_MCP_ALLOWED_ORIGINS", ""),
            mcp_allowed_hosts=_csv("BRAVO_MCP_ALLOWED_HOSTS", ""),
            dev_auth=os.environ.get("BRAVO_DEV_AUTH", "0") == "1",
            dev_tokens_dir=Path(dev_dir) if dev_dir else None,
            approval_ttl_s=int(_env("BRAVO_APPROVAL_TTL_S", "900")),
            gate_max_lag_bytes=int(_env("BRAVO_GATE_MAX_LAG_BYTES", "65536")),
            gate_required_controls=_csv("BRAVO_GATE_REQUIRED_CONTROLS", "DB-ANON-READ,DB-AUTH"),
            run_poll_interval_s=float(_env("BRAVO_RUN_POLL_INTERVAL_S", "0.5")),
            sse_poll_interval_s=float(_env("BRAVO_SSE_POLL_INTERVAL_S", "0.5")),
            sse_ticket_secret=os.environ.get("BRAVO_SSE_TICKET_SECRET", "").encode() or None,
            db_pool_min=int(_env("BRAVO_DB_POOL_MIN", "1")),
            db_pool_max=int(_env("BRAVO_DB_POOL_MAX", "10")),
            rehearsal_url=_opt_url("BRAVO_REHEARSAL_URL"),
            rehearsal_timeout_s=float(_env("BRAVO_REHEARSAL_TIMEOUT_S", "5")),
            rehearsal_poll_interval_s=float(_env("BRAVO_REHEARSAL_POLL_INTERVAL_S", "1")),
            rehearsal_max_attempts=int(_env("BRAVO_REHEARSAL_MAX_ATTEMPTS", "3")),
            rehearsal_max_infra_errors=int(_env("BRAVO_REHEARSAL_MAX_INFRA_ERRORS", "2")),
            collector_url=_opt_url("BRAVO_COLLECTOR_URL"),
            gate_controls_url=_opt_url("BRAVO_GATE_URL"),
        )
