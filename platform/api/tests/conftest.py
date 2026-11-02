"""Shared fixtures. DB tests run against a throwaway postgres:16 container (real RLS,
triggers and grants); the Executor is a small HTTP stub served on localhost.

Test identities (audit A3): the tests generate their own RSA key and serve its JWKS over HTTP on
localhost, so the API fetches keys exactly as it does from the Cloudflare Access certs URL. The product
package has no dev keys, no dev token endpoint and no seed; the seed lives in ``tests/devseed.py``.

Upstreams the state machine needs before it may leave COLLECT / ANALYZE (audits B11, A4) are real
services started per session: a throwaway OpenBao dev server holding the KV entries the tests' secret
refs name (metadata is all the API reads), and the real platform/collector scanning a small app with no
gap-grade dependency. Nothing in the API is told to assume either answer."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import jwt
import psycopg
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Header

from bravo_api.config import Mode, Settings
from bravo_api.migrate import apply

from .devseed import DEV_USERS, dev_sub

PG_IMAGE = os.environ.get(
    "BRAVO_TEST_PG_IMAGE",
    "postgres:16.13@sha256:5d143123fdf80462d1778cd4f24b9f7ca13c87174bca19141fb194c5a1ebca59",
)
ISSUER = "https://bravo-test.cloudflareaccess.com"
CONSOLE_AUD = "console-aud-test"
MCP_AUD = "mcp-aud-test"
KID = "test-1"
BAO_IMAGE = "openbao/openbao@sha256:5046911075e137e154ce49ab051502f4ec7cf367d484ae4a4dce665d9b90561f"  # 2.7.2
COLLECTOR_DIR = __import__("pathlib").Path(__file__).resolve().parents[2] / "collector"
# secret refs the tests register; anything else is "not found" in the test OpenBao, as in production.
TEST_SECRET_REFS = ("tenants/t1/src", "tenants/t1/dst", "tenants/t1/a", "tenants/t1/s", "tenants/t1/d", "tenants/t1/x",
                    "envs/landing-src/chat-db", "envs/landing-dst/chat-db")
UPSTREAMS: dict[str, str | None] = {}
PASSWORDS = {"bravo_api": "api-pw", "bravo_mcp": "mcp-pw", "bravo_executor": "exec-pw", "bravo_apply": "apply-pw"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Keys:
    private: rsa.RSAPrivateKey
    jwks_url: str  # served on localhost, fetched by the API like the Access certs URL

    def token(
        self,
        email: str,
        aud: str,
        *,
        sub: str | None = None,
        iss: str = ISSUER,
        alg: str = "RS256",
        exp_delta: int = 3600,
        drop: tuple[str, ...] = (),
        kid: str = KID,
        key: Any = None,
    ) -> str:
        now = int(time.time())
        claims = {"iss": iss, "aud": [aud], "sub": sub or dev_sub(email), "email": email, "iat": now, "exp": now + exp_delta}
        for k in drop:
            claims.pop(k, None)
        return jwt.encode(claims, key if key is not None else self.private, algorithm=alg, headers={"kid": kid})


def public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return jwk


@pytest.fixture(scope="session")
def keys() -> Keys:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    app = FastAPI()
    body = {"keys": [public_jwk(key, KID)]}

    @app.get("/cdn-cgi/access/certs")
    def certs() -> dict[str, Any]:
        return body

    return Keys(private=key, jwks_url=_serve(app) + "/cdn-cgi/access/certs")


@pytest.fixture(scope="session")
def pg() -> Iterator[dict[str, str]]:
    port = free_port()
    name = f"bravo-api-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_USER=bravo_owner",
            "-e",
            "POSTGRES_PASSWORD=owner-pw",
            "-e",
            "POSTGRES_DB=bravo",
            "-p",
            f"127.0.0.1:{port}:5432",
            PG_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    owner = f"postgresql://bravo_owner:owner-pw@127.0.0.1:{port}/bravo"
    try:
        deadline = time.time() + 60
        while True:
            try:
                with psycopg.connect(owner, connect_timeout=2) as c:
                    c.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        os.environ.update(
            {
                "BRAVO_API_DB_PASSWORD": PASSWORDS["bravo_api"],
                "BRAVO_MCP_DB_PASSWORD": PASSWORDS["bravo_mcp"],
                "BRAVO_EXECUTOR_DB_PASSWORD": PASSWORDS["bravo_executor"],
                "BRAVO_APPLY_DB_PASSWORD": PASSWORDS["bravo_apply"],
            }
        )
        # Production shape (CNPG on the platform cluster): the migrations run as a NON-superuser database owner
        # without BYPASSRLS, so FORCE ROW LEVEL SECURITY applies to it and to its SECURITY DEFINER functions.
        # A superuser owner would hide exactly that class of bug (found 2026-11-02 on bravo-platform-rke2).
        with psycopg.connect(owner, autocommit=True) as c:
            c.execute("CREATE ROLE bravo_db_owner LOGIN PASSWORD 'dbowner-pw' NOSUPERUSER CREATEROLE NOBYPASSRLS")
            c.execute("CREATE DATABASE bravo_app OWNER bravo_db_owner")
        owner = f"postgresql://bravo_owner:owner-pw@127.0.0.1:{port}/bravo_app"  # superuser: test seeding only
        apply(f"postgresql://bravo_db_owner:dbowner-pw@127.0.0.1:{port}/bravo_app")
        from .devseed import seed

        with psycopg.connect(owner, autocommit=True) as c:
            seed(c, issuer=ISSUER, tenant_id="t1")
            c.execute("INSERT INTO tenants (tenant_id, name) VALUES ('t2', 'Other tenant')")
            # A principal holding both operator and approver: used to prove the DB SoD constraint.
            c.execute(
                "INSERT INTO principals (iss, sub, email) VALUES (%s, %s, 'dual@bravo.test')",
                (ISSUER, dev_sub("dual@bravo.test")),
            )
            c.execute("""INSERT INTO memberships (principal_id, tenant_id, role)
                         SELECT id, 't1', r FROM principals, unnest(ARRAY['operator','approver']) r
                          WHERE email = 'dual@bravo.test'""")
        base = f"127.0.0.1:{port}/bravo_app"
        yield {"owner": owner, **{role: f"postgresql://{role}:{pw}@{base}" for role, pw in PASSWORDS.items()}}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@dataclass
class ExecutorStub:
    url: str
    controls: list[dict[str, Any]] = field(default_factory=list)
    readiness: dict[str, Any] = field(default_factory=dict)
    cutovers: dict[str, str] = field(default_factory=dict)
    cutover_bodies: list[dict[str, Any]] = field(default_factory=list)
    failback_bodies: list[dict[str, Any]] = field(default_factory=list)
    fail_cutover: bool = False

    def healthy(self) -> None:
        self.controls = [
            {"control_id": "DB-ANON-READ", "question": "q", "src": "PASS", "dst": "PASS"},
            {"control_id": "DB-AUTH", "question": "q", "src": "PASS", "dst": "PASS"},
        ]
        self.readiness = {
            "lag_bytes": 0,
            "sub_state": "streaming",
            "schema_hash": {"src": "abc", "dst": "abc"},
            "seq": {"src": 10, "dst": 10},
            "rows": {"src": 5, "dst": 5},
        }


def _serve(app: FastAPI) -> str:
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("stub did not start")
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}"


@pytest.fixture(scope="session")
def executor() -> ExecutorStub:
    app = FastAPI()
    stub = ExecutorStub(url="")

    @app.get("/v1/controls")
    def controls() -> list[dict[str, Any]]:
        return stub.controls

    @app.get("/v1/readiness")
    def readiness() -> dict[str, Any]:
        return stub.readiness

    @app.post("/v1/cutover", status_code=202)
    def cutover(body: dict[str, Any], idempotency_key: str = Header(alias="Idempotency-Key")) -> Any:
        stub.cutover_bodies.append(body)
        if stub.fail_cutover:
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": "boom"}, status_code=503)
        run_id = stub.cutovers.setdefault(idempotency_key, str(uuid.uuid4()))
        return {"run_id": run_id}

    @app.post("/v1/failback", status_code=202)
    def failback(body: dict[str, Any], idempotency_key: str = Header(alias="Idempotency-Key")) -> Any:
        # Same contract as /v1/cutover (§8.6); the real endpoint is platform/executor's.
        stub.failback_bodies.append(body)
        return {"run_id": stub.cutovers.setdefault(idempotency_key, str(uuid.uuid4()))}

    stub.url = _serve(app)
    stub.healthy()
    return stub


@pytest.fixture(scope="session", autouse=True)
def platform_upstreams(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str | None]]:
    """Real OpenBao (dev server, throwaway) + real platform/collector for every API test session."""
    import httpx

    procs: list[subprocess.Popen[bytes]] = []
    containers: list[str] = []
    try:
        port = free_port()
        name = f"bravo-api-test-bao-{uuid.uuid4().hex[:8]}"
        root_token = uuid.uuid4().hex
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:8200", BAO_IMAGE,
             "bao", "server", "-dev", f"-dev-root-token-id={root_token}", "-dev-listen-address=0.0.0.0:8200"],
            check=True, capture_output=True,
        )
        containers.append(name)
        bao = f"http://127.0.0.1:{port}"
        deadline = time.time() + 30
        while True:
            try:
                if httpx.get(f"{bao}/v1/sys/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                raise RuntimeError("test OpenBao did not start")
            time.sleep(0.2)
        for ref in TEST_SECRET_REFS:
            r = httpx.post(f"{bao}/v1/secret/data/{ref}", headers={"X-Vault-Token": root_token},
                           json={"data": {"dsn": "postgresql://placeholder-not-read-by-the-api"}})
            r.raise_for_status()
        token_file = tmp_path_factory.mktemp("bao") / "token"
        token_file.write_text(root_token)
        token_file.chmod(0o600)
        UPSTREAMS.update(bao_addr=bao, bao_token_file=str(token_file))

        collector_py = COLLECTOR_DIR / ".venv" / "bin" / "python"
        if collector_py.is_file():
            repo = tmp_path_factory.mktemp("clean-app")
            (repo / "app.py").write_text('import os\n\nDSN = os.environ["DATABASE_URL"]\n')
            envs = tmp_path_factory.mktemp("collector-envs") / "environments.yaml"
            envs.write_text("environments: {}\n")
            cport = free_port()
            proc = subprocess.Popen(
                [str(collector_py), "-m", "bravo_collector", "serve"],
                env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path_factory.mktemp("collector-home")),
                     "BRAVO_ENVIRONMENTS_FILE": str(envs), "BRAVO_INVENTORY_ROOT": str(repo),
                     "BRAVO_COLLECTOR_HOST": "127.0.0.1", "BRAVO_COLLECTOR_PORT": str(cport), "LOG_LEVEL": "warning"},
                cwd=COLLECTOR_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            procs.append(proc)
            curl = f"http://127.0.0.1:{cport}"
            deadline = time.time() + 30
            while True:
                try:
                    if httpx.get(f"{curl}/healthz", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.time() > deadline or proc.poll() is not None:
                    raise RuntimeError("collector did not start")
                time.sleep(0.2)
            UPSTREAMS["collector_url"] = curl
        else:
            UPSTREAMS["collector_url"] = None  # flow tests will then stay in ANALYZE (collector_unconfigured)
        yield UPSTREAMS
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait(10)
        for name in containers:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def make_settings(keys: Keys, pg: dict[str, str], executor_url: str, mode: Mode) -> Settings:
    return Settings(
        mode=mode,
        database_url=pg["bravo_api"] if mode is Mode.API else pg["bravo_mcp"],
        jwt_issuer=ISSUER,
        console_aud=CONSOLE_AUD,
        mcp_aud=MCP_AUD,
        executor_url=executor_url,
        executor_timeout_s=2.0,
        console_base_url="http://console.test",
        cors_origins=("http://localhost:5173",),
        mcp_allowed_origins=(),
        mcp_allowed_hosts=(),
        approval_ttl_s=900,
        gate_max_lag_bytes=65536,
        gate_required_controls=("DB-ANON-READ", "DB-AUTH"),
        run_poll_interval_s=0.1,
        sse_poll_interval_s=0.1,
        sse_ticket_secret=b"test-secret",
        collector_url=UPSTREAMS.get("collector_url"),
        bao_addr=UPSTREAMS.get("bao_addr"),
        bao_token_file=UPSTREAMS.get("bao_token_file"),
        change_poll_interval_s=0.1,
        jwks_url=keys.jwks_url,
    )


def email(user: str) -> str:
    return DEV_USERS[user][0] if user in DEV_USERS else f"{user}@bravo.test"
