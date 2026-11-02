"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


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


BOOTSTRAP_ROLES = frozenset({"viewer", "operator", "approver", "security_approver"})
CERTS_PATH = "/cdn-cgi/access/certs"  # Cloudflare Access signing keys under the team domain


def _https(name: str, value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise RuntimeError(f"{name} must be an https URL")
    return value.rstrip("/")


def access_from_env() -> tuple[str | None, str | None]:
    """(issuer, certs URL) from ``BRAVO_ACCESS_TEAM_DOMAIN`` or ``BRAVO_JWT_ISSUER`` [+ ``BRAVO_JWKS_URL``].

    The team domain gives both. An explicit issuer without a JWKS URL uses the Access certs path under it.
    Nothing set -> (None, None): Access is "not connected" and the API refuses every login.
    """
    team = os.environ.get("BRAVO_ACCESS_TEAM_DOMAIN", "").strip().removeprefix("https://").rstrip("/")
    issuer = os.environ.get("BRAVO_JWT_ISSUER", "").strip()
    jwks = os.environ.get("BRAVO_JWKS_URL", "").strip()
    if team:
        team_issuer = f"https://{team}"
        if issuer and issuer.rstrip("/") != team_issuer:
            raise RuntimeError("BRAVO_JWT_ISSUER disagrees with BRAVO_ACCESS_TEAM_DOMAIN")
        issuer = team_issuer
    if not issuer:
        if jwks:
            raise RuntimeError("BRAVO_JWKS_URL is set without an issuer (BRAVO_ACCESS_TEAM_DOMAIN or BRAVO_JWT_ISSUER)")
        return None, None
    issuer = _https("BRAVO_JWT_ISSUER", issuer)
    return issuer, _https("BRAVO_JWKS_URL", jwks or issuer + CERTS_PATH)


def bootstrap_roles_from_env() -> tuple[str, ...]:
    raw = os.environ.get("BRAVO_BOOTSTRAP_ROLES", "viewer,operator").split(",")
    roles = tuple(sorted({r.strip() for r in raw if r.strip()}))
    if not roles or not set(roles) <= BOOTSTRAP_ROLES:
        raise RuntimeError("BRAVO_BOOTSTRAP_ROLES must be a subset of " + ",".join(sorted(BOOTSTRAP_ROLES)))
    if "operator" in roles and ({"approver", "security_approver"} & set(roles)):
        raise RuntimeError("BRAVO_BOOTSTRAP_ROLES cannot combine operator with an approver role (separation of duties)")
    return roles


@dataclass(frozen=True, slots=True)
class AccessMembers:
    """Just-in-time membership allowlist (config, not code): verified Access email -> roles in ``tenant``.

    File (``BRAVO_ACCESS_MEMBERS_FILE``, JSON): ``{"tenant": "<id>", "members": [{"email": "...", "roles":
    ["operator"]}, ...]}``. Applied on an identity's first login only; any other email is never granted.
    """

    tenant_id: str
    roles_by_email: tuple[tuple[str, tuple[str, ...]], ...]

    def roles_for(self, email: str) -> tuple[str, ...] | None:
        e = email.strip().lower()
        return next((r for m, r in self.roles_by_email if m == e), None)


def access_members_from_file(path: str | None) -> AccessMembers | None:
    if not path:
        return None
    import json

    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    tenant = raw.get("tenant") if isinstance(raw, dict) else None
    if not isinstance(tenant, str) or not tenant:
        raise RuntimeError("BRAVO_ACCESS_MEMBERS_FILE: 'tenant' is required")
    seen: dict[str, tuple[str, ...]] = {}
    for m in raw.get("members") or []:
        email, roles = str(m.get("email", "")).strip().lower(), tuple(sorted(set(m.get("roles") or [])))
        if "@" not in email or not roles or not set(roles) <= BOOTSTRAP_ROLES:
            raise RuntimeError(f"BRAVO_ACCESS_MEMBERS_FILE: bad member entry for {email or '?'}")
        if "operator" in roles and ({"approver", "security_approver"} & set(roles)):
            raise RuntimeError(f"BRAVO_ACCESS_MEMBERS_FILE: {email} cannot be operator and approver (separation of duties)")
        if email in seen:
            raise RuntimeError(f"BRAVO_ACCESS_MEMBERS_FILE: {email} listed twice")
        seen[email] = roles
    return AccessMembers(tenant, tuple(sorted(seen.items())))


@dataclass(frozen=True, slots=True)
class Settings:
    mode: Mode
    database_url: str
    # Cloudflare Access (audit A3). None = not connected: every login is refused with 503.
    jwt_issuer: str | None
    console_aud: str | None
    mcp_aud: str | None
    executor_url: str
    executor_timeout_s: float
    console_base_url: str
    cors_origins: tuple[str, ...]
    mcp_allowed_origins: tuple[str, ...]
    mcp_allowed_hosts: tuple[str, ...]
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
    change_poll_interval_s: float = 1.0  # T31 change watcher (changes / change_stages written by platform/apply)
    # Platform OpenBao for secret_ref resolution (audit B11). Unset -> environments are "unverified".
    bao_addr: str | None = None
    bao_cacert: str | None = None
    bao_kv_mount: str = "secret"
    bao_role_id_file: str | None = None
    bao_secret_id_file: str | None = None
    bao_token_file: str | None = None
    rehearsal_max_attempts: int = 3  # FR-5
    rehearsal_max_infra_errors: int = 2  # 07 3
    collector_url: str | None = None  # platform/collector inventory (FR-1, FR-2)
    gate_controls_url: str | None = None  # platform/gate control results (FR-12)
    gate_timeout_s: float = 60.0  # the gate report runs every probe of both environments
    jwks_url: str | None = None  # Access certs URL; keys are fetched from here only
    jwks_ttl_s: float = 300.0
    # First-login bootstrap (A3): this exact email may become the first member of this tenant.
    bootstrap_email: str | None = None
    bootstrap_tenant: str | None = None
    bootstrap_roles: tuple[str, ...] = ("operator", "viewer")
    access_members: AccessMembers | None = None  # JIT allowlist (console landing, 2026-11-02)

    @staticmethod
    def bao_from_env() -> tuple[str | None, str | None, str, str | None, str | None, str | None]:
        """(addr, cacert, kv mount, role_id file, secret_id file, token file) for ``BaoSettings``."""
        return (
            _opt_url("BRAVO_BAO_ADDR"),
            os.environ.get("BRAVO_BAO_CACERT") or None,
            os.environ.get("BRAVO_BAO_KV_MOUNT", "secret").strip("/") or "secret",
            os.environ.get("BRAVO_BAO_ROLE_ID_FILE") or None,
            os.environ.get("BRAVO_BAO_SECRET_ID_FILE") or None,
            os.environ.get("BRAVO_BAO_TOKEN_FILE") or None,
        )

    @property
    def access_configured(self) -> bool:
        return bool(self.jwt_issuer and self.jwks_url and self.console_aud and self.mcp_aud)

    @classmethod
    def from_env(cls) -> Settings:
        mode = Mode(_env("BRAVO_MODE"))
        issuer, jwks_url = access_from_env()
        return cls(
            mode=mode,
            database_url=_env("BRAVO_DATABASE_URL"),
            jwt_issuer=issuer,
            console_aud=os.environ.get("BRAVO_CONSOLE_AUD", "").strip() or None,
            mcp_aud=os.environ.get("BRAVO_MCP_AUD", "").strip() or None,
            executor_url=_env("BRAVO_EXECUTOR_URL").rstrip("/"),
            executor_timeout_s=float(_env("BRAVO_EXECUTOR_TIMEOUT_S", "3")),
            console_base_url=_env("BRAVO_CONSOLE_BASE_URL").rstrip("/"),
            cors_origins=_csv("BRAVO_CORS_ORIGINS", ""),
            mcp_allowed_origins=_csv("BRAVO_MCP_ALLOWED_ORIGINS", ""),
            mcp_allowed_hosts=_csv("BRAVO_MCP_ALLOWED_HOSTS", ""),
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
            change_poll_interval_s=float(_env("BRAVO_CHANGE_POLL_INTERVAL_S", "1")),
            bao_addr=_opt_url("BRAVO_BAO_ADDR"),
            bao_cacert=os.environ.get("BRAVO_BAO_CACERT") or None,
            bao_kv_mount=os.environ.get("BRAVO_BAO_KV_MOUNT", "secret").strip("/") or "secret",
            bao_role_id_file=os.environ.get("BRAVO_BAO_ROLE_ID_FILE") or None,
            bao_secret_id_file=os.environ.get("BRAVO_BAO_SECRET_ID_FILE") or None,
            bao_token_file=os.environ.get("BRAVO_BAO_TOKEN_FILE") or None,
            rehearsal_max_attempts=int(_env("BRAVO_REHEARSAL_MAX_ATTEMPTS", "3")),
            rehearsal_max_infra_errors=int(_env("BRAVO_REHEARSAL_MAX_INFRA_ERRORS", "2")),
            collector_url=_opt_url("BRAVO_COLLECTOR_URL"),
            gate_controls_url=_opt_url("BRAVO_GATE_URL"),
            gate_timeout_s=float(_env("BRAVO_GATE_TIMEOUT_S", "60")),
            jwks_url=jwks_url,
            jwks_ttl_s=float(_env("BRAVO_JWKS_TTL_S", "300")),
            bootstrap_email=os.environ.get("BRAVO_BOOTSTRAP_EMAIL", "").strip() or None,
            bootstrap_tenant=os.environ.get("BRAVO_BOOTSTRAP_TENANT", "").strip() or None,
            bootstrap_roles=bootstrap_roles_from_env(),
            access_members=access_members_from_file(os.environ.get("BRAVO_ACCESS_MEMBERS_FILE") or None),
        )
