"""Shared fixtures. DB tests run against a throwaway postgres:16 container (real RLS,
triggers and grants); the Executor is a small HTTP stub served on localhost."""

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
from pathlib import Path
from typing import Any

import jwt
import psycopg
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Header

from bravo_api.config import Mode, Settings
from bravo_api.devseed import DEV_USERS, dev_sub
from bravo_api.migrate import apply

PG_IMAGE = os.environ.get(
    "BRAVO_TEST_PG_IMAGE",
    "postgres:16.13@sha256:5d143123fdf80462d1778cd4f24b9f7ca13c87174bca19141fb194c5a1ebca59",
)
ISSUER = "https://bravo-test.cloudflareaccess.com"
CONSOLE_AUD = "console-aud-test"
MCP_AUD = "mcp-aud-test"
KID = "test-1"
PASSWORDS = {"bravo_api": "api-pw", "bravo_mcp": "mcp-pw", "bravo_executor": "exec-pw"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Keys:
    private: rsa.RSAPrivateKey
    jwks_path: Path

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


@pytest.fixture(scope="session")
def keys(tmp_path_factory: pytest.TempPathFactory) -> Keys:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    path = tmp_path_factory.mktemp("jwks") / "jwks.json"
    path.write_text(json.dumps({"keys": [jwk]}))
    return Keys(private=key, jwks_path=path)


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
            }
        )
        apply(owner)
        from bravo_api.devseed import seed

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
        base = f"127.0.0.1:{port}/bravo"
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


def make_settings(keys: Keys, pg: dict[str, str], executor_url: str, mode: Mode) -> Settings:
    return Settings(
        mode=mode,
        database_url=pg["bravo_api"] if mode is Mode.API else pg["bravo_mcp"],
        jwks_path=keys.jwks_path,
        jwt_issuer=ISSUER,
        console_aud=CONSOLE_AUD,
        mcp_aud=MCP_AUD,
        executor_url=executor_url,
        executor_timeout_s=2.0,
        console_base_url="http://console.test",
        cors_origins=("http://localhost:5173",),
        mcp_allowed_origins=(),
        mcp_allowed_hosts=(),
        dev_auth=False,
        dev_tokens_dir=None,
        approval_ttl_s=900,
        gate_max_lag_bytes=65536,
        gate_required_controls=("DB-ANON-READ", "DB-AUTH"),
        run_poll_interval_s=0.1,
        sse_poll_interval_s=0.1,
        sse_ticket_secret=b"test-secret",
    )


def email(user: str) -> str:
    return DEV_USERS[user][0] if user in DEV_USERS else f"{user}@bravo.test"
