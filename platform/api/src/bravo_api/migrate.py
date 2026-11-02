"""Apply SQL migrations in order and record them. ``python -m bravo_api.migrate``.

- Runs as the database owner (``BRAVO_MIGRATE_DATABASE_URL``), never as a service role.
- One transaction per file; an advisory lock serialises concurrent runners.
- Applied files are checksummed; a changed file that was already applied is an error.
- Service-role passwords come from env (``BRAVO_{API,MCP,EXECUTOR,APPLY}_DB_PASSWORD``).
- No users are seeded (audit A3). The first member arrives through the Access first-login bootstrap
  (``BRAVO_BOOTSTRAP_EMAIL``/``BRAVO_BOOTSTRAP_TENANT`` on the API) or ``python -m bravo_api.admin grant``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from importlib import resources

import psycopg
from psycopg import sql

LOG = logging.getLogger("bravo_api.migrate")
LOCK_ID = 0x62726176  # "brav"
ROLE_PASSWORD_ENV = {
    "bravo_api": "BRAVO_API_DB_PASSWORD",
    "bravo_mcp": "BRAVO_MCP_DB_PASSWORD",
    "bravo_executor": "BRAVO_EXECUTOR_DB_PASSWORD",
    "bravo_apply": "BRAVO_APPLY_DB_PASSWORD",  # T31 apply worker (migration 0004)
}


def migration_files() -> list[tuple[str, str]]:
    root = resources.files("bravo_api") / "migrations"
    files = sorted((p.name, p.read_text()) for p in root.iterdir() if p.name.endswith(".sql"))
    return files


def apply(url: str, files: list[tuple[str, str]] | None = None) -> list[str]:
    files = files if files is not None else migration_files()
    applied_now: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                     version text PRIMARY KEY, checksum text NOT NULL,
                     applied_at timestamptz NOT NULL DEFAULT now())"""
            )
            conn.execute("REVOKE ALL ON schema_migrations FROM PUBLIC")
            done = {r[0]: r[1] for r in conn.execute("SELECT version, checksum FROM schema_migrations")}
            for name, body in files:
                checksum = hashlib.sha256(body.encode()).hexdigest()
                if name in done:
                    if done[name] != checksum:
                        raise RuntimeError(f"migration {name} changed after it was applied")
                    continue
                with conn.transaction():
                    conn.execute(body.encode())
                    conn.execute("INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)", (name, checksum))
                applied_now.append(name)
                LOG.info("applied %s", name)
            for role, env in ROLE_PASSWORD_ENV.items():
                password = os.environ.get(env)
                if password:
                    conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    return applied_now


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    url = os.environ.get("BRAVO_MIGRATE_DATABASE_URL")
    if not url:
        LOG.error("BRAVO_MIGRATE_DATABASE_URL is required")
        return 2
    applied = apply(url)
    if os.environ.get("BRAVO_SEED_DEV"):
        LOG.warning("BRAVO_SEED_DEV is ignored: dev users were removed (audit A3)")
    LOG.info("migrations up to date (%d applied now)", len(applied))
    return 0


if __name__ == "__main__":
    sys.exit(main())
