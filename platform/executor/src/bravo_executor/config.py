"""Executor settings, read once from the environment.

DSN values carry secrets. They are never logged and never put on spans; only the
resource IDs in ``resources`` appear in telemetry and in the effect ledger.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """A required setting is missing or malformed."""


def _req(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"missing required environment variable {name}")
    return value


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    platform_dsn: str
    src_dsn: str
    dst_dsn: str
    src_app_dsn: str
    dst_app_dsn: str
    src_observe_dsn: str
    dst_observe_dsn: str
    pgb_stats_dsn: str
    pgb_app_dsn: str
    pgb_admin_dsn: str
    pgb_ini_path: Path
    pgb_db_name: str
    chat_db: str
    pgb_src_target: str
    pgb_dst_target: str
    src_store_id: str
    dst_store_id: str
    fwd_sub: str = "bravo_fwd_sub"
    fwd_slot: str = "bravo_fwd_sub"
    rev_sub: str = "bravo_rev_sub"
    app_role: str = "app_writer"
    src_cluster_name: str = "src"
    dst_cluster_name: str = "dst"
    lag_max_bytes: int = 1_048_576
    long_tx_max_s: float = 0.2
    pause_timeout_s: float = 5.0
    barrier_timeout_s: float = 15.0
    subscription_timeout_s: float = 15.0
    probe_timeout_s: float = 10.0
    probe_room: str = "bravo-probe"
    lock_lease_s: float = 600.0
    freeze_lock_timeout_ms: int = 2000
    fault_crash_after: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        return cls(
            platform_dsn=_req(e, "PLATFORM_DB_DSN"),
            src_dsn=_req(e, "BRAVO_SRC_DSN"),
            dst_dsn=_req(e, "BRAVO_DST_DSN"),
            src_app_dsn=_req(e, "BRAVO_SRC_APP_DSN"),
            dst_app_dsn=_req(e, "BRAVO_DST_APP_DSN"),
            src_observe_dsn=_req(e, "BRAVO_SRC_OBSERVE_DSN"),
            dst_observe_dsn=_req(e, "BRAVO_DST_OBSERVE_DSN"),
            pgb_stats_dsn=_req(e, "BRAVO_PGB_STATS_DSN"),
            pgb_app_dsn=_req(e, "BRAVO_PGB_APP_DSN"),
            pgb_admin_dsn=_req(e, "BRAVO_PGB_ADMIN_DSN"),
            pgb_ini_path=Path(_req(e, "BRAVO_PGB_INI_PATH")),
            pgb_db_name=_req(e, "BRAVO_PGB_DB_NAME"),
            chat_db=_req(e, "BRAVO_CHAT_DB"),
            pgb_src_target=_req(e, "BRAVO_PGB_SRC_TARGET"),
            pgb_dst_target=_req(e, "BRAVO_PGB_DST_TARGET"),
            src_store_id=_req(e, "BRAVO_SRC_STORE_ID"),
            dst_store_id=_req(e, "BRAVO_DST_STORE_ID"),
            lag_max_bytes=int(_float(e, "BRAVO_LAG_MAX_BYTES", 1_048_576)),
            pause_timeout_s=_float(e, "BRAVO_PAUSE_TIMEOUT_S", 5.0),
            barrier_timeout_s=_float(e, "BRAVO_BARRIER_TIMEOUT_S", 15.0),
            subscription_timeout_s=_float(e, "BRAVO_SUBSCRIPTION_TIMEOUT_S", 15.0),
            probe_timeout_s=_float(e, "BRAVO_PROBE_TIMEOUT_S", 10.0),
            lock_lease_s=_float(e, "BRAVO_LOCK_LEASE_S", 600.0),
            freeze_lock_timeout_ms=int(_float(e, "BRAVO_FREEZE_LOCK_TIMEOUT_MS", 2000)),
            fault_crash_after=(e.get("BRAVO_FAULT_CRASH_AFTER") or "").strip() or None,
        )

    def app_dsn(self, side: str) -> str:
        return self.src_app_dsn if side == "src" else self.dst_app_dsn

    def pgb_target(self, side: str) -> str:
        return self.pgb_src_target if side == "src" else self.pgb_dst_target

    def store_id(self, side: str) -> str:
        return self.src_store_id if side == "src" else self.dst_store_id

    def cluster_name(self, side: str) -> str:
        return self.src_cluster_name if side == "src" else self.dst_cluster_name


class Resources:
    """Normalised resource IDs for the effect ledger and resource_locks (platform/executor/README.md)."""

    PGBOUNCER = "pgbouncer/0"
    PGBOUNCER_CONFIG = "pgbouncer/0/config"
    PGBOUNCER_POOL_PATH = "pgbouncer/0/db:chat"
    PLATFORM_RUN = "platform/db/run"

    @staticmethod
    def fence(side: str) -> str:
        return f"{side}/db/fence"

    @staticmethod
    def app_role(side: str) -> str:
        return f"{side}/db/role:app_writer"

    @staticmethod
    def app_grant(side: str) -> str:
        return f"{side}/db/grant:messages:app_writer"

    @staticmethod
    def app_sessions(side: str) -> str:
        return f"{side}/db/sessions:app_writer"

    @staticmethod
    def attachment_store(side: str) -> str:
        return f"{side}/db/attachment_store"

    @staticmethod
    def barrier(side: str) -> str:
        return f"{side}/db/table:bravo_barrier"

    @staticmethod
    def sequence(side: str, name: str) -> str:
        return f"{side}/db/sequence:{name}"

    @staticmethod
    def subscription(side: str, name: str) -> str:
        return f"{side}/db/subscription:{name}"

    @staticmethod
    def slot(side: str, name: str) -> str:
        return f"{side}/db/slot:{name}"

    @classmethod
    def lock_set(cls, settings: Settings) -> list[str]:
        """Every resource a cutover or failback can change. Both directions take the same set,
        so any two runs on this cell conflict (E2)."""
        out = [cls.PGBOUNCER, cls.PGBOUNCER_CONFIG, cls.PGBOUNCER_POOL_PATH]
        for side in ("src", "dst"):
            out += [cls.fence(side), cls.app_role(side), cls.app_grant(side), cls.app_sessions(side),
                    cls.barrier(side), cls.attachment_store(side)]
            out += [cls.sequence(side, n) for n in ("messages_id_seq", "bravo_barrier_id_seq")]
        out += [
            cls.subscription("dst", settings.fwd_sub), cls.subscription("src", settings.rev_sub),
            cls.slot("src", settings.fwd_slot), cls.slot("dst", settings.rev_sub),
        ]
        return sorted(set(out))
