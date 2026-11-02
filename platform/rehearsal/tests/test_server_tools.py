"""Server-side tools (controls_describe, evidence_read, db_inspect, db_rehearse) and their OpenBao reads.

Pure tests cover the SQL guard, redaction and fail-closed paths. The ``docker`` tests run against
real containers: an OpenBao dev server (KV v2) holding the DB credentials, and postgres:16 with a
read-only ``collector_ro`` role and a synthetic database role. They are skipped with
``docker_unavailable`` when there is no Docker daemon. No mocks.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from bravo_rehearsal.server_tools import (
    OpenBao,
    OpenBaoConfig,
    ServerTools,
    ServerToolSettings,
    ToolBlocked,
    redact,
    rehearse_problem,
    split_sql,
)

PG_IMAGE = "postgres:16.15@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a"
BAO_IMAGE = "openbao/openbao:2.7.2@sha256:5046911075e137e154ce49ab051502f4ec7cf367d484ae4a4dce665d9b90561f"
DB_QUERIES = frozenset({"server_version", "extensions", "tables_without_pk", "settings_replication"})


def settings(tmp_path: Path, **kw: Any) -> ServerToolSettings:
    base: dict[str, Any] = dict(gate_url=None, gate_secret_path=None, evidence_dir=tmp_path / "evidence",
                                db_ro_path=None, db_synthetic_path=None,
                                openbao=OpenBaoConfig(addr=None, kv_mount="secret", token_file=None, k8s_role=None,
                                                      k8s_mount="kubernetes", k8s_jwt_file=tmp_path / "jwt",
                                                      ca_cert=None))
    return ServerToolSettings(**{**base, **kw})


async def blocked(coro: Any) -> str:
    with pytest.raises(ToolBlocked) as exc:
        await coro
    return exc.value.reason


# SQL guard for db_rehearse --------------------------------------------------------------------


def test_split_sql_respects_quotes_comments_and_dollar_quotes() -> None:
    sql = """-- leading comment; with semicolon
    CREATE TABLE t (id int PRIMARY KEY, note text DEFAULT 'a;b''c');
    /* block ; /* nested ; */ still comment */ INSERT INTO "we;ird" VALUES (1);
    CREATE FUNCTION f() RETURNS int AS $body$ SELECT 1; $body$ LANGUAGE sql;
    SELECT $$;$$"""
    parts = split_sql(sql)
    assert len(parts) == 4
    assert parts[0].startswith("CREATE TABLE") and "'a;b''c'" in parts[0]
    assert parts[1].startswith("INSERT INTO \"we;ird\"") and "$body$ SELECT 1; $body$" in parts[2]
    for bad in ("SELECT 'open", "SELECT $x$ open", "/* open"):
        with pytest.raises(ToolBlocked):
            split_sql(bad)


@pytest.mark.parametrize(("stmt", "reason"), [
    ("COMMIT", "statement_not_allowed:commit"),
    ("begin", "statement_not_allowed:begin"),
    ("SET ROLE postgres", "statement_not_allowed:set"),
    ("DO $$ BEGIN COMMIT; END $$", "statement_not_allowed:do"),
    ("CREATE DATABASE x", "statement_not_allowed:create_database"),
    ("ALTER SYSTEM SET wal_level = logical", "statement_not_allowed:alter_system"),
    ("COPY t TO PROGRAM 'id'", "statement_not_allowed:copy"),
    ("SELECT pg_terminate_backend(1)", "function_not_allowed"),
    ("SELECT set_config('role', 'postgres', false)", "function_not_allowed"),
])
def test_rehearse_refuses_transaction_and_session_escapes(stmt: str, reason: str) -> None:
    assert rehearse_problem(stmt) == reason


@pytest.mark.parametrize("stmt", ["CREATE TABLE t (id int)", "ALTER TABLE t ADD COLUMN x int",
                                  "SELECT setval('s', 10)", "ALTER TABLE t REPLICA IDENTITY FULL"])
def test_rehearse_allows_plan_sql(stmt: str) -> None:
    assert rehearse_problem(stmt) is None


async def test_db_rehearse_refuses_before_any_connection(tmp_path: Path) -> None:
    tools = ServerTools(settings(tmp_path))
    assert await blocked(tools.db_rehearse({"sql": "SELECT 1; COMMIT; DROP TABLE t"})) == "statement_not_allowed:commit"
    assert await blocked(tools.db_rehearse({"sql": "SELECT E'\\';COMMIT;--'"})) == "escape_string_not_allowed"
    assert await blocked(tools.db_rehearse({"sql": ""})) == "bad_sql"
    assert await blocked(tools.db_rehearse({"sql": "SELECT 1"})) == "db_unconfigured"


def test_redact_hides_secret_like_strings() -> None:
    out = redact({"a": ["AKIAABCDEFGHIJKLMNOP", {"b": "password = 'hunter2hunter2'"}], "n": 3})
    assert "AKIA" not in json.dumps(out) and "hunter2" not in json.dumps(out) and out["n"] == 3


# Fail-closed paths without Docker -------------------------------------------------------------


async def test_server_tools_fail_closed_when_unconfigured(tmp_path: Path) -> None:
    tools = ServerTools(settings(tmp_path))
    inputs = {"source_env": "aws-src", "target_env": "onprem-dst"}
    assert await blocked(tools.controls_describe(inputs, {})) == "gate_unconfigured"
    tools = ServerTools(settings(tmp_path, gate_url="http://127.0.0.1:9"))
    assert await blocked(tools.controls_describe({}, {})) == "environments_unconfigured"
    assert await blocked(tools.controls_describe(inputs, {})) == "gate_unreachable"
    assert await blocked(tools.db_inspect(inputs, DB_QUERIES, {"query": "pg_shadow"})) == "query_not_allowed"
    assert await blocked(tools.db_inspect(inputs, DB_QUERIES, {"query": "extensions"})) == "db_unconfigured"
    tools = ServerTools(settings(tmp_path, db_ro_path="bravo/db/{env}/collector_ro"))
    assert await blocked(tools.db_inspect(inputs, DB_QUERIES, {"query": "extensions"})) == "openbao_unconfigured"


async def test_evidence_read_scope_redaction_and_symlinks(tmp_path: Path) -> None:
    store = tmp_path / "evidence"
    store.mkdir()
    (store / "inv:bucket-1.json").write_text(json.dumps({"policy": "s3", "note": "token = 'abcdefghij'"}))
    (store / "inv:other.json").write_text("{}")
    (tmp_path / "outside.json").write_text("{}")
    (store / "inv:link.json").symlink_to(tmp_path / "outside.json")
    tools = ServerTools(settings(tmp_path))
    scope = {"inv:bucket-1", "inv:link", "inv:gone"}
    out = await tools.evidence_read(scope, {"id": "inv:bucket-1"})
    assert out["content"]["policy"] == "s3" and "abcdefghij" not in json.dumps(out)
    assert await blocked(tools.evidence_read(scope, {"id": "inv:other"})) == "evidence_not_in_scope"
    assert await blocked(tools.evidence_read(scope, {"id": "../x"})) == "bad_evidence_id"
    assert await blocked(tools.evidence_read(scope, {"id": "inv:link"})) == "evidence_missing"
    assert await blocked(tools.evidence_read(scope, {"id": "inv:gone"})) == "evidence_missing"


# Real OpenBao + PostgreSQL --------------------------------------------------------------------


def _docker_reason() -> str | None:
    if shutil.which("docker") is None:
        return "docker_unavailable: docker CLI not found"
    r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,
                       timeout=20, check=False)
    return None if r.returncode == 0 else "docker_unavailable: daemon not reachable"


def _run(image: str, *args: str, env: dict[str, str] | None = None) -> tuple[str, int]:
    cmd = ["docker", "run", "-d", "--rm", "-P"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cid = subprocess.run([*cmd, image, *args], capture_output=True, text=True, timeout=300, check=True).stdout.strip()
    port_line = subprocess.run(["docker", "port", cid], capture_output=True, text=True, timeout=20,
                               check=True).stdout.splitlines()[0]
    return cid, int(port_line.rsplit(":", 1)[1])


@dataclass(frozen=True)
class Stack:
    tools: ServerTools
    superuser: dict[str, Any]
    passwords: tuple[str, ...]
    bao_addr: str
    root: str
    ids: dict[str, str]


REPO = Path(__file__).resolve().parents[3]
API_MIGRATIONS = REPO / "platform" / "api" / "src" / "bravo_api" / "migrations"
EVIDENCE_SQL = REPO / "platform" / "rehearsal" / "sql" / "rehearsal_evidence_ro.sql"
SYNTHETIC_INIT = REPO / "platform" / "rehearsal" / "sql" / "synthetic-init.sh"
REHEARSAL_POLICY = REPO / "deploy" / "pilot" / "bao" / "policies" / "platform-rehearsal.hcl"


def _psql(cid: str, db: str, sql: str, env: dict[str, str] | None = None) -> None:
    cmd = ["docker", "exec", "-i"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    subprocess.run([*cmd, cid, "psql", "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1", "-q", "--no-psqlrc",
                    "-f", "-"], input=sql, capture_output=True, text=True, timeout=120, check=True)


def _wait_pg(conninfo: dict[str, Any]) -> None:
    import psycopg

    deadline = time.monotonic() + 90
    while True:
        try:
            with psycopg.connect(**conninfo, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    """Real OpenBao (AppRole + the pilot policy file), postgres:16 with the platform/api migrations and the
    evidence role SQL, and a synthetic DB built by synthetic-init.sh."""
    reason = _docker_reason()
    if reason:
        pytest.skip(reason)
    import psycopg

    tmp = tmp_path_factory.mktemp("stack")
    root, su_pw, ro_pw, rw_pw, ev_pw, syn_pw = (secrets.token_urlsafe(18) for _ in range(6))
    cids: list[str] = []
    try:
        bao_cid, bao_port = _run(BAO_IMAGE, "server", "-dev", "-dev-listen-address=0.0.0.0:8200",
                                 env={"BAO_DEV_ROOT_TOKEN_ID": root})
        cids.append(bao_cid)
        pg_cid, pg_port = _run(PG_IMAGE, env={"POSTGRES_PASSWORD": su_pw, "POSTGRES_DB": "chat"})
        cids.append(pg_cid)
        syn_cmd = ["docker", "run", "-d", "--rm", "-P", "-e", f"POSTGRES_PASSWORD={syn_pw}", "-e",
                   "POSTGRES_DB=synthetic", "-e", f"REHEARSE_RW_PASSWORD={rw_pw}", "-v",
                   f"{SYNTHETIC_INIT}:/docker-entrypoint-initdb.d/10-synthetic.sh:ro", PG_IMAGE]
        syn_cid = subprocess.run(syn_cmd, capture_output=True, text=True, timeout=300, check=True).stdout.strip()
        cids.append(syn_cid)
        syn_port = int(subprocess.run(["docker", "port", syn_cid], capture_output=True, text=True, timeout=20,
                                      check=True).stdout.splitlines()[0].rsplit(":", 1)[1])
        su = {"host": "127.0.0.1", "port": pg_port, "dbname": "chat", "user": "postgres", "password": su_pw}
        _wait_pg(su)
        # The init script runs on a temporary server first; wait until rehearse_rw can log in on TCP.
        _wait_pg({"host": "127.0.0.1", "port": syn_port, "dbname": "synthetic", "user": "rehearse_rw",
                  "password": rw_pw})
        bao = f"http://127.0.0.1:{bao_port}"
        deadline = time.monotonic() + 60
        while httpx.get(f"{bao}/v1/sys/health", timeout=2).status_code != 200:
            if time.monotonic() > deadline:
                raise RuntimeError("openbao not ready")
            time.sleep(0.5)
        with psycopg.connect(**su, autocommit=True) as conn:
            conn.execute("CREATE TABLE messages (id bigserial PRIMARY KEY, body text NOT NULL)")
            conn.execute("CREATE TABLE audit_log (at timestamptz, line text)")  # no primary key
            # Same rights as platform/collector/sql/collector_ro.sql: catalog and stats reads, session read-only.
            conn.execute(f"CREATE ROLE collector_ro LOGIN PASSWORD '{ro_pw}'")
            conn.execute("GRANT pg_read_all_settings, pg_read_all_stats, pg_monitor TO collector_ro")
            conn.execute("ALTER ROLE collector_ro SET default_transaction_read_only = on")
            conn.execute("CREATE DATABASE platform")
        # Platform DB: the real API migrations, then the shipped evidence role SQL (password via \\getenv).
        for f in sorted(API_MIGRATIONS.glob("*.sql")):
            _psql(pg_cid, "platform", f.read_text())
        _psql(pg_cid, "platform", EVIDENCE_SQL.read_text(), env={"BRAVO_REHEARSAL_RO_PASSWORD": ev_pw})
        ids = {k: str(uuid.uuid4()) for k in ("plan", "other_plan", "gate", "other_gate", "src", "dst",
                                              "src2", "dst2")}
        with psycopg.connect(**{**su, "dbname": "platform"}, autocommit=True) as conn:  # superuser: RLS bypassed
            pid = conn.execute("INSERT INTO principals (iss, sub, email) VALUES ('t', 's', 'a@b') RETURNING id"
                               ).fetchone()[0]
            for tenant, plan, gate, src, dst in (("t1", ids["plan"], ids["gate"], ids["src"], ids["dst"]),
                                                 ("t2", ids["other_plan"], ids["other_gate"], ids["src2"],
                                                  ids["dst2"])):
                conn.execute("INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)", (tenant, tenant))
                for env_id, name, role in ((src, "aws-src", "source"), (dst, "onprem-dst", "target")):
                    conn.execute("INSERT INTO environments (environment_id, tenant_id, name, role, engine, secret_ref,"
                                 " created_by) VALUES (%s, %s, %s, %s, 'postgres', %s, %s)",
                                 (env_id, tenant, name, role, f"tenants/{tenant}/{name}", pid))
                conn.execute("INSERT INTO plans (plan_id, tenant_id, workload, source_environment_id, "
                             "target_environment_id, state, created_by, analysis) VALUES "
                             "(%s, %s, 'chat', %s, %s, 'PROPOSE', %s, %s)",
                             (plan, tenant, src, dst, pid, json.dumps({"gaps": ["open_bucket"],
                                                                       "note": "password = 'not-a-real-1'"})))
                conn.execute("INSERT INTO gate_verdicts (verdict_id, tenant_id, plan_id, plan_revision, verdict, "
                             "reasons, controls, readiness, input_digest, rule_version, requested_by, channel) "
                             "VALUES (%s, %s, %s, 1, 'BLOCK', ARRAY['ISO-01'], %s, '{}', 'sha256:x', 'r1', %s, "
                             "'console')", (gate, tenant, plan, json.dumps([{"control_id": "ISO-01",
                                                                             "dst": "FAIL"}]), pid))
        # OpenBao: KV secrets at the pilot paths, the pilot policy file, an AppRole like rehearsal-bao.sh setup.
        headers = {"X-Vault-Token": root}
        for path, data in (
            ("envs/aws-src/collector-ro", {"host": "127.0.0.1", "port": str(pg_port), "dbname": "chat",
                                           "user": "collector_ro", "password": ro_pw, "sslmode": "disable"}),
            ("platform/rehearsal-synthetic-db", {"host": "127.0.0.1", "port": str(syn_port), "dbname": "synthetic",
                                                 "user": "rehearse_rw", "password": rw_pw, "sslmode": "disable"}),
            ("platform/rehearsal-evidence-db", {"host": "127.0.0.1", "port": str(pg_port), "dbname": "platform",
                                                "user": "bravo_rehearsal_ro", "password": ev_pw,
                                                "sslmode": "disable"}),
            ("platform/cloudflare", {"api_token": "never-readable-by-rehearsal"}),
        ):
            httpx.post(f"{bao}/v1/secret/data/{path}", json={"data": data}, headers=headers,
                       timeout=5).raise_for_status()
        httpx.post(f"{bao}/v1/sys/auth/approle", json={"type": "approle"}, headers=headers,
                   timeout=5).raise_for_status()
        httpx.put(f"{bao}/v1/sys/policies/acl/platform-rehearsal", json={"policy": REHEARSAL_POLICY.read_text()},
                  headers=headers, timeout=5).raise_for_status()
        httpx.post(f"{bao}/v1/auth/approle/role/rehearsal", headers=headers, timeout=5, json={
            "token_policies": ["platform-rehearsal"], "token_type": "service", "token_period": "1h",
            "bind_secret_id": True, "secret_id_ttl": "60s", "secret_id_num_uses": 1}).raise_for_status()
        role_id = httpx.get(f"{bao}/v1/auth/approle/role/rehearsal/role-id", headers=headers,
                            timeout=5).json()["data"]["role_id"]
        secret_id = httpx.post(f"{bao}/v1/auth/approle/role/rehearsal/secret-id", headers=headers,
                               timeout=5).json()["data"]["secret_id"]
        cfg = OpenBaoConfig(addr=bao, kv_mount="secret", role_id=role_id, secret_id=secret_id)
        tools = ServerTools(settings(tmp, db_ro_path="envs/{env}/collector-ro",
                                     db_synthetic_path="platform/rehearsal-synthetic-db",
                                     evidence_db_path="platform/rehearsal-evidence-db", openbao=cfg))
        import asyncio

        asyncio.run(tools.bao.login())
        yield Stack(tools=tools, superuser=su, passwords=(root, su_pw, ro_pw, rw_pw, ev_pw, syn_pw, secret_id),
                    bao_addr=bao, root=root, ids={**ids, "role_id": role_id, "secret_id": secret_id})
    finally:
        for cid in cids:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=60, check=False)


INPUTS = {"source_env": "aws-src", "target_env": "onprem-dst"}


@pytest.mark.docker
async def test_openbao_approle_login_is_single_use_and_policy_is_narrow(stack: Stack) -> None:
    bao: OpenBao = stack.tools.bao
    assert "BRAVO_BAO_SECRET_ID" not in os.environ and bao._secret_id is None  # dropped after login
    assert (await bao.read("envs/aws-src/collector-ro"))["user"] == "collector_ro"
    # The pilot policy (deploy/pilot/bao/policies/platform-rehearsal.hcl) covers nothing else.
    assert await blocked(bao.read("platform/cloudflare")) == "openbao_denied"
    assert await blocked(bao.read("envs/aws-src/chat-admin")) == "openbao_denied"
    # The secret_id was single-use: a second login with it is refused.
    again = OpenBao(OpenBaoConfig(addr=stack.bao_addr, kv_mount="secret", role_id=stack.ids["role_id"],
                                  secret_id=stack.ids["secret_id"]))
    assert await blocked(again.login()) == "openbao_denied"
    assert await blocked(again.read("envs/aws-src/collector-ro")) == "openbao_denied"
    await bao.renew()  # periodic token renews
    assert bao._period > 0 and (await bao.read("platform/rehearsal-synthetic-db"))["user"] == "rehearse_rw"


@pytest.mark.docker
async def test_db_inspect_reads_catalog_through_collector_ro_without_secrets(stack: Stack) -> None:
    out = await stack.tools.db_inspect(INPUTS, DB_QUERIES, {"query": "tables_without_pk"})
    assert out["rows"] == [["public", "audit_log"]] and out["truncated"] is False
    version = await stack.tools.db_inspect(INPUTS, DB_QUERIES, {"query": "server_version"})
    assert version["rows"][0][0].startswith("16.")
    text = json.dumps([out, version])
    assert not any(pw in text for pw in stack.passwords)  # D-26: no secret values in results
    # The destination has no credentials in OpenBao: refused, nothing simulated.
    assert await blocked(stack.tools.db_inspect(INPUTS, DB_QUERIES, {"query": "extensions",
                                                                     "target": "destination"})) == "secret_missing"


@pytest.mark.docker
async def test_db_rehearse_runs_in_one_transaction_and_always_rolls_back(stack: Stack) -> None:
    import psycopg

    sql = ("CREATE TABLE orders (id bigserial PRIMARY KEY, total numeric);"
           "INSERT INTO orders (total) VALUES (1), (2);"
           "ALTER TABLE orders REPLICA IDENTITY FULL")
    out = await stack.tools.db_rehearse({"sql": sql})
    assert out["rolled_back"] is True
    assert [(s["status"], s.get("rowcount")) for s in out["statements"]] == [("ok", -1), ("ok", 2), ("ok", -1)]
    secret = await stack.tools.bao.read("platform/rehearsal-synthetic-db")
    with psycopg.connect(host=secret["host"], port=int(secret["port"]), dbname=secret["dbname"], user=secret["user"],
                         password=secret["password"]) as conn:
        assert conn.execute("SELECT to_regclass('app.orders')").fetchone() == (None,)  # nothing persisted
    out = await stack.tools.db_rehearse({"sql": "CREATE TABLE x (id int); INSERT INTO missing VALUES (1); "
                                                "CREATE TABLE never_reached (id int)"})
    assert [s["status"] for s in out["statements"]] == ["ok", "error"]
    assert out["statements"][1]["sqlstate"] == "42P01"  # undefined_table; the rest is not run
    refused = await blocked(stack.tools.db_rehearse({"sql": "DROP DATABASE chat"}))  # before connecting
    assert refused == "statement_not_allowed:drop_database"


@pytest.mark.docker
async def test_evidence_read_from_platform_db_is_tenant_and_plan_bound(stack: Stack) -> None:
    import psycopg

    ids, tools = stack.ids, stack.tools
    scope = {f"gate:{ids['gate']}", f"gate:{ids['other_gate']}", f"analysis:{ids['plan']}",
             f"analysis:{ids['other_plan']}", f"rehearsal:{uuid.uuid4()}"}
    out = await tools.evidence_read(scope, {"id": f"gate:{ids['gate']}"}, tenant_id="t1", plan_id=ids["plan"])
    assert out["source"] == "platform_db" and out["content"]["verdict"] == "BLOCK"
    assert out["content"]["reasons"] == ["ISO-01"] and out["content"]["plan_id"] == ids["plan"]
    analysis = await tools.evidence_read(scope, {"id": f"analysis:{ids['plan']}"}, tenant_id="t1", plan_id=ids["plan"])
    assert analysis["content"]["analysis"]["gaps"] == ["open_bucket"]
    assert "not-a-real-1" not in json.dumps(analysis)  # redacted
    # Another tenant's verdict: RLS hides it even with the right plan id; another plan: refused.
    assert await blocked(tools.evidence_read(scope, {"id": f"gate:{ids['other_gate']}"}, tenant_id="t1",
                                             plan_id=ids["other_plan"])) == "evidence_missing"
    assert await blocked(tools.evidence_read(scope, {"id": f"gate:{ids['gate']}"}, tenant_id="t2",
                                             plan_id=ids["plan"])) == "evidence_missing"
    assert await blocked(tools.evidence_read(scope, {"id": f"analysis:{ids['other_plan']}"}, tenant_id="t1",
                                             plan_id=ids["plan"])) == "evidence_not_in_scope"
    assert await blocked(tools.evidence_read({"gate:nope"}, {"id": "gate:nope"}, tenant_id="t1",
                                             plan_id=ids["plan"])) == "bad_evidence_id"
    # The role itself: column grants only, read-only sessions.
    secret = await tools.bao.read("platform/rehearsal-evidence-db")
    conn = psycopg.connect(host=secret["host"], port=int(secret["port"]), dbname=secret["dbname"],
                           user=secret["user"], password=secret["password"])
    with conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):  # session default: read-only
            conn.execute("INSERT INTO tenants (tenant_id, name) VALUES ('x', 'x')")
        conn.rollback()
        conn.execute("SET default_transaction_read_only = off")  # a session can lift it; the grants still hold
        conn.commit()
        for sql in ("SELECT collect_snapshot FROM plans", "SELECT * FROM approvals",
                    "INSERT INTO tenants (tenant_id, name) VALUES ('x', 'x')", "UPDATE plans SET analysis = NULL"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
            conn.rollback()


@pytest.mark.docker
async def test_synthetic_init_script_role_cannot_escape(stack: Stack) -> None:
    import psycopg

    secret = await stack.tools.bao.read("platform/rehearsal-synthetic-db")
    with psycopg.connect(host=secret["host"], port=int(secret["port"]), dbname=secret["dbname"], user=secret["user"],
                         password=secret["password"], autocommit=True) as conn:
        assert conn.execute("SHOW search_path").fetchone() == ("app",)
        role = conn.execute("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"
                            ).fetchone()
        assert role == (False, False, False)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE public.x (id int)")
