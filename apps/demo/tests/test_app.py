"""End to end through the real app and PgBouncer (transaction mode); attachments on a real S3 store."""

from __future__ import annotations

import socket
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from bravo_demo import config
from bravo_demo.app import create_app

from .conftest import S3Target, Stack


@pytest.fixture(scope="session")
def pgb(stack: Stack) -> Stack:
    if stack.pgb_port is None:
        pytest.skip("PgBouncer image bravo-pgbouncer:pilot not built (docker compose build in deploy/pilot)")
    return stack


ENV_IDS = {"AWS": "aws", "온프렘": "onprem", "GCP": "gcp"}


def _env(stack: Stack, tmp: Path, label: str, *, s3: S3Target | None = None, store: str = "store-src",
         wait_s: float = 8.0) -> dict[str, str]:
    pw = tmp / f"pw-{uuid.uuid4().hex[:6]}"
    pw.write_text(stack.passwords["app_writer"] + "\n")
    env = {
        "APP_ENV_LABEL": label,
        "APP_ENV_ID": ENV_IDS[label],
        "DB_DSN": stack.app_dsn_no_password,
        "DB_PASSWORD_SOURCE": "env-file",
        "DB_PASSWORD_FILE": str(pw),
        "WRITE_WAIT_S": str(wait_s),
        "POLL_INTERVAL_S": "0.2",
    }
    if s3 is not None:
        env.update(s3.env(store))
    return env


@contextmanager
def client(env: dict[str, str]) -> Iterator[TestClient]:
    with TestClient(create_app(config.load(env))) as c:
        yield c


def _recv_until(ws: Any, kind: str, pred=lambda m: True, limit: int = 50) -> dict[str, Any]:
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == kind and pred(msg):
            return msg
    raise AssertionError(f"no {kind} message")


def _wait_ready(c: TestClient, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        r = c.get("/readyz")
        if r.status_code == 200:
            return r.json()
        if time.monotonic() > deadline:
            raise AssertionError(f"not ready: {r.json()}")
        time.sleep(0.2)


def test_readiness_and_live_need_a_real_database_answer(pgb: Stack, tmp_path: Path) -> None:
    """No DB answer yet: not ready, and the socket says db_ok=false (the page shows "확인 중", not live)."""
    env = _env(pgb, tmp_path, "AWS")
    with socket.socket() as s:  # a port with nothing listening: connection refused
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    env["DB_DSN"] = f"host=127.0.0.1 port={closed} dbname=chat user=app_writer"
    with client(env) as c:
        r = c.get("/readyz")
        assert r.status_code == 503 and r.json()["db"] == "not_reached"
    with client(_env(pgb, tmp_path, "AWS")) as c:
        doc = _wait_ready(c)
        assert doc["status"] == "ready" and doc["db_age_s"] < 5 and doc["attachments"] == "off"
        with c.websocket_connect(f"/ws?room={_room()}") as ws:
            assert ws.receive_json()["db_ok"] is True
            assert _recv_until(ws, "status")["db_ok"] is True


def test_attachments_not_offered_until_the_store_answers(pgb: Stack, tmp_path: Path) -> None:
    """A configured but unreachable store: attachments stay off ("unavailable"), uploads get 503."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    env = _env(pgb, tmp_path, "AWS")
    env.update({"S3_BUCKET": "unreachable", "S3_ENDPOINT_URL": f"http://127.0.0.1:{closed}",
                "S3_REGION": "us-east-1", "S3_ADDRESSING_STYLE": "path", "ATTACHMENT_STORE_ID": "store-src",
                "S3_ACCESS_KEY_ID": "unused-access-key", "S3_SECRET_ACCESS_KEY": "unused-secret-key"})
    with client(env) as c:
        assert c.get("/api/config").json()["limits"]["attachments"] is False  # "checking" or "unavailable"
        deadline = time.monotonic() + 20
        while c.get("/api/config").json()["limits"]["attachments_state"] != "unavailable":
            assert time.monotonic() < deadline, "store state never left 'checking'"
            time.sleep(0.3)
        r = _put(c, "consult-1", b"x", str(uuid.uuid4()))
        assert r.status_code == 503 and r.json()["error"] == "attachments_unavailable"


def _room() -> str:
    return "t-" + uuid.uuid4().hex[:10]


def test_send_is_idempotent(pgb: Stack, tmp_path: Path) -> None:
    room = _room()
    with client(_env(pgb, tmp_path, "AWS")) as c, c.websocket_connect(f"/ws?room={room}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["env"]["label"] == "AWS"
        assert _recv_until(ws, "messages")["backlog"] is True
        key = str(uuid.uuid4())
        cmd = {"type": "send", "idem_key": key, "author": "고객", "body": "안녕하세요"}
        ws.send_json(cmd)
        first = _recv_until(ws, "ack", lambda m: m["idem_key"] == key)
        ws.send_json(cmd)
        second = _recv_until(ws, "ack", lambda m: m["idem_key"] == key)
        assert first["created"] is True and second["created"] is False
        assert first["message"]["id"] == second["message"]["id"]
        ws.send_json({**cmd, "body": "다른 내용"})
        nack = _recv_until(ws, "nack", lambda m: m["idem_key"] == key)
        assert nack["code"] == "conflict" and nack["retryable"] is False
    with psycopg.connect(pgb.dsn("src", "postgres")) as conn:
        assert conn.execute("SELECT count(*) FROM messages WHERE idem_key = %s", (key,)).fetchone()[0] == 1  # type: ignore[index]


def test_fanout_across_instances_and_resume_after(pgb: Stack, tmp_path: Path) -> None:
    """Two instances (two environments) share nothing but the database."""
    room = _room()
    with client(_env(pgb, tmp_path, "AWS")) as a, client(_env(pgb, tmp_path, "온프렘")) as b:
        with b.websocket_connect(f"/ws?room={room}") as wb:
            assert wb.receive_json()["env"]["label"] == "온프렘"
            _recv_until(wb, "messages")
            with a.websocket_connect(f"/ws?room={room}") as wa:
                wa.receive_json()
                _recv_until(wa, "messages")
                key = str(uuid.uuid4())
                wa.send_json({"type": "send", "idem_key": key, "author": "상담사", "body": "무엇을 도와드릴까요?"})
                ack = _recv_until(wa, "ack")
                seen = _recv_until(wb, "messages", lambda m: any(i["idem_key"] == key for i in m["items"]))
                assert seen["backlog"] is False
                first_id = ack["message"]["id"]
                wa.send_json({"type": "send", "idem_key": str(uuid.uuid4()), "author": "상담사", "body": "두 번째"})
                _recv_until(wa, "ack")
        # Reconnect (as after a 1012 close) with the last id: only newer messages come back.
        with b.websocket_connect(f"/ws?room={room}&after={first_id}") as wb2:
            wb2.receive_json()
            backlog = _recv_until(wb2, "messages")
            assert [m["body"] for m in backlog["items"]] == ["두 번째"]


def test_write_waits_during_pause_then_retry_is_exactly_once(pgb: Stack, tmp_path: Path) -> None:
    room = _room()
    with client(_env(pgb, tmp_path, "AWS", wait_s=1.5)) as c, c.websocket_connect(f"/ws?room={room}") as ws:
        ws.receive_json()
        _recv_until(ws, "messages")
        key = str(uuid.uuid4())
        cmd = {"type": "send", "idem_key": key, "author": "고객", "body": "쓰기 정지 중"}
        admin = psycopg.connect(pgb.pgb_admin_dsn, autocommit=True)
        try:
            admin.execute("PAUSE chat")
            t0 = time.monotonic()
            ws.send_json(cmd)
            nack = _recv_until(ws, "nack", lambda m: m["idem_key"] == key, limit=200)
            waited = time.monotonic() - t0
            assert nack["code"] == "write_timeout" and nack["retryable"] is True
            assert 1.4 <= waited < 4.0
        finally:
            admin.execute("RESUME chat")
            admin.close()
        ws.send_json(cmd)  # the user presses "다시 보내기": same idempotency key
        ack = _recv_until(ws, "ack", lambda m: m["idem_key"] == key, limit=200)
        assert ack["message"]["body"] == "쓰기 정지 중"
    with psycopg.connect(pgb.dsn("src", "postgres")) as conn:
        deadline = time.monotonic() + 5
        while True:  # the abandoned first attempt may land after RESUME; it must not duplicate
            n = conn.execute("SELECT count(*) FROM messages WHERE idem_key = %s", (key,)).fetchone()[0]  # type: ignore[index]
            assert n == 1
            if time.monotonic() > deadline:
                break
            time.sleep(0.5)


def _put(c: TestClient, room: str, data: bytes, key: str, ctype: str = "text/plain", name: str = "memo.txt"):
    return c.put(f"/api/rooms/{room}/attachments", content=data,
                 headers={"Idempotency-Key": key, "X-Filename": name, "X-Author": "%EA%B3%A0%EA%B0%9D",
                          "Content-Type": ctype})


def test_attachment_contract(pgb: Stack, s3_target: S3Target, tmp_path: Path) -> None:
    room = _room()
    data = b"loan consultation memo " + uuid.uuid4().bytes
    with client(_env(pgb, tmp_path, "AWS", s3=s3_target)) as c:
        key = str(uuid.uuid4())
        r1 = _put(c, room, data, key)
        assert r1.status_code == 201, r1.text
        att = r1.json()["attachment"]
        r2 = _put(c, room, data, key)
        assert r2.status_code == 200 and r2.json()["attachment"]["id"] == att["id"]
        assert _put(c, room, data + b"x", key).status_code == 409
        got = c.get(f"/api/attachments/{att['id']}")
        assert got.status_code == 200 and got.content == data
        assert got.headers["cache-control"] == "private, no-store"
        # Same content, new key: a new row that points at the same object key.
        r3 = _put(c, room, data, str(uuid.uuid4()))
        assert r3.status_code == 201 and r3.json()["attachment"]["sha256"] == att["sha256"]
        # Send it in a message.
        with c.websocket_connect(f"/ws?room={room}") as ws:
            ws.receive_json()
            _recv_until(ws, "messages")
            ws.send_json({"type": "send", "idem_key": str(uuid.uuid4()), "author": "고객", "body": "메모",
                          "attachment_id": att["id"]})
            ack = _recv_until(ws, "ack")
            assert ack["message"]["attachment"]["id"] == att["id"]
            ws.send_json({"type": "send", "idem_key": str(uuid.uuid4()), "author": "고객", "body": "x",
                          "attachment_id": str(uuid.uuid4())})
            assert _recv_until(ws, "nack")["code"] == "attachment_missing"
        # Size cap.
        big = c.put(f"/api/rooms/{room}/attachments", content=b"0" * (5 * 1024 * 1024 + 1),
                    headers={"Idempotency-Key": str(uuid.uuid4()), "X-Author": "a"})
        assert big.status_code == 413
    with psycopg.connect(pgb.dsn("src", "postgres")) as conn:
        row = conn.execute("SELECT object_key, store_id FROM attachments WHERE id = %s", (att["id"],)).fetchone()
        assert row == (f"att/{att['sha256']}", "store-src")


def test_attachment_paused_when_frozen_or_wrong_store(pgb: Stack, s3_target: S3Target, tmp_path: Path,
                                                      superuser_src: psycopg.Connection) -> None:
    room = _room()
    # An instance whose store is not the active store of the database its writes reach.
    with client(_env(pgb, tmp_path, "온프렘", s3=s3_target, store="store-dst")) as c:
        r = _put(c, room, b"from the other environment " + uuid.uuid4().bytes, str(uuid.uuid4()))
        assert r.status_code == 423 and r.json() == {"error": "attachments_paused", "reason": "store_mismatch"}
    superuser_src.execute("UPDATE attachment_store SET frozen = true, freeze_watermark = now()")
    with client(_env(pgb, tmp_path, "AWS", s3=s3_target)) as c:
        key = str(uuid.uuid4())
        body = b"during freeze " + uuid.uuid4().bytes
        r = _put(c, room, body, key)
        assert r.status_code == 423 and r.json()["reason"] == "frozen"
        superuser_src.execute("UPDATE attachment_store SET frozen = false, freeze_watermark = NULL")
        r = _put(c, room, body, key)  # retry with the same idempotency key
        assert r.status_code == 201


def test_pages_and_qr(pgb: Stack, tmp_path: Path) -> None:
    env = _env(pgb, tmp_path, "AWS")
    env["APP_PUBLIC_URL"] = "https://chat.example.test"
    with client(env) as c:
        assert "금융 상담실" in c.get("/").text
        assert c.get("/join").status_code == 200
        qr = c.get("/qr.svg?room=consult-1")
        assert qr.status_code == 200 and qr.text.startswith("<svg") and 'xmlns="http://www.w3.org/2000/svg"' in qr.text
        cfg = c.get("/api/config").json()
        assert cfg["join_url"] == "https://chat.example.test/?room=consult-1"
        _wait_ready(c)
        assert c.get("/qr.svg?room=../../x").status_code == 400
        assert "default-src 'self'" in c.get("/").headers["content-security-policy"]


def test_amazon_s3_read_only() -> None:
    """Read-only check against the real source bucket: reachable, credentials accepted, a random
    content key is absent. Nothing is written to Amazon S3 by the tests."""
    import hashlib
    import os

    from bravo_demo.storage import ObjectStore, object_key

    bucket = os.environ.get("BRAVO_TEST_AWS_S3_BUCKET", "").strip()
    if not bucket:
        pytest.skip("BRAVO_TEST_AWS_S3_BUCKET not set (source bucket: terraform -chdir=infra/envs/source-aws "
                    "output attachments_bucket) plus AWS credentials allowed by its bucket policy")
    settings = config.load({
        "APP_ENV_ID": "aws", "APP_ENV_LABEL": "AWS", "DB_DSN": "host=x dbname=chat user=app_writer",
        "DB_PASSWORD_SOURCE": "env-file", "DB_PASSWORD_FILE": "/dev/null",
        "S3_BUCKET": bucket, "S3_REGION": os.environ.get("BRAVO_TEST_AWS_S3_REGION", ""),
        "S3_ADDRESSING_STYLE": "virtual", "ATTACHMENT_STORE_ID": "src",
    })
    assert settings.s3 is not None
    store = ObjectStore(settings.s3)
    store.ping()
    assert store.exists(object_key(hashlib.sha256(uuid.uuid4().bytes).hexdigest())) is False


def test_secrets_manager_source() -> None:
    import os

    secret_id = os.environ.get("BRAVO_TEST_AWS_SECRET_ID")
    if not secret_id:
        pytest.skip("BRAVO_TEST_AWS_SECRET_ID not set: needs a real AWS Secrets Manager secret and credentials")
    from bravo_demo import secrets as app_secrets

    settings = config.load({
        "APP_ENV_ID": "aws", "APP_ENV_LABEL": "AWS", "DB_DSN": "host=x dbname=chat user=app_writer",
        "DB_PASSWORD_SOURCE": "aws-secrets-manager", "DB_PASSWORD_SECRET_ID": secret_id,
        "DB_PASSWORD_SECRET_KEY": os.environ.get("BRAVO_TEST_AWS_SECRET_KEY", ""),
        "AWS_REGION": os.environ.get("AWS_REGION", ""),
    })
    assert app_secrets.db_password(settings.db)
