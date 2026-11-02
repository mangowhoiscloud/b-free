"""D-04 attachment contract enforced by the database (real PostgreSQL 16, pilot schema)."""

from __future__ import annotations

import hashlib
import time
import uuid

import psycopg
import pytest

from .conftest import Stack


def _att_row(store: str = "store-src", room: str = "consult-1") -> dict[str, object]:
    sha = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    return {
        "logical_id": uuid.uuid4(), "idem_key": uuid.uuid4(), "room": room, "author": "tester",
        "object_key": f"att/{sha}", "sha256": sha, "size_bytes": 16, "content_type": "text/plain",
        "filename": "a.txt", "store_id": store,
    }


INSERT = """
INSERT INTO attachments (logical_id, idem_key, room, author, object_key, sha256, size_bytes,
                         content_type, filename, store_id)
VALUES (%(logical_id)s, %(idem_key)s, %(room)s, %(author)s, %(object_key)s, %(sha256)s, %(size_bytes)s,
        %(content_type)s, %(filename)s, %(store_id)s)
RETURNING id
"""


@pytest.fixture()
def app_src(stack: Stack):
    with psycopg.connect(stack.dsn("src", "app_writer"), autocommit=True) as conn:
        yield conn


def test_insert_on_active_store_succeeds(app_src: psycopg.Connection) -> None:
    assert app_src.execute(INSERT, _att_row()).fetchone() is not None


def test_insert_rejected_when_frozen(app_src: psycopg.Connection, superuser_src: psycopg.Connection) -> None:
    superuser_src.execute("UPDATE attachment_store SET frozen = true, freeze_watermark = now()")
    with pytest.raises(psycopg.Error) as exc:
        app_src.execute(INSERT, _att_row())
    assert exc.value.sqlstate == "ZB001"


def test_insert_rejected_on_store_mismatch(app_src: psycopg.Connection) -> None:
    with pytest.raises(psycopg.Error) as exc:
        app_src.execute(INSERT, _att_row(store="store-dst"))
    assert exc.value.sqlstate == "ZB002"


def test_object_key_must_be_content_hash(app_src: psycopg.Connection) -> None:
    row = _att_row()
    row["object_key"] = "att/" + "0" * 64
    with pytest.raises(psycopg.errors.CheckViolation):
        app_src.execute(INSERT, row)


def test_size_cap(app_src: psycopg.Connection) -> None:
    row = _att_row()
    row["size_bytes"] = 5 * 1024 * 1024 + 1
    with pytest.raises(psycopg.errors.CheckViolation):
        app_src.execute(INSERT, row)


def test_idempotency_key_unique(app_src: psycopg.Connection) -> None:
    row = _att_row()
    app_src.execute(INSERT, row)
    row2 = _att_row()
    row2["idem_key"] = row["idem_key"]
    with pytest.raises(psycopg.errors.UniqueViolation):
        app_src.execute(INSERT, row2)


def test_rows_immutable_and_delete_is_one_way(app_src: psycopg.Connection) -> None:
    att_id = app_src.execute(INSERT, _att_row()).fetchone()[0]  # type: ignore[index]
    # The app role may only update deleted_at (column privilege).
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_src.execute("UPDATE attachments SET store_id = 'x' WHERE id = %s", (att_id,))
    app_src.execute("UPDATE attachments SET deleted_at = now() WHERE id = %s", (att_id,))
    with pytest.raises(psycopg.Error) as exc:
        app_src.execute("UPDATE attachments SET deleted_at = NULL WHERE id = %s", (att_id,))
    assert exc.value.sqlstate == "ZB003"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_src.execute("DELETE FROM attachments WHERE id = %s", (att_id,))


def test_immutability_trigger_applies_to_owner_too(stack: Stack, app_src: psycopg.Connection,
                                                   superuser_src: psycopg.Connection) -> None:
    att_id = app_src.execute(INSERT, _att_row()).fetchone()[0]  # type: ignore[index]
    with pytest.raises(psycopg.Error) as exc:
        superuser_src.execute("UPDATE attachments SET sha256 = repeat('a', 64) WHERE id = %s", (att_id,))
    assert exc.value.sqlstate in ("ZB003", "23514")


def test_app_role_cannot_touch_store_control(app_src: psycopg.Connection) -> None:
    for sql in (
        "SELECT * FROM attachment_store",
        "UPDATE attachment_store SET frozen = false",
        "SET session_replication_role = replica",
        "ALTER TABLE attachments DISABLE TRIGGER attachments_guard_insert",
        "SELECT attachments_guard_insert()",
    ):
        with pytest.raises(psycopg.Error):
            app_src.execute(sql)


def test_freeze_waits_for_inflight_insert(stack: Stack, app_src: psycopg.Connection) -> None:
    """FOR SHARE in the guard: a freeze cannot slip between the check and the commit."""
    with psycopg.connect(stack.dsn("src", "executor_ctl"), autocommit=True) as ctl:
        with app_src.transaction():
            app_src.execute(INSERT, _att_row())
            ctl.execute("SET lock_timeout = '300ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                ctl.execute("UPDATE attachment_store SET frozen = true, freeze_watermark = now()")
        ctl.execute("UPDATE attachment_store SET frozen = true, freeze_watermark = now()")
    with pytest.raises(psycopg.Error) as exc:
        app_src.execute(INSERT, _att_row())
    assert exc.value.sqlstate == "ZB001"


def test_replicated_rows_bypass_guard_on_destination(stack: Stack, app_src: psycopg.Connection) -> None:
    """Rows written on src (store-src) replicate to dst, whose active store is store-dst."""
    body_key = uuid.uuid4()
    with app_src.transaction():
        att_id = app_src.execute(INSERT, _att_row()).fetchone()[0]  # type: ignore[index]
        msg_id = app_src.execute(
            "INSERT INTO messages (room, author, body, idem_key) VALUES ('consult-1', 'tester', 'hi', %s) RETURNING id",
            (body_key,),
        ).fetchone()[0]  # type: ignore[index]
        app_src.execute("INSERT INTO message_attachments (message_id, attachment_id) VALUES (%s, %s)", (msg_id, att_id))
    with psycopg.connect(stack.dsn("dst", "postgres"), autocommit=True) as dst:
        deadline = time.monotonic() + 20
        row = None
        while time.monotonic() < deadline:
            row = dst.execute(
                "SELECT a.store_id, s.active_store FROM message_attachments ma"
                " JOIN attachments a ON a.id = ma.attachment_id"
                " CROSS JOIN attachment_store s WHERE ma.message_id = %s",
                (msg_id,),
            ).fetchone()
            if row:
                break
            time.sleep(0.3)
        assert row == ("store-src", "store-dst")


def test_messages_columns_unchanged(stack: Stack, superuser_src: psycopg.Connection) -> None:
    """The Executor digests messages by a fixed column list (CONTRACT.md 3); keep it intact."""
    cols = [r[0] for r in superuser_src.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'messages' ORDER BY ordinal_position"
    ).fetchall()]
    assert cols == ["id", "room", "author", "body", "idem_key", "created_at"]
    pubs = {r[0] for r in superuser_src.execute(
        "SELECT tablename FROM pg_publication_tables WHERE pubname = 'bravo_fwd'").fetchall()}
    assert pubs == {"messages", "bravo_barrier", "attachments", "message_attachments"}


def test_roles_store_scram_verifiers_made_client_side(stack: Stack, superuser_src: psycopg.Connection) -> None:
    """B19: 00-bravo-init.sh hashes on the client; the server holds SCRAM verifiers only."""
    rows = superuser_src.execute(
        "SELECT rolname, rolpassword FROM pg_authid WHERE rolname = ANY(%s) ORDER BY rolname",
        (["app_writer", "executor_ctl", "executor_observe", "replicator"],),
    ).fetchall()
    assert [r[0] for r in rows] == ["app_writer", "executor_ctl", "executor_observe", "replicator"]
    for name, verifier in rows:
        assert verifier.startswith("SCRAM-SHA-256$4096:"), name
        assert stack.passwords[name] not in verifier
    # And the verifier is valid: SCRAM login with the real password works.
    for user in ("app_writer", "executor_ctl", "executor_observe"):
        with psycopg.connect(stack.dsn("src", user)) as conn:
            assert conn.execute("SELECT current_user").fetchone()[0] == user  # type: ignore[index]


def test_replication_password_stays_out_of_logs_and_catalog_reads(stack: Stack) -> None:
    """B19: repl-setup passes the replication password by environment (\\getenv), never argv; the
    CREATE SUBSCRIPTION session keeps it out of the server log; only superusers read subconninfo."""
    from .conftest import _docker

    for side in ("src", "dst"):
        logs = _docker("logs", f"{stack.tag}-{side}", check=False)
        assert stack.passwords["replicator"] not in logs.stdout + logs.stderr, side
        with psycopg.connect(stack.dsn(side, "executor_ctl")) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT subconninfo FROM pg_subscription")
    with psycopg.connect(stack.dsn("dst", "postgres")) as conn:
        assert conn.execute(
            "SELECT pid IS NOT NULL FROM pg_stat_subscription WHERE subname = 'bravo_fwd_sub' AND relid IS NULL"
        ).fetchone()[0] is True  # type: ignore[index]


def test_planted_regression_is_off_by_default(stack: Stack) -> None:
    """Item 28: the 심은 시나리오 grant exists only with BRAVO_PLANT_REGRESSION=1."""
    with psycopg.connect(stack.dsn("dst", "postgres")) as conn:
        assert conn.execute("SELECT has_table_privilege('public', 'messages', 'SELECT')").fetchone()[0] is False  # type: ignore[index]


def test_planted_regression_with_flag(stack: Stack) -> None:
    """Item 28: a dst initialised with BRAVO_PLANT_REGRESSION=1 has the planted grant."""
    from .conftest import PG_IMAGE, SCHEMA_DIR, _docker, _host_port, _wait_pg

    name = f"{stack.tag}-plant"
    pw = stack.passwords
    _docker(
        "run", "-d", "--name", name, "-p", "127.0.0.1::5432",
        "-e", "POSTGRES_DB=chat", "-e", f"POSTGRES_PASSWORD={pw['postgres']}",
        "-e", "BRAVO_SCHEMA_DIR=/bravo-schema/sql", "-e", "BRAVO_SIDE=dst", "-e", "BRAVO_PLANT_REGRESSION=1",
        "-e", f"APP_WRITER_PASSWORD={pw['app_writer']}", "-e", f"REPLICATOR_PASSWORD={pw['replicator']}",
        "-e", f"EXECUTOR_CTL_PASSWORD={pw['executor_ctl']}",
        "-e", f"EXECUTOR_OBSERVE_PASSWORD={pw['executor_observe']}",
        "-v", f"{SCHEMA_DIR / '00-bravo-init.sh'}:/docker-entrypoint-initdb.d/00-bravo-init.sh:ro",
        "-v", f"{SCHEMA_DIR / 'sql'}:/bravo-schema/sql:ro",
        PG_IMAGE, "postgres", "-c", "wal_level=logical",
    )
    try:
        port = _host_port(name, 5432)
        dsn = f"host=127.0.0.1 port={port} dbname=chat user=postgres password={pw['postgres']}"
        _wait_pg(dsn)
        deadline = time.monotonic() + 60
        while True:
            with psycopg.connect(dsn) as conn:
                if conn.execute("SELECT to_regclass('attachment_store')").fetchone()[0] is not None:  # type: ignore[index]
                    granted = conn.execute("SELECT has_table_privilege('public', 'messages', 'SELECT')").fetchone()[0]  # type: ignore[index]
                    if granted or time.monotonic() > deadline:
                        break
            time.sleep(0.5)
        assert granted is True
        logs = _docker("logs", name, check=False)
        assert "심은 시나리오 적용" in logs.stdout + logs.stderr
    finally:
        _docker("rm", "-f", "-v", name, check=False)
