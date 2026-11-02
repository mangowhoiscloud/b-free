"""Real services for the tests. Nothing is faked; what is not available is skipped with the reason.

Containers: PostgreSQL 16 (src and dst, the pilot schema and logical replication exactly as
compose.data.yaml sets them up) and PgBouncer in transaction mode (the pilot image).
  BRAVO_TEST_PG_IMAGE         default: the pilot CHAT_DB_IMAGE (postgres 16.15 by digest)
  BRAVO_TEST_PGBOUNCER_IMAGE  default: bravo-pgbouncer:pilot (built by deploy/pilot)

Object storage: no local S3 stand-in. Attachment tests write to a real S3-compatible store, the
Rook-Ceph RGW of the on-prem K3s landing zone, given by
  BRAVO_TEST_S3_ENDPOINT, BRAVO_TEST_S3_BUCKET   (required to run them)
  BRAVO_TEST_S3_REGION (default us-east-1), BRAVO_TEST_S3_ADDRESSING_STYLE (default path),
  BRAVO_TEST_S3_ACCESS_KEY_ID, BRAVO_TEST_S3_SECRET_ACCESS_KEY (else the SDK default chain).
The bucket must exist; the tests only add att/<sha256> objects. Amazon S3 is only read
(HeadBucket, HeadObject): BRAVO_TEST_AWS_S3_BUCKET, BRAVO_TEST_AWS_S3_REGION.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

DEMO_DIR = Path(__file__).resolve().parents[1]
SCHEMA_DIR = DEMO_DIR / "schema"
PG_IMAGE = os.environ.get(
    "BRAVO_TEST_PG_IMAGE",
    "postgres:16.15@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a",
)
PGB_IMAGE = os.environ.get("BRAVO_TEST_PGBOUNCER_IMAGE", "bravo-pgbouncer:pilot")


def _docker(*args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


def _docker_ok() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI not found"
    try:
        _docker("info", "--format", "{{.ServerVersion}}", timeout=20)
    except Exception as exc:  # noqa: BLE001
        return f"docker daemon not reachable: {exc}"
    return None


def _image_present(image: str) -> bool:
    return _docker("image", "inspect", image, check=False).returncode == 0


def _host_port(name: str, port: int) -> int:
    out = _docker("port", name, f"{port}/tcp").stdout.strip().splitlines()[0]
    return int(out.rsplit(":", 1)[1])


def _wait_pg(dsn: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"database not ready: {last}")


def _wait_tcp(port: int, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.3)
    raise RuntimeError(f"port {port} not open")


@dataclass
class Stack:
    tag: str
    network: str
    passwords: dict[str, str]
    src_port: int
    dst_port: int
    pgb_port: int | None
    tmp: Path

    def dsn(self, side: str, user: str) -> str:
        port = {"src": self.src_port, "dst": self.dst_port}[side]
        return f"host=127.0.0.1 port={port} dbname=chat user={user} password={self.passwords[user]}"

    @property
    def app_dsn_no_password(self) -> str:
        assert self.pgb_port is not None
        return f"host=127.0.0.1 port={self.pgb_port} dbname=chat user=app_writer"

    @property
    def pgb_admin_dsn(self) -> str:
        assert self.pgb_port is not None
        return (
            f"host=127.0.0.1 port={self.pgb_port} dbname=pgbouncer user=pgb_admin "
            f"password={self.passwords['pgb_admin']}"
        )


@pytest.fixture(scope="session")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    reason = _docker_ok()
    if reason:
        pytest.skip(reason)
    if not _image_present(PG_IMAGE):
        pull = _docker("pull", PG_IMAGE, check=False, timeout=300)
        if pull.returncode != 0:
            pytest.skip(f"PostgreSQL image {PG_IMAGE} not available: {pull.stderr.strip()[:200]}")

    tag = "bravo-demo-test-" + uuid.uuid4().hex[:8]
    network = tag
    tmp = tmp_path_factory.mktemp("stack")
    pw = {k: secrets.token_urlsafe(18) for k in ("postgres", "app_writer", "replicator", "executor_ctl",
                                                  "executor_observe", "pgb_admin", "pgb_stats")}
    names: list[str] = []
    _docker("network", "create", network)
    try:
        for side, store in (("src", "store-src"), ("dst", "store-dst")):
            name = f"{tag}-{side}"
            names.append(name)
            _docker(
                "run", "-d", "--name", name, "--network", network, "--network-alias", f"{side}-db",
                "-p", "127.0.0.1::5432",
                "-e", "POSTGRES_DB=chat", "-e", "POSTGRES_USER=postgres", "-e", f"POSTGRES_PASSWORD={pw['postgres']}",
                "-e", "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
                "-e", "POSTGRES_INITDB_ARGS=--auth-local=scram-sha-256 --auth-host=scram-sha-256",
                "-e", "BRAVO_SCHEMA_DIR=/bravo-schema/sql", "-e", f"BRAVO_SIDE={side}",
                "-e", f"BRAVO_ATTACHMENT_STORE_ID={store}",
                "-e", f"APP_WRITER_PASSWORD={pw['app_writer']}", "-e", f"REPLICATOR_PASSWORD={pw['replicator']}",
                "-e", f"EXECUTOR_CTL_PASSWORD={pw['executor_ctl']}",
                "-e", f"EXECUTOR_OBSERVE_PASSWORD={pw['executor_observe']}",
                "-v", f"{SCHEMA_DIR / '00-bravo-init.sh'}:/docker-entrypoint-initdb.d/00-bravo-init.sh:ro",
                "-v", f"{SCHEMA_DIR / 'sql'}:/bravo-schema/sql:ro",
                PG_IMAGE,
                "postgres", "-c", "wal_level=logical", "-c", "max_replication_slots=10", "-c", "max_wal_senders=10",
                "-c", "password_encryption=scram-sha-256", "-c", f"cluster_name={side}",
            )
        src_port = _host_port(f"{tag}-src", 5432)
        dst_port = _host_port(f"{tag}-dst", 5432)
        for side, port in (("src", src_port), ("dst", dst_port)):
            if _docker("inspect", "-f", "{{.State.Running}}", f"{tag}-{side}").stdout.strip() != "true":
                raise RuntimeError(_docker("logs", f"{tag}-{side}", check=False).stderr[-1500:])
            # The init-time server listens on the unix socket only; wait for the real one.
            _wait_pg(f"host=127.0.0.1 port={port} dbname=chat user=postgres password={pw['postgres']}")
            with psycopg.connect(
                f"host=127.0.0.1 port={port} dbname=chat user=postgres password={pw['postgres']}"
            ) as conn:
                deadline = time.monotonic() + 60
                while conn.execute("SELECT to_regclass('attachment_store')").fetchone()[0] is None:  # type: ignore[index]
                    if time.monotonic() > deadline:
                        raise RuntimeError("schema init did not finish")
                    time.sleep(0.5)

        repl = _docker(
            "run", "--rm", "--network", network, "--entrypoint", "bash",
            "-e", f"POSTGRES_PASSWORD={pw['postgres']}", "-e", f"REPLICATOR_PASSWORD={pw['replicator']}",
            "-e", "SRC_HOST=src-db", "-e", "DST_HOST=dst-db", "-e", "DB_PORT=5432", "-e", "CHAT_DB=chat",
            "-e", "SYNC_TIMEOUT_S=90",
            "-v", f"{SCHEMA_DIR / 'repl-setup.sh'}:/bravo/repl-setup.sh:ro",
            PG_IMAGE, "/bravo/repl-setup.sh", check=False, timeout=180,
        )
        if repl.returncode != 0:
            raise RuntimeError(f"repl-setup failed: {repl.stdout[-800:]} {repl.stderr[-800:]}")

        pgb_port = None
        if _image_present(PGB_IMAGE):
            conf = tmp / "pgbouncer"
            conf.mkdir()
            ini = (DEMO_DIR.parents[1] / "deploy/pilot/pgbouncer/pgbouncer.ini").read_text()
            (conf / "pgbouncer.ini").write_text(ini)
            (conf / "userlist.txt").write_text(
                "".join(f'"{u}" "{pw[u]}"\n' for u in ("app_writer", "pgb_admin", "pgb_stats"))
            )
            os.chmod(conf, 0o755)
            os.chmod(conf / "pgbouncer.ini", 0o644)
            os.chmod(conf / "userlist.txt", 0o644)
            name = f"{tag}-pgb"
            names.append(name)
            _docker("run", "-d", "--name", name, "--network", network, "-p", "127.0.0.1::6432",
                    "-v", f"{conf}:/etc/pgbouncer:ro", PGB_IMAGE)
            pgb_port = _host_port(name, 6432)
            _wait_tcp(pgb_port)

        yield Stack(tag, network, pw, src_port, dst_port, pgb_port, tmp)
    finally:
        if os.environ.get("BRAVO_TEST_KEEP") != "1":
            for name in names:
                _docker("rm", "-f", "-v", name, check=False)
            _docker("network", "rm", network, check=False)


@dataclass(frozen=True)
class S3Target:
    endpoint: str | None
    bucket: str
    region: str
    addressing_style: str
    access_key_id: str | None
    secret_access_key: str | None

    def env(self, store_id: str) -> dict[str, str]:
        env = {
            "S3_BUCKET": self.bucket, "S3_REGION": self.region, "S3_ADDRESSING_STYLE": self.addressing_style,
            "ATTACHMENT_STORE_ID": store_id,
        }
        if self.endpoint:
            env["S3_ENDPOINT_URL"] = self.endpoint
        if self.access_key_id and self.secret_access_key:
            env["S3_ACCESS_KEY_ID"] = self.access_key_id
            env["S3_SECRET_ACCESS_KEY"] = self.secret_access_key
        return env


@pytest.fixture(scope="session")
def s3_target() -> S3Target:
    """A real S3-compatible store for write tests (RGW on the on-prem K3s landing zone)."""
    endpoint = os.environ.get("BRAVO_TEST_S3_ENDPOINT", "").strip()
    bucket = os.environ.get("BRAVO_TEST_S3_BUCKET", "").strip()
    if not endpoint or not bucket:
        pytest.skip(
            "attachment write tests need a real S3-compatible store: set BRAVO_TEST_S3_ENDPOINT and "
            "BRAVO_TEST_S3_BUCKET (Rook-Ceph RGW on the on-prem K3s landing zone); no local stand-in is used"
        )
    return S3Target(
        endpoint=endpoint,
        bucket=bucket,
        region=os.environ.get("BRAVO_TEST_S3_REGION", "").strip() or "us-east-1",
        addressing_style=os.environ.get("BRAVO_TEST_S3_ADDRESSING_STYLE", "").strip() or "path",
        access_key_id=os.environ.get("BRAVO_TEST_S3_ACCESS_KEY_ID") or None,
        secret_access_key=os.environ.get("BRAVO_TEST_S3_SECRET_ACCESS_KEY") or None,
    )


@pytest.fixture()
def superuser_src(stack: Stack) -> Iterator[psycopg.Connection]:
    with psycopg.connect(stack.dsn("src", "postgres"), autocommit=True) as conn:
        yield conn


@pytest.fixture(autouse=True)
def _reset_attachment_store(request: pytest.FixtureRequest) -> Iterator[None]:
    yield
    if "stack" in request.fixturenames:
        stack: Stack = request.getfixturevalue("stack")
        with psycopg.connect(stack.dsn("src", "postgres"), autocommit=True) as conn:
            conn.execute(
                "UPDATE attachment_store SET frozen = false, freeze_watermark = NULL, active_store = 'store-src'"
            )
