"""Real engines only: two postgres:16 containers (plaintext and TLS-enforced) and, when named by the
operator, a real object store (Rook-Ceph RGW or S3) through BRAVO_TEST_S3_*.

Skips with a clear reason when docker is unavailable. Containers are removed at session end.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

PG_IMAGE = "postgres:16.15@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a"

PROBE_SQL = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gate_ro') THEN
    CREATE ROLE gate_ro LOGIN;
  END IF;
END $$;
ALTER ROLE gate_ro PASSWORD {pw};
GRANT pg_read_all_settings, pg_read_all_stats, pg_monitor TO gate_ro;
GRANT SELECT ON pg_catalog.pg_hba_file_rules TO gate_ro;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_hba_file_rules() TO gate_ro;
ALTER ROLE gate_ro SET default_transaction_read_only = on;
CREATE TABLE IF NOT EXISTS messages (id bigserial PRIMARY KEY, body text NOT NULL);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_writer') THEN
    CREATE ROLE app_writer LOGIN;
  END IF;
END $$;
ALTER ROLE app_writer PASSWORD {app_pw};
GRANT SELECT, INSERT ON messages TO app_writer;
"""


def _docker(*args: str, timeout: int = 300) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,  # noqa: S603,S607
                          check=True).stdout.strip()


def _docker_reason() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI not found"
    r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True,  # noqa: S603,S607
                       text=True, timeout=20, check=False)
    return None if r.returncode == 0 else f"docker daemon unavailable: {r.stderr.strip()[:200]}"


def _host_port(cid: str, port: str) -> int:
    return int(_docker("port", cid, port).splitlines()[0].rsplit(":", 1)[1])


@dataclass(frozen=True)
class Pg:
    superuser_dsn: str
    probe_dsn: str
    ca_file: str | None


def _make_certs(d: Path) -> Path:
    def ossl(*a: str) -> None:
        subprocess.run(["openssl", *a], cwd=d, capture_output=True, check=True, timeout=60)  # noqa: S603,S607

    ossl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=bravo-test-ca",
         "-keyout", "ca.key", "-out", "ca.crt")
    ossl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost", "-keyout", "server.key",
         "-out", "server.csr")
    (d / "san.ext").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    ossl("x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
         "-days", "2", "-extfile", "san.ext", "-out", "server.crt")
    (d / "pg_hba.conf").write_text(
        "local all all scram-sha-256\nhostssl all all all scram-sha-256\nhost all all all reject\n")
    for f in d.iterdir():
        f.chmod(0o644)
    # The directory is bind-mounted as /certs and the server (uid postgres, not the host user) reads
    # /certs/pg_hba.conf directly. pytest creates tmp dirs 0700, so on Docker Engine (Linux) postgres
    # could not enter /certs and exited at startup. Docker Desktop's file sharing hides this.
    d.chmod(0o755)
    return d / "ca.crt"


def _start_pg(tls_dir: Path | None, *, ident: bool = False) -> tuple[str, Pg]:
    su_pw, ro_pw, app_pw = (secrets.token_urlsafe(18) for _ in range(3))
    # No --rm: if the server exits during startup, the container (and its log) stays for the error below.
    args = ["run", "-d", "-P", "-e", f"POSTGRES_PASSWORD={su_pw}", "-e", "POSTGRES_DB=chat",
            "-e", "POSTGRES_INITDB_ARGS=--auth-local=scram-sha-256 --auth-host=scram-sha-256"]
    if tls_dir is None:
        args += [PG_IMAGE, "postgres", "-c", "password_encryption=scram-sha-256"]
    else:
        args += ["-v", f"{tls_dir}:/certs:ro", "--entrypoint", "bash", PG_IMAGE, "-c",
                 "install -o postgres -m 600 /certs/server.key /tmp/server.key && "
                 "install -o postgres -m 644 /certs/server.crt /tmp/server.crt && "
                 "exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt "
                 "-c ssl_key_file=/tmp/server.key -c ssl_min_protocol_version=TLSv1.2 "
                 "-c hba_file=/certs/pg_hba.conf -c log_connections=on"
                 + (" -c ident_file=/certs/pg_ident.conf" if ident else "")]
    cid = _docker(*args)
    try:
        port = _host_port(cid, "5432/tcp")
        host = "localhost" if tls_dir else "127.0.0.1"
        su = f"host={host} port={port} dbname=chat user=postgres password={su_pw}"
        deadline = time.monotonic() + 90
        while True:
            try:
                with psycopg.connect(su, connect_timeout=2, autocommit=True) as conn:
                    conn.execute(sql.SQL(PROBE_SQL).format(pw=sql.Literal(ro_pw), app_pw=sql.Literal(app_pw)))
                break
            except psycopg.OperationalError:
                exited = _docker("inspect", "--format", "{{.State.Running}}", cid) != "true"
                if exited or time.monotonic() > deadline:
                    # postgres logs to stderr; keep both streams.
                    logs = subprocess.run(["docker", "logs", cid], capture_output=True, text=True,  # noqa: S603,S607
                                          timeout=60, check=False)
                    raise RuntimeError(f"postgres did not start ({'exited' if exited else 'timeout'}): "
                                       f"{(logs.stdout + logs.stderr)[-2000:]}") from None
                time.sleep(1)
    except BaseException:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, check=False, timeout=60)  # noqa: S603,S607
        raise
    probe = f"host={host} port={port} dbname=chat user=gate_ro password={ro_pw}"
    return cid, Pg(su, probe, str(tls_dir / "ca.crt") if tls_dir else None)


@pytest.fixture(scope="session")
def pg_pair(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Pg, Pg]]:
    reason = _docker_reason()
    if reason:
        pytest.skip(f"PostgreSQL tests need real postgres:16 containers: {reason}")
    tls_dir = tmp_path_factory.mktemp("pgtls")
    _make_certs(tls_dir)
    cids: list[str] = []
    try:
        c1, plain = _start_pg(None)
        cids.append(c1)
        c2, tls = _start_pg(tls_dir)
        cids.append(c2)
        yield plain, tls
    finally:
        for c in cids:
            subprocess.run(["docker", "rm", "-f", c], capture_output=True, check=False, timeout=60)  # noqa: S603,S607


@dataclass(frozen=True)
class S3Target:
    """A real object store named by the operator. No local stand-in is started (D-22)."""

    endpoint: str
    bucket: str
    region: str
    addressing_style: str
    canary_key: str
    write_probes: bool
    app_env: tuple[str, str] | None  # env var names holding the app key pair; None -> default chain
    other_env: tuple[str, str] | None


@pytest.fixture(scope="session")
def s3_target() -> S3Target:
    """Object-store behaviour tests run only against a real Rook-Ceph RGW or S3 bucket.

    Required: BRAVO_TEST_S3_ENDPOINT, BRAVO_TEST_S3_BUCKET (an existing bucket; the platform's canary
    object at BRAVO_TEST_S3_CANARY_KEY, default ``_gate/canary``, makes OBJ-01/05/06 decidable).
    Optional: BRAVO_TEST_S3_REGION (us-east-1), BRAVO_TEST_S3_ADDRESSING_STYLE (path),
    BRAVO_TEST_S3_ACCESS_KEY_ID / BRAVO_TEST_S3_SECRET_ACCESS_KEY (app credential, else the default chain),
    BRAVO_TEST_S3_OTHER_ACCESS_KEY_ID / BRAVO_TEST_S3_OTHER_SECRET_ACCESS_KEY (OBJ-05),
    BRAVO_TEST_S3_WRITE_PROBES=1 (allow the OBJ-03/OBJ-04 PUT probes on this bucket).
    """
    endpoint = os.environ.get("BRAVO_TEST_S3_ENDPOINT", "").strip()
    bucket = os.environ.get("BRAVO_TEST_S3_BUCKET", "").strip()
    if not endpoint or not bucket:
        pytest.skip("object-store behaviour tests need a real store: set BRAVO_TEST_S3_ENDPOINT and "
                    "BRAVO_TEST_S3_BUCKET (Rook-Ceph RGW on the on-prem K3s landing zone, or an S3 bucket); "
                    "no local stand-in is used")
    app = ("BRAVO_TEST_S3_ACCESS_KEY_ID", "BRAVO_TEST_S3_SECRET_ACCESS_KEY")
    other = ("BRAVO_TEST_S3_OTHER_ACCESS_KEY_ID", "BRAVO_TEST_S3_OTHER_SECRET_ACCESS_KEY")
    return S3Target(
        endpoint=endpoint,
        bucket=bucket,
        region=os.environ.get("BRAVO_TEST_S3_REGION", "").strip() or "us-east-1",
        addressing_style=os.environ.get("BRAVO_TEST_S3_ADDRESSING_STYLE", "").strip() or "path",
        canary_key=os.environ.get("BRAVO_TEST_S3_CANARY_KEY", "").strip() or "_gate/canary",
        write_probes=os.environ.get("BRAVO_TEST_S3_WRITE_PROBES", "") == "1",
        app_env=app if all(os.environ.get(v) for v in app) else None,
        other_env=other if all(os.environ.get(v) for v in other) else None,
    )


# CloudNativePG's generated pg_hba.conf (operator defaults, user rules empty): local peer through the "local"
# ident map, client certificates for replication and the pooler, scram-sha-256 for every other host connection.
CNPG_DEFAULT_HBA = """\
# Grant local access ('local' user map)
local all all peer map=local

# Require client certificate authentication for the streaming_replica user
hostssl postgres streaming_replica all cert
hostssl replication streaming_replica all cert
hostssl all cnpg_pooler_pgbouncer all cert

# Default authentication method
host all all all scram-sha-256
"""


@dataclass(frozen=True)
class HbaLab:
    pg: Pg
    hba_file: Path  # host path of the server's pg_hba.conf (pg_hba_file_rules reads it at query time)


@pytest.fixture(scope="session")
def pg_hba_lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[HbaLab]:
    """A TLS-enabled postgres:16 whose pg_hba.conf is CNPG's default (ssl is needed for the hostssl lines)."""
    reason = _docker_reason()
    if reason:
        pytest.skip(f"PostgreSQL tests need real postgres:16 containers: {reason}")
    d = tmp_path_factory.mktemp("hbalab")
    _make_certs(d)
    (d / "pg_hba.conf").write_text(CNPG_DEFAULT_HBA)
    (d / "pg_ident.conf").write_text("local postgres postgres\n")
    for f in d.iterdir():
        f.chmod(0o644)
    cid, pg = _start_pg(d, ident=True)
    try:
        yield HbaLab(pg, d / "pg_hba.conf")
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, check=False, timeout=60)  # noqa: S603,S607
