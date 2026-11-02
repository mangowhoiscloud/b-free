"""[databases] target rewrite (pure text functions + atomic write on the real filesystem)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from bravo_executor.pgbouncer import IniError, read_database_target, rewrite_database_target, write_ini_atomically

SRC = "host=src-db port=5432 dbname=chat"
DST = "host=dst-db port=5432 dbname=chat"
INI = f"""\
;; comment
[databases]
; chat = host=commented-out
chat = {SRC}
other = host=x dbname=other

[pgbouncer]
listen_port = 6432
chat = not-a-database-entry
"""


def test_read_target_ignores_comments_and_other_sections() -> None:
    assert read_database_target(INI, "chat") == SRC
    assert read_database_target(INI, "missing") is None


def test_rewrite_changes_only_the_target_line() -> None:
    out = rewrite_database_target(INI, "chat", DST)
    assert read_database_target(out, "chat") == DST
    before, after = INI.splitlines(), out.splitlines()
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert changed == [3]
    assert "chat = not-a-database-entry" in out
    assert "; chat = host=commented-out" in out


def test_rewrite_requires_exactly_one_entry() -> None:
    with pytest.raises(IniError):
        rewrite_database_target(INI, "missing", DST)
    with pytest.raises(IniError):
        rewrite_database_target(INI + "[databases]\nchat = host=y\n", "chat", DST)


def test_rewrite_rejects_multiline_target() -> None:
    with pytest.raises(IniError):
        rewrite_database_target(INI, "chat", "host=dst-db\nadmin_users = attacker")


def test_atomic_write_replaces_file_and_sets_mode(tmp_path: Path) -> None:
    path = tmp_path / "pgbouncer.ini"
    path.write_text(INI, encoding="utf-8")
    write_ini_atomically(path, rewrite_database_target(INI, "chat", DST), mode=0o664)
    assert read_database_target(path.read_text(encoding="utf-8"), "chat") == DST
    assert stat.S_IMODE(path.stat().st_mode) == 0o664
    assert [p.name for p in tmp_path.iterdir()] == ["pgbouncer.ini"]
