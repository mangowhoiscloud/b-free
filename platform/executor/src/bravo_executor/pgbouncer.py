"""PgBouncer admin console and the [databases] target (CONTRACT.md 4).

The admin console accepts only the simple query protocol. psycopg sends a query
without parameters through the simple protocol, and autocommit avoids BEGIN.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
_ADMIN_COMMANDS = frozenset({"PAUSE", "RESUME", "RELOAD"})
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


class IniError(ValueError):
    """pgbouncer.ini does not have the expected [databases] entry."""


def _entry_re(db_name: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{re.escape(db_name)}\s*=\s*(?P<target>.*?)\s*$")


def read_database_target(ini_text: str, db_name: str) -> str | None:
    """Return the connect string of ``db_name`` in [databases], or None."""
    section: str | None = None
    entry = _entry_re(db_name)
    for line in ini_text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            section = m.group("name").strip().lower()
            continue
        if section == "databases" and not line.lstrip().startswith((";", "#")):
            em = entry.match(line)
            if em:
                return em.group("target")
    return None


def rewrite_database_target(ini_text: str, db_name: str, target: str) -> str:
    """Replace exactly one ``db_name = ...`` line in [databases]; keep every other byte."""
    if "\n" in target or "\r" in target:
        raise IniError("target must be a single line")
    section: str | None = None
    entry = _entry_re(db_name)
    out: list[str] = []
    replaced = 0
    for line in ini_text.splitlines(keepends=True):
        m = _SECTION_RE.match(line)
        if m:
            section = m.group("name").strip().lower()
        elif section == "databases" and not line.lstrip().startswith((";", "#")) and entry.match(line):
            newline = "\n" if line.endswith("\n") else ""
            out.append(f"{db_name} = {target}{newline}")
            replaced += 1
            continue
        out.append(line)
    if replaced != 1:
        raise IniError(f"expected one [databases] entry for {db_name!r}, found {replaced}")
    return "".join(out)


def write_ini_atomically(path: Path, text: str, mode: int = 0o664) -> None:
    """Write via a temp file in the same directory and rename, so PgBouncer never reads a partial file."""
    fd, tmp_name = tempfile.mkstemp(prefix=".pgbouncer.ini.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


class PgBouncerAdmin:
    """One admin-console session. Commands are restricted to PAUSE/RESUME/RELOAD and SHOW."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    @classmethod
    async def connect(cls, dsn: str, timeout_s: float = 5.0) -> PgBouncerAdmin:
        conn = await AsyncConnection.connect(dsn, autocommit=True, connect_timeout=max(1, int(timeout_s)))
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    async def command(self, verb: str, db_name: str | None = None) -> None:
        verb = verb.upper()
        if verb not in _ADMIN_COMMANDS:
            raise ValueError(f"admin command {verb!r} is not allowed")
        if db_name is not None and not _DB_NAME_RE.match(db_name):
            raise ValueError(f"invalid database name {db_name!r}")
        sql = verb if db_name is None else f"{verb} {db_name}"
        await self._conn.execute(sql.encode())

    async def show_databases(self) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(b"SHOW DATABASES")
            return list(await cur.fetchall())

    async def database(self, db_name: str) -> dict[str, Any] | None:
        for row in await self.show_databases():
            if row.get("name") == db_name:
                return row
        return None
