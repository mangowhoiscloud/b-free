"""FR-1 PostgreSQL collection through collector_ro, against a real postgres:16 container."""

from __future__ import annotations

import psycopg
import pytest

from bravo_collector.environments import Environment, PostgresSection
from bravo_collector.postgres import PostgresCollector
from bravo_collector.service import collect

pytestmark = pytest.mark.pg


def _collector(monkeypatch: pytest.MonkeyPatch, dsn: str) -> PostgresCollector:
    monkeypatch.setenv("BRAVO_TEST_COLLECTOR_DSN", dsn)
    return PostgresCollector(PostgresSection(dsn_env="BRAVO_TEST_COLLECTOR_DSN", app_role="app_writer",
                                             app_tables=("messages",)))


def test_snapshot_reports_settings_hba_and_public_select(pg_server, monkeypatch):
    data = _collector(monkeypatch, pg_server.collector_dsn).collect()
    conn = data["connection"]
    assert conn.status == "ok"
    assert conn.value["current_user"] == "collector_ro"
    assert conn.value["transaction_read_only"] == "on"
    settings = data["settings"]
    assert settings.status == "ok"
    assert settings.value["password_encryption"] == "scram-sha-256"  # noqa: S105 - setting value
    assert "ssl" in settings.value and "wal_level" in settings.value
    hba = data["hba_rules"]
    assert hba.status == "ok" and hba.value, "collector_ro must read pg_hba_file_rules"
    assert all("options" not in r for r in hba.value)
    assert data["archive_command"].status == "ok"
    table = data["app_tables"]["messages"]
    assert table["public_privileges"].value == {"SELECT": True, "INSERT": False, "UPDATE": False, "DELETE": False}
    acl = table["grants"].value["acl"]
    assert {"grantee": "PUBLIC", "privilege_type": "SELECT"} in acl
    assert {"grantee": "app_writer", "privilege_type": "INSERT"} in acl
    assert data["app_role"].value["rolname"] == "app_writer"
    # No password material anywhere.
    assert "rolpassword" not in str(data)


def test_collector_credential_cannot_write(pg_server):
    """README completion criterion: 수집 자격으로 쓰기가 거부된다."""
    with psycopg.connect(pg_server.collector_dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TABLE collector_probe (id int)")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO messages (body) VALUES ('x')")
        # Even with the session flag turned off, the role has no write privilege.
        conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO messages (body) VALUES ('x')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE messages SET body = 'y'")


def test_missing_dsn_env_is_an_error_item(monkeypatch):
    monkeypatch.delenv("BRAVO_TEST_MISSING_DSN", raising=False)
    data = PostgresCollector(PostgresSection(dsn_env="BRAVO_TEST_MISSING_DSN")).collect()
    assert data["connection"].status == "error"
    assert "BRAVO_TEST_MISSING_DSN" in data["connection"].error


def test_snapshot_serialises_and_marks_unconfigured(pg_server, monkeypatch):
    monkeypatch.setenv("BRAVO_TEST_COLLECTOR_DSN", pg_server.collector_dsn)
    env = Environment(environment_id="t", postgres=PostgresSection(dsn_env="BRAVO_TEST_COLLECTOR_DSN",
                                                                   app_tables=("messages",)))
    snap = collect(env).model_dump(mode="json")
    assert snap["schema_version"] == "collector/v1"
    assert snap["sources"]["aws"]["status"] == "not_configured"
    assert snap["sources"]["kubernetes"]["status"] == "not_configured"
    pg = snap["sources"]["postgres"]
    assert pg["status"] == "ok"
    assert pg["data"]["connection"]["status"] == "ok"
    assert snap["expires_at"] > snap["collected_at"]



def test_collector_ro_sql_takes_only_a_scram_verifier(pg_server) -> None:
    """Audit B19: the role password is stored from a client-side verifier; plaintext input is refused."""
    import subprocess

    import psycopg
    import pytest as _pytest
    from conftest import SQL_DIR, apply_collector_ro

    from bravo_collector.scram import scram_sha256_verifier

    with psycopg.connect(pg_server.superuser_dsn) as conn:
        stored = conn.execute("SELECT rolpassword FROM pg_authid WHERE rolname = 'collector_ro'").fetchone()[0]
    assert stored.startswith("SCRAM-SHA-256$4096:")
    cid = pg_server.container_id
    sql = (SQL_DIR / "collector_ro.sql").read_text(encoding="utf-8")
    with _pytest.raises(subprocess.CalledProcessError):
        apply_collector_ro(cid, sql, "plaintext-password-123")
    with psycopg.connect(pg_server.superuser_dsn) as conn:
        again = conn.execute("SELECT rolpassword FROM pg_authid WHERE rolname = 'collector_ro'").fetchone()[0]
    assert again == stored  # unchanged after the refused run
    v = scram_sha256_verifier("x" * 12, salt=b"\0" * 16)
    assert v.startswith("SCRAM-SHA-256$4096:AAAAAAAAAAAAAAAAAAAAAA==$")
