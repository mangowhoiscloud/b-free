"""Write-block switch (spec r2 §8.10, CLOUD-E2E-CONTRACT §8) against the real app process.

The app runs as `python -m bravo_demo` (both listeners: chat and admin) and writes through the
real PgBouncer to the real PostgreSQL 16 of the test stack.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
import pytest
import websockets

from .conftest import Stack


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class AppProc:
    proc: subprocess.Popen[bytes]
    chat: str
    admin: str
    token: str | None
    env_id: str
    log: Path

    def auth(self, token: str | None = None) -> dict[str, str]:
        value = token if token is not None else self.token
        return {"Authorization": f"Bearer {value}"} if value else {}


@pytest.fixture(scope="session")
def pgb(stack: Stack) -> Stack:
    if stack.pgb_port is None:
        pytest.skip("PgBouncer image bravo-pgbouncer:pilot not built (docker compose build in deploy/pilot)")
    return stack


@contextmanager
def app_process(stack: Stack, tmp: Path, *, env_id: str, token: str | None) -> Iterator[AppProc]:
    pw = tmp / f"pw-{uuid.uuid4().hex[:6]}"
    pw.write_text(stack.passwords["app_writer"] + "\n")
    chat_port, admin_port = _free_port(), _free_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "APP_HOST": "127.0.0.1",
        "APP_PORT": str(chat_port),
        "ADMIN_PORT": str(admin_port),
        "APP_ENV_ID": env_id,
        "APP_ENV_LABEL": env_id,
        "APP_INSTANCE_ID": f"{env_id}-{uuid.uuid4().hex[:4]}",
        "DB_DSN": stack.app_dsn_no_password,
        "DB_PASSWORD_SOURCE": "env-file",
        "DB_PASSWORD_FILE": str(pw),
        "POLL_INTERVAL_S": "0.2",
        "WRITE_WAIT_S": "3",
        "WRITE_BLOCK_RETRY_AFTER_S": "2",
    }
    if token is not None:
        tok = tmp / f"admin-token-{uuid.uuid4().hex[:6]}"
        tok.write_text(token + "\n")
        env["ADMIN_TOKEN_FILE"] = str(tok)
    log = tmp / f"app-{env_id}-{chat_port}.log"
    with log.open("wb") as out:
        proc = subprocess.Popen([sys.executable, "-m", "bravo_demo"], env=env, stdout=out, stderr=subprocess.STDOUT)
    app = AppProc(proc, f"http://127.0.0.1:{chat_port}", f"http://127.0.0.1:{admin_port}", token, env_id, log)
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"app exited {proc.returncode}: {log.read_text()[-2000:]}")
            try:
                if httpx.get(f"{app.chat}/readyz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"app not ready: {log.read_text()[-2000:]}")
            time.sleep(0.2)
        yield app
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()


def _token() -> str:
    return secrets.token_urlsafe(32)


def _env_id() -> str:
    return "t" + uuid.uuid4().hex[:10]


def _post(app: AppProc, room: str, key: str, body: str) -> httpx.Response:
    return httpx.post(f"{app.chat}/api/rooms/{room}/messages", headers={"Idempotency-Key": key},
                      json={"author": "고객", "body": body}, timeout=10)


def _count(stack: Stack, key: str) -> int:
    with psycopg.connect(stack.dsn("src", "postgres")) as conn:
        return conn.execute("SELECT count(*) FROM messages WHERE idem_key = %s", (key,)).fetchone()[0]  # type: ignore[index]


def test_admin_fails_closed_without_token(pgb: Stack, tmp_path: Path) -> None:
    with app_process(pgb, tmp_path, env_id=_env_id(), token=None) as app:
        r = httpx.get(f"{app.admin}/admin/writes", headers={"Authorization": "Bearer anything"})
        assert r.status_code == 503 and r.json() == {"error": "admin_token_not_configured"}
        r = httpx.put(f"{app.admin}/admin/writes", json={"blocked": True})
        assert r.status_code == 503
        # The chat itself still works.
        assert _post(app, "consult-1", str(uuid.uuid4()), "토큰 없는 관리 포트").status_code == 201


def test_admin_rejects_missing_or_wrong_token(pgb: Stack, tmp_path: Path) -> None:
    with app_process(pgb, tmp_path, env_id=_env_id(), token=_token()) as app:
        for headers in ({}, app.auth("x" * 43), {"Authorization": f"Basic {app.token}"}):
            r = httpx.put(f"{app.admin}/admin/writes", json={"blocked": True}, headers=headers)
            assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert httpx.get(f"{app.admin}/admin/writes", headers=app.auth()).json()["blocked"] is False
        # The switch is not on the chat port (the one the edge exposes).
        assert httpx.get(f"{app.chat}/admin/writes", headers=app.auth()).status_code == 404
        r = httpx.put(f"{app.admin}/admin/writes", json={"blocked": "yes"}, headers=app.auth())
        assert r.status_code == 400


def test_block_rejects_new_writes_and_keeps_reads(pgb: Stack, tmp_path: Path) -> None:
    room = "t-" + uuid.uuid4().hex[:10]
    with app_process(pgb, tmp_path, env_id=_env_id(), token=_token()) as app:
        assert _post(app, room, str(uuid.uuid4()), "차단 전").status_code == 201
        r = httpx.put(f"{app.admin}/admin/writes", json={"blocked": True}, headers=app.auth())
        assert r.status_code == 200 and r.json()["blocked"] is True and r.json()["since"]
        since = r.json()["since"]
        # Idempotent: the same state keeps its `since`.
        again = httpx.put(f"{app.admin}/admin/writes", json={"blocked": True}, headers=app.auth()).json()
        assert again == {"blocked": True, "since": since}
        assert httpx.get(f"{app.admin}/admin/writes", headers=app.auth()).json() == {"blocked": True, "since": since}

        key = str(uuid.uuid4())
        r = _post(app, room, key, "차단 중")
        assert r.status_code == 503 and r.headers["retry-after"] == "2" and r.json()["error"] == "writes_blocked"

        async def ws_checks() -> None:
            async with websockets.connect(f"ws://127.0.0.1:{app.chat.rsplit(':', 1)[1]}/ws?room={room}") as ws:
                hello = json.loads(await ws.recv())
                assert hello["type"] == "hello"
                backlog = json.loads(await ws.recv())
                assert [m["body"] for m in backlog["items"]] == ["차단 전"]  # reads keep working
                await ws.send(json.dumps({"type": "send", "idem_key": key, "author": "고객", "body": "차단 중"}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg["type"] == "nack":
                        break
                assert msg["code"] == "writes_blocked" and msg["status"] == 503 and msg["retry_after_s"] == 2

        asyncio.run(ws_checks())
        assert httpx.get(f"{app.chat}/api/config").status_code == 200
        assert _count(pgb, key) == 0

        r = httpx.put(f"{app.admin}/admin/writes", json={"blocked": False}, headers=app.auth())
        assert r.json()["blocked"] is False and r.json()["since"] != since
        # The client retries with the same idempotency key: stored exactly once.
        assert _post(app, room, key, "차단 중").status_code == 201
        assert _post(app, room, key, "차단 중").status_code == 200
        assert _count(pgb, key) == 1


def test_switch_is_shared_by_replicas_and_scoped_to_environment(pgb: Stack, tmp_path: Path) -> None:
    env_a, env_b = _env_id(), _env_id()
    token = _token()
    with (
        app_process(pgb, tmp_path, env_id=env_a, token=token) as a1,
        app_process(pgb, tmp_path, env_id=env_a, token=token) as a2,
        app_process(pgb, tmp_path, env_id=env_b, token=token) as b1,
    ):
        assert httpx.put(f"{a1.admin}/admin/writes", json={"blocked": True}, headers=a1.auth()).status_code == 200
        # The other replica of the same environment sees the switch at once (state in the write DB).
        assert httpx.get(f"{a2.admin}/admin/writes", headers=a2.auth()).json()["blocked"] is True
        assert _post(a2, "consult-1", str(uuid.uuid4()), "a2").status_code == 503
        # Another environment is not blocked by it.
        assert _post(b1, "consult-1", str(uuid.uuid4()), "b1").status_code == 201
        assert httpx.put(f"{a2.admin}/admin/writes", json={"blocked": False}, headers=a2.auth()).status_code == 200
        assert _post(a1, "consult-1", str(uuid.uuid4()), "a1").status_code == 201


def test_switch_waits_for_write_in_flight(pgb: Stack, tmp_path: Path) -> None:
    """Once PUT returns, no write that saw the old state can still commit."""
    env_id = _env_id()
    with app_process(pgb, tmp_path, env_id=env_id, token=_token()) as app:
        result: dict[str, object] = {}
        with psycopg.connect(pgb.dsn("src", "app_writer")) as writer:
            # A business write in flight: it has passed the guard (FOR SHARE) and not committed yet.
            writer.execute("SELECT blocked FROM write_block WHERE env_id = %s FOR SHARE", (env_id,))

            def put() -> None:
                t0 = time.monotonic()
                r = httpx.put(f"{app.admin}/admin/writes", json={"blocked": True}, headers=app.auth(), timeout=10)
                result.update(status=r.status_code, body=r.json(), waited=time.monotonic() - t0)

            th = threading.Thread(target=put)
            th.start()
            time.sleep(0.8)
            assert th.is_alive(), "the switch returned while a write was still in flight"
            writer.commit()
        th.join(10)
        assert result["status"] == 200 and result["body"]["blocked"] is True  # type: ignore[index]
        assert float(result["waited"]) >= 0.8  # type: ignore[arg-type]
        assert _post(app, "consult-1", str(uuid.uuid4()), "after").status_code == 503


def test_sigterm_stops_both_listeners_and_closes_websockets_with_1012(pgb: Stack, tmp_path: Path) -> None:
    with app_process(pgb, tmp_path, env_id=_env_id(), token=_token()) as app:
        port = app.chat.rsplit(":", 1)[1]

        async def run() -> int | None:
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws?room=consult-1") as ws:
                await ws.recv()
                app.proc.send_signal(signal.SIGTERM)
                try:
                    async for _ in ws:
                        pass
                except websockets.ConnectionClosed:
                    pass
                return ws.close_code

        assert asyncio.run(run()) == 1012
        assert app.proc.wait(timeout=20) == 0
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"{app.admin}/admin/writes", headers=app.auth(), timeout=1)
