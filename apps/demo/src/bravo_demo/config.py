"""Settings from environment variables only (§8.10). No host, path or secret is built in."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Database ceiling for one attachment (CHECK attachments_size_cap in schema/sql/01_schema.sql).
SCHEMA_MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

_STORE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_ENV_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class ConfigError(ValueError):
    """A required setting is missing or invalid. The message names the variable, never its value."""


def _req(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _opt(env: dict[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _float(env: dict[str, str], name: str, default: float, lo: float, hi: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return value


def _int(env: dict[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return value


def _bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class DbSettings:
    # libpq DSN without the password, e.g. "host=pgbouncer port=6432 dbname=chat user=app_writer".
    dsn: str
    password_source: str  # aws-secrets-manager | env-file
    password_file: str | None
    secret_id: str | None
    secret_key: str | None  # JSON key inside the secret; None = the secret string is the password
    secrets_region: str | None
    secrets_endpoint_url: str | None
    pool_min: int
    pool_max: int
    statement_timeout_ms: int  # per write transaction (SET LOCAL), §8.10 max 200 ms
    write_wait_s: float  # how long a new write may wait during a write pause (§5.3: 8 s)
    connect_timeout_s: int


@dataclass(frozen=True, slots=True)
class S3Settings:
    bucket: str
    region: str | None
    endpoint_url: str | None
    access_key_id: str | None
    secret_access_key: str | None
    addressing_style: str  # auto | path | virtual
    store_id: str  # must equal attachment_store.active_store on the DB the writes reach
    max_bytes: int
    conditional_put: bool  # If-None-Match: * so an existing key is never overwritten


@dataclass(frozen=True, slots=True)
class AdminSettings:
    # Separate listener for the write-block switch (spec r2 §8.10). Never exposed through the edge.
    host: str
    port: int
    token_file: str | None  # Secret chat-admin, key token; missing/short token = admin API refuses all
    retry_after_s: int  # Retry-After for writes rejected while blocked
    switch_wait_s: float  # how long a switch change may wait for writes in flight


@dataclass(frozen=True, slots=True)
class Settings:
    env_label: str
    env_label_ja: str | None
    env_id: str
    instance_id: str
    public_url: str | None
    host: str
    port: int
    room_default: str
    poll_interval_s: float
    gap_grace_s: float
    backlog_limit: int
    db: DbSettings
    s3: S3Settings | None
    otlp_endpoint: str | None
    service_name: str
    log_level: str
    forwarded_allow_ips: str
    admin: AdminSettings


# Newer AWS SDKs send CRC checksum trailers by default; S3-compatible stores and the GCS XML API
# can reject them. The SDK reads these two variables; the deployment may still override them.
SDK_CHECKSUM_DEFAULTS = {
    "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
    "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
}


def apply_sdk_defaults() -> None:
    for name, value in SDK_CHECKSUM_DEFAULTS.items():
        os.environ.setdefault(name, value)


def load(env: dict[str, str] | None = None) -> Settings:
    """Read settings. The same variable names on every environment (AWS S3, GCS XML API with HMAC
    keys, an on-prem S3-compatible store): there is no per-cloud code path."""
    env = dict(os.environ) if env is None else env

    source = _req(env, "DB_PASSWORD_SOURCE")
    if source not in ("aws-secrets-manager", "env-file"):
        raise ConfigError("DB_PASSWORD_SOURCE must be aws-secrets-manager or env-file")
    statement_timeout_ms = _int(env, "DB_STATEMENT_TIMEOUT_MS", 200, 10, 200)
    db = DbSettings(
        dsn=_req(env, "DB_DSN"),
        password_source=source,
        password_file=_req(env, "DB_PASSWORD_FILE") if source == "env-file" else None,
        secret_id=_req(env, "DB_PASSWORD_SECRET_ID") if source == "aws-secrets-manager" else None,
        secret_key=_opt(env, "DB_PASSWORD_SECRET_KEY"),
        secrets_region=_opt(env, "DB_PASSWORD_SECRET_REGION") or _opt(env, "AWS_REGION"),
        secrets_endpoint_url=_opt(env, "DB_PASSWORD_SECRET_ENDPOINT_URL"),
        pool_min=_int(env, "DB_POOL_MIN", 1, 0, 50),
        pool_max=_int(env, "DB_POOL_MAX", 8, 1, 100),
        statement_timeout_ms=statement_timeout_ms,
        write_wait_s=_float(env, "WRITE_WAIT_S", 8.0, 0.5, 60.0),
        connect_timeout_s=_int(env, "DB_CONNECT_TIMEOUT_S", 3, 1, 30),
    )
    if "password=" in db.dsn.replace(" ", "").lower() or re.search(r"://[^/@]*:[^/@]*@", db.dsn):
        raise ConfigError("DB_DSN must not contain a password; use DB_PASSWORD_SOURCE")
    if db.pool_min > db.pool_max:
        raise ConfigError("DB_POOL_MIN must not exceed DB_POOL_MAX")

    s3: S3Settings | None = None
    if _opt(env, "S3_BUCKET"):
        store_id = _req(env, "ATTACHMENT_STORE_ID")
        if not _STORE_ID.match(store_id):
            raise ConfigError("ATTACHMENT_STORE_ID must be 1-63 characters of letters, digits, . _ -")
        style = env.get("S3_ADDRESSING_STYLE", "").strip() or "auto"
        if style not in ("auto", "path", "virtual"):
            raise ConfigError("S3_ADDRESSING_STYLE must be auto, path or virtual")
        access_key = _opt(env, "S3_ACCESS_KEY_ID")
        secret_key = _opt(env, "S3_SECRET_ACCESS_KEY")
        secret_file = _opt(env, "S3_SECRET_ACCESS_KEY_FILE")
        if secret_file and not secret_key:
            with open(secret_file, encoding="utf-8") as fh:
                secret_key = fh.read().strip()
        if bool(access_key) != bool(secret_key):
            raise ConfigError("set both S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY(_FILE), or neither")
        s3 = S3Settings(
            bucket=_req(env, "S3_BUCKET"),
            region=_opt(env, "S3_REGION"),
            endpoint_url=_opt(env, "S3_ENDPOINT_URL"),
            access_key_id=access_key,
            secret_access_key=secret_key,
            addressing_style=style,
            store_id=store_id,
            max_bytes=_int(env, "ATTACHMENT_MAX_BYTES", SCHEMA_MAX_ATTACHMENT_BYTES, 1, SCHEMA_MAX_ATTACHMENT_BYTES),
            conditional_put=_bool(env, "S3_CONDITIONAL_PUT", True),
        )

    env_id = _opt(env, "APP_ENV_ID") or "unknown"
    if not _ENV_ID.match(env_id):
        raise ConfigError("APP_ENV_ID must be 1-32 lowercase ASCII letters, digits or - (e.g. aws, onprem, gcp)")
    instance_id = re.sub(r"[^A-Za-z0-9._-]", "-", _opt(env, "APP_INSTANCE_ID") or _opt(env, "HOSTNAME") or "local")[:63]
    host = env.get("APP_HOST", "").strip() or "0.0.0.0"  # noqa: S104 - container listens on all interfaces
    app_port = _int(env, "APP_PORT", 8000, 1, 65535)
    if _int(env, "ADMIN_PORT", 8081, 1, 65535) == app_port:
        raise ConfigError("ADMIN_PORT must differ from APP_PORT")
    return Settings(
        env_label=_req(env, "APP_ENV_LABEL"),
        env_label_ja=_opt(env, "APP_ENV_LABEL_JA"),
        env_id=env_id,
        instance_id=instance_id,
        public_url=_opt(env, "APP_PUBLIC_URL"),
        host=host,
        port=app_port,
        room_default=_opt(env, "APP_DEFAULT_ROOM") or "consult-1",
        poll_interval_s=_float(env, "POLL_INTERVAL_S", 0.5, 0.05, 5.0),
        gap_grace_s=_float(env, "POLL_GAP_GRACE_S", 2.0, 0.2, 60.0),
        backlog_limit=_int(env, "BACKLOG_LIMIT", 200, 1, 2000),
        db=db,
        s3=s3,
        otlp_endpoint=_opt(env, "OTEL_EXPORTER_OTLP_ENDPOINT"),
        service_name=_opt(env, "OTEL_SERVICE_NAME") or "bravo-demo",
        log_level=(_opt(env, "LOG_LEVEL") or "INFO").upper(),
        forwarded_allow_ips=_opt(env, "FORWARDED_ALLOW_IPS") or "127.0.0.1",
        admin=AdminSettings(
            host=_opt(env, "ADMIN_HOST") or host,
            port=_int(env, "ADMIN_PORT", 8081, 1, 65535),
            token_file=_opt(env, "ADMIN_TOKEN_FILE"),
            retry_after_s=_int(env, "WRITE_BLOCK_RETRY_AFTER_S", 1, 1, 60),
            switch_wait_s=_float(env, "WRITE_BLOCK_SWITCH_WAIT_S", 5.0, 0.5, 30.0),
        ),
    )
