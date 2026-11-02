"""PostgreSQL read-only collection (FR-1) through the ``collector_ro`` role (``sql/collector_ro.sql``).

The session is forced read-only (``default_transaction_read_only=on``) on top of the role's own
setting and grants, so a collector bug cannot write. No password or secret value is read.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .environments import PostgresSection
from .model import Item, failed, observe

SETTINGS: tuple[str, ...] = (
    "ssl",
    "ssl_min_protocol_version",
    "password_encryption",
    "log_connections",
    "log_disconnections",
    "log_statement",
    "shared_preload_libraries",
    "wal_level",
    "archive_mode",
    "pgaudit.log",
    "pgaudit.log_role",
)
TABLE_PRIVILEGES: tuple[str, ...] = ("SELECT", "INSERT", "UPDATE", "DELETE")
_SETTINGS_SQL = "SELECT name, setting FROM pg_settings WHERE name = ANY(%s) ORDER BY name"
# Only whether archiving is configured; the command text can hold credentials.
_ARCHIVE_SQL = (
    "SELECT coalesce(current_setting('archive_command', true), '') NOT IN ('', '(disabled)') AS configured"
)


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _rows(conn: psycopg.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)  # type: ignore[arg-type]
        return [{k: _iso(v) for k, v in r.items()} for r in cur.fetchall()]


class PostgresCollector:
    def __init__(self, section: PostgresSection) -> None:
        self.section = section

    def connect(self) -> psycopg.Connection:
        dsn = os.environ.get(self.section.dsn_env)
        if not dsn:
            raise RuntimeError(f"environment variable {self.section.dsn_env} is not set")
        return psycopg.connect(
            dsn,
            connect_timeout=self.section.connect_timeout_s,
            options="-c default_transaction_read_only=on",
            autocommit=True,
            application_name="bravo-collector",
        )

    def collect(self) -> dict[str, Any]:
        try:
            conn = self.connect()
        except Exception as exc:  # noqa: BLE001 - connection failure becomes an error item
            item = failed(exc)
            return {"connection": item}
        try:
            return self._collect(conn)
        finally:
            conn.close()

    def _collect(self, conn: psycopg.Connection) -> dict[str, Any]:
        q: Callable[[str, tuple[Any, ...]], list[dict[str, Any]]] = lambda sql, p=(): _rows(conn, sql, p)  # noqa: E731
        data: dict[str, Any] = {
            "connection": observe(lambda: q(
                "SELECT current_setting('server_version') AS server_version, current_user AS current_user, "
                "current_database() AS database, current_setting('transaction_read_only') AS transaction_read_only",
                (),
            )[0]),
            "settings": observe(lambda: {r["name"]: r["setting"] for r in q(_SETTINGS_SQL, (list(SETTINGS),))}),
            "archive_command": observe(lambda: {"configured": q(_ARCHIVE_SQL, ())[0]["configured"]}),
            "own_ssl": observe(lambda: (q(
                "SELECT ssl, version, cipher, bits FROM pg_stat_ssl WHERE pid = pg_backend_pid()", ()) or [None])[0]),
            "hba_rules": observe(lambda: q(
                "SELECT line_number, type, database, user_name, address, netmask, auth_method, error "
                "FROM pg_hba_file_rules ORDER BY line_number", ())),
            "extensions": observe(lambda: q("SELECT extname, extversion FROM pg_extension ORDER BY extname", ())),
            "roles": observe(lambda: q(
                "SELECT rolname, rolsuper, rolcanlogin, rolreplication, rolbypassrls, rolvaliduntil "
                "FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY rolname", ())),
            "archiver": observe(lambda: q(
                "SELECT archived_count, last_archived_time, failed_count, last_failed_time, stats_reset "
                "FROM pg_stat_archiver", ())[0]),
        }
        data["app_tables"] = {t: self._table(conn, t) for t in sorted(self.section.app_tables)}
        if self.section.app_role:
            role = self.section.app_role
            data["app_role"] = observe(lambda: (q(
                "SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolvaliduntil FROM pg_roles WHERE rolname = %s",
                (role,)) or [{"rolname": role, "missing": True}])[0])
        return data

    def _table(self, conn: psycopg.Connection, table: str) -> dict[str, Item]:
        def public_privileges() -> dict[str, bool]:
            out = {}
            for priv in TABLE_PRIVILEGES:
                r = _rows(conn, "SELECT has_table_privilege('public', %s::regclass, %s) AS allowed", (table, priv))
                out[priv] = bool(r[0]["allowed"])
            return out

        def grants() -> dict[str, Any]:
            # pg_class.relacl is readable by every role (information_schema hides other roles' grants).
            owner = _rows(conn, "SELECT pg_get_userbyid(relowner) AS owner FROM pg_class WHERE oid = %s::regclass",
                          (table,))[0]["owner"]
            acl = _rows(
                conn,
                "SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END AS grantee, "
                "a.privilege_type FROM pg_class c, aclexplode(c.relacl) a WHERE c.oid = %s::regclass "
                "ORDER BY 1, 2",
                (table,),
            )
            return {"owner": owner, "acl": acl}

        return {"public_privileges": observe(public_privileges), "grants": observe(grants)}
