"""HTTP and WebSocket surface of the demo chat (§8.10, T4, T22).

WebSocket protocol (``/ws?room=<room>&after=<last id>``), JSON text frames:

  server -> client
    hello     {env:{id,label,label_ja}, instance, room, limits}
    messages  {items:[message...], backlog:bool}
    ack       {idem_key, message, created}
    nack      {idem_key, code, retryable}       code: write_timeout | conflict | invalid |
                                                       attachment_missing | db_error
    status    {db_ok, poll_age_ms}              every STATUS_INTERVAL_S; drives the live badge
  client -> server
    send      {idem_key, author, body, attachment_id?}

On server shutdown uvicorn closes every socket with 1012 (service restart). The client then
reconnects with ``after=<last id>`` and resends unacknowledged writes with the same keys.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import random
import re
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager, suppress
from importlib import resources
from typing import Any
from urllib.parse import quote, unquote

import segno
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from . import secrets
from .admin import create_admin_app
from .config import Settings
from .db import (
    AttachmentNotFound,
    AttachmentsPaused,
    ChatDb,
    IdempotencyConflict,
    WritesBlocked,
    WriteTimeout,
    message_public,
)
from .fanout import Hub, Poller
from .storage import ObjectMissing, ObjectStore, StoreError, object_key
from .telemetry import Instruments

log = logging.getLogger(__name__)

STATUS_INTERVAL_S = 2.0
STORE_CHECK_INTERVAL_S = 15.0
MAX_INFLIGHT_PER_SOCKET = 16
MAX_AUTHOR = 40
MAX_BODY = 2000
ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PREVIEW_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
ALLOWED_TYPES = PREVIEW_TYPES | {"application/pdf", "text/plain"}


def _static(name: str) -> str:
    return resources.files("bravo_demo").joinpath("static", name).read_text(encoding="utf-8")


def _clean_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value).strip()
    value = "".join(ch for ch in value if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    if not value or len(value) > limit:
        return None
    return value


def _clean_filename(raw: str | None) -> str:
    name = unquote(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip(" .")
    return (name or "file")[:200]


def _uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class _TraceMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, instruments: Instruments) -> None:
        super().__init__(app)
        self.instruments = instruments

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in ("/healthz", "/readyz"):
            return await call_next(request)
        route = request.url.path if not request.url.path.startswith("/api/attachments/") else "/api/attachments/{id}"
        with self.instruments.tracer.start_as_current_span(
            f"{request.method} {route}", kind=SpanKind.SERVER,
            attributes={"http.request.method": request.method, "url.path": route},
        ) as span:
            response = await call_next(request)
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR))
            return response


def create_app(settings: Settings) -> FastAPI:
    instruments = Instruments()
    hub = Hub()
    state: dict[str, Any] = {"ready": False}

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001
        password = await asyncio.to_thread(secrets.db_password, settings.db)
        db = ChatDb(settings.db, password, f"bravo-demo/{settings.env_id}/{settings.instance_id}", settings.env_id)
        del password
        await db.open()

        async def ensure_switch_row() -> None:
            while True:
                try:
                    await db.ensure_write_block_row(settings.instance_id)
                    return
                except Exception as exc:  # retried until the database answers
                    log.warning("write_block row not ensured yet: %s", type(exc).__name__)
                    await asyncio.sleep(2.0)

        switch_task = asyncio.create_task(ensure_switch_row(), name="ensure-write-block-row")
        store = ObjectStore(settings.s3) if settings.s3 else None

        async def watch_store() -> None:
            # Attachments are offered only after a real HeadBucket succeeds, and withdrawn when it fails.
            assert store is not None
            while True:
                try:
                    await asyncio.to_thread(store.ping)
                    if not state.get("store_ok"):
                        log.info("object store reachable", extra={"store_id": store.store_id})
                    state["store_ok"] = True
                except Exception as exc:
                    if state.get("store_ok") is not False:
                        log.warning("object store not reachable: %s", exc, extra={"store_id": store.store_id})
                    state["store_ok"] = False
                await asyncio.sleep(STORE_CHECK_INTERVAL_S)

        store_task = asyncio.create_task(watch_store(), name="watch-object-store") if store else None
        poller = Poller(
            db, hub, interval_s=settings.poll_interval_s, grace_s=settings.gap_grace_s,
            to_message=message_public,
            on_poll=lambda ms, ok: instruments.poll_ms.record(ms, {"outcome": "ok" if ok else "error"}),
        )
        poller.start()
        state.update(db=db, store=store, poller=poller, ready=True)
        log.info(
            "demo app started",
            extra={"env_label": settings.env_label, "attachments": store is not None,
                   "password_source": settings.db.password_source},
        )
        try:
            yield
        finally:
            state["ready"] = False
            switch_task.cancel()
            if store_task is not None:
                store_task.cancel()
            await poller.stop()
            await db.close()

    app = FastAPI(title="Bravo demo chat", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_TraceMiddleware, instruments=instruments)
    # The write-block switch listens on its own port (ADMIN_PORT) and shares this runtime state.
    app.state.admin_app = create_admin_app(settings, state, instruments)
    retry_after = str(settings.admin.retry_after_s)

    def blocked_response() -> JSONResponse:
        return JSONResponse(
            {"error": "writes_blocked", "retryable": True, "retry_after_s": settings.admin.retry_after_s},
            status_code=503,
            headers={"Retry-After": retry_after},
        )

    def env_doc() -> dict[str, Any]:
        return {"id": settings.env_id, "label": settings.env_label, "label_ja": settings.env_label_ja}

    def attachments_state() -> str:
        # off: no bucket configured | checking: no HeadBucket answer yet | ok | unavailable
        if settings.s3 is None:
            return "off"
        ok = state.get("store_ok")
        return "checking" if ok is None else ("ok" if ok else "unavailable")

    def db_status() -> tuple[bool, int | None]:
        """True only when the last poll of the write DB succeeded recently (no answer yet = False)."""
        poller: Poller | None = state.get("poller")
        if poller is None or poller.last_ok is None:
            return False, None
        age = int((time.monotonic() - poller.last_ok) * 1000)
        return age < max(3000, int(settings.poll_interval_s * 4000)), age

    def limits_doc() -> dict[str, Any]:
        return {
            "attachments": attachments_state() == "ok",
            "attachments_state": attachments_state(),
            "max_attachment_bytes": settings.s3.max_bytes if settings.s3 else 0,
            "write_wait_s": settings.db.write_wait_s,
            "max_body": MAX_BODY,
            "max_author": MAX_AUTHOR,
        }

    @app.middleware("http")
    async def injected_fault(request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 장애 주입 (admin PUT /admin/fault): health paths stay up so the pod and the LB monitor keep it in.
        rate = state.get("fault_5xx_rate", 0.0)
        if rate and request.url.path not in ("/healthz", "/readyz") and random.random() < rate:  # noqa: S311
            return JSONResponse({"error": "injected_fault", "env": settings.env_id}, status_code=500,
                                headers={"X-Served-By": settings.env_id, "X-Bravo-Fault": "injected"})
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        response.headers.setdefault("X-Served-By", settings.env_id)
        return response

    # --- pages --------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_static("index.html"), headers={"Cache-Control": "no-store"})

    @app.get("/join", response_class=HTMLResponse)
    async def join() -> HTMLResponse:
        return HTMLResponse(_static("join.html"), headers={"Cache-Control": "no-store"})

    @app.get("/static/{name}")
    async def static(name: str) -> Response:
        types = {"app.js": "text/javascript", "join.js": "text/javascript", "style.css": "text/css",
                 "i18n.js": "text/javascript"}
        if name not in types:
            return Response(status_code=404)
        return Response(_static(name), media_type=types[name], headers={"Cache-Control": "no-cache"})

    def _base_url(request: Request) -> str:
        if settings.public_url:
            return settings.public_url.rstrip("/")
        return str(request.base_url).rstrip("/")

    @app.get("/qr.svg")
    async def qr(request: Request, room: str = Query(default="")) -> Response:
        room = room or settings.room_default
        if not ROOM_RE.match(room):
            return Response(status_code=400)
        url = f"{_base_url(request)}/?room={quote(room)}"
        buf = io.BytesIO()
        # A standalone SVG document (with xmlns): it is loaded through <img>, not inlined.
        segno.make(url, error="m").save(buf, kind="svg", scale=8, border=2, dark="#111827", light="#ffffff",
                                        xmldecl=False)
        return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    @app.get("/api/config")
    async def config(request: Request) -> dict[str, Any]:
        return {
            "env": env_doc(),
            "instance": settings.instance_id,
            "room_default": settings.room_default,
            "join_url": f"{_base_url(request)}/?room={quote(settings.room_default)}",
            "limits": limits_doc(),
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        # Ready only after this process has read the write DB (the poller's queries go through
        # PgBouncer) and only while its last successful read is younger than READY_DB_STALE_S.
        # The window is longer than any write pause (seconds): a PgBouncer PAUSE must not take
        # every pod out of the Service at once, a lost database must.
        poller: Poller | None = state.get("poller")
        if not state.get("ready") or poller is None or poller.last_ok is None:
            return JSONResponse({"status": "starting", "db": "not_reached"}, status_code=503)
        age = time.monotonic() - poller.last_ok
        if age > settings.ready_db_stale_s:
            return JSONResponse({"status": "db_unreachable", "db_age_s": round(age, 1)}, status_code=503)
        return JSONResponse({"status": "ready", "db_age_s": round(age, 1), "attachments": attachments_state()})

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        poller: Poller | None = state.get("poller")
        age = None if not poller or poller.last_ok is None else int((time.monotonic() - poller.last_ok) * 1000)
        return {"env": env_doc(), "instance": settings.instance_id, "poll_age_ms": age,
                "ws_connections": hub.connections, "attachments": attachments_state()}

    # --- attachments (D-04) -------------------------------------------------------------------

    def _err(status_code: int, code: str, **extra: Any) -> JSONResponse:
        return JSONResponse({"error": code, **extra}, status_code=status_code)

    @app.put("/api/rooms/{room}/attachments")
    async def upload(room: str, request: Request) -> Response:
        store: ObjectStore | None = state.get("store")
        db: ChatDb = state["db"]
        if store is None or settings.s3 is None:
            return _err(404, "attachments_disabled")
        if attachments_state() != "ok":
            return JSONResponse({"error": "attachments_unavailable", "state": attachments_state(), "retryable": True},
                                status_code=503, headers={"Retry-After": str(int(STORE_CHECK_INTERVAL_S))})
        if not ROOM_RE.match(room):
            return _err(400, "invalid_room")
        idem_key = _uuid(request.headers.get("idempotency-key"))
        if idem_key is None:
            return _err(400, "idempotency_key_required")
        author = _clean_text(unquote(request.headers.get("x-author", "")), MAX_AUTHOR)
        if author is None:
            return _err(400, "invalid_author")
        logical_id = _uuid(request.headers.get("x-logical-id")) or uuid.uuid4()
        filename = _clean_filename(request.headers.get("x-filename"))
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type not in ALLOWED_TYPES:
            content_type = "application/octet-stream"
        max_bytes = settings.s3.max_bytes
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            return _err(413, "too_large", max_bytes=max_bytes)

        t0 = time.monotonic()
        with instruments.tracer.start_as_current_span("attachment.upload") as span:
            buf = bytearray()
            async for chunk in request.stream():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    instruments.attachments.add(1, {"outcome": "too_large"})
                    return _err(413, "too_large", max_bytes=max_bytes)
            if not buf:
                return _err(400, "empty")
            data = bytes(buf)
            digest = hashlib.sha256(data).hexdigest()
            key = object_key(digest)
            span.set_attribute("bravo.attachment.sha256", digest)
            span.set_attribute("bravo.attachment.store_id", store.store_id)
            try:
                # Do not touch the object store while writes are blocked; the row insert checks again.
                if (await db.write_block_state(settings.db.write_wait_s)).blocked:
                    raise WritesBlocked(None)
                existing = await db.attachment_by_idem(idem_key)
                if existing is not None:
                    if existing.sha256 != digest or existing.room != room:
                        return _err(409, "idempotency_conflict")
                    instruments.attachments.add(1, {"outcome": "replayed"})
                    return JSONResponse({"attachment": existing.public(), "created": False})
                # D-04 order: 1 store the object, 2 verify its SHA-256, 3 commit the row.
                await asyncio.to_thread(store.put_if_absent, key, data, content_type)
                await asyncio.to_thread(store.verify, key, digest, max_bytes)
                att, created = await db.insert_attachment(
                    logical_id=logical_id, idem_key=idem_key, room=room, author=author, sha256=digest,
                    size_bytes=len(data), content_type=content_type, filename=filename, store_id=store.store_id,
                )
            except AttachmentsPaused as exc:
                instruments.attachments.add(1, {"outcome": f"paused_{exc.reason}"})
                log.info("attachment rejected by store guard", extra={"reason": exc.reason})
                return _err(423, "attachments_paused", reason=exc.reason)
            except IdempotencyConflict:
                return _err(409, "idempotency_conflict")
            except WritesBlocked:
                instruments.attachments.add(1, {"outcome": "writes_blocked"})
                return blocked_response()
            except WriteTimeout:
                instruments.attachments.add(1, {"outcome": "write_timeout"})
                return _err(503, "write_timeout", retryable=True)
            except StoreError as exc:
                instruments.attachments.add(1, {"outcome": "store_error"})
                log.warning("object store error: %s", exc)
                span.set_status(Status(StatusCode.ERROR))
                return _err(502, "store_error", retryable=True)
            instruments.attachments.add(1, {"outcome": "created" if created else "replayed"})
            instruments.write_ms.record((time.monotonic() - t0) * 1000.0, {"kind": "attachment"})
            return JSONResponse({"attachment": att.public(), "created": created}, status_code=201 if created else 200)

    @app.post("/api/rooms/{room}/messages")
    async def post_message(room: str, request: Request) -> Response:
        """HTTP form of the WebSocket `send` (same idempotency and write-block rules)."""
        if not state.get("ready"):
            return _err(503, "starting", retryable=True)
        db: ChatDb = state["db"]
        if not ROOM_RE.match(room):
            return _err(400, "invalid_room")
        idem_key = _uuid(request.headers.get("idempotency-key"))
        if idem_key is None:
            return _err(400, "idempotency_key_required")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > 16_384:
            return _err(413, "too_large")
        try:
            doc = await request.json()
        except ValueError:
            return _err(400, "invalid_json")
        if not isinstance(doc, dict):
            return _err(400, "invalid_json")
        author = _clean_text(doc.get("author"), MAX_AUTHOR)
        body = _clean_text(doc.get("body"), MAX_BODY)
        att_raw = doc.get("attachment_id")
        attachment_id = _uuid(att_raw) if att_raw else None
        if author is None or body is None or (att_raw and attachment_id is None):
            return _err(400, "invalid")
        t0 = time.monotonic()
        outcome = "ok"
        try:
            message, created = await db.send_message(
                room=room, author=author, body=body, idem_key=idem_key, attachment_id=attachment_id
            )
            return JSONResponse({"message": message, "created": created}, status_code=201 if created else 200)
        except WritesBlocked:
            outcome = "blocked"
            return blocked_response()
        except WriteTimeout:
            outcome = "timeout"
            return JSONResponse({"error": "write_timeout", "retryable": True}, status_code=503,
                                headers={"Retry-After": retry_after})
        except IdempotencyConflict:
            outcome = "conflict"
            return _err(409, "idempotency_conflict")
        except AttachmentNotFound:
            outcome = "attachment_missing"
            return _err(422, "attachment_missing")
        finally:
            instruments.write_ms.record((time.monotonic() - t0) * 1000.0, {"kind": "message", "outcome": outcome})
            instruments.writes.add(1, {"kind": "message", "outcome": outcome})

    @app.get("/api/attachments/{attachment_id}")
    async def download(attachment_id: str) -> Response:
        store: ObjectStore | None = state.get("store")
        db: ChatDb = state["db"]
        if store is None or settings.s3 is None:
            return _err(404, "attachments_disabled")
        aid = _uuid(attachment_id)
        if aid is None:
            return _err(404, "not_found")
        try:
            att = await db.attachment(aid)
        except WriteTimeout:
            return _err(503, "db_waiting", retryable=True)
        if att is None:
            return _err(404, "not_found")
        if att.deleted_at is not None:
            return _err(410, "deleted")
        try:
            data, _ = await asyncio.to_thread(store.read, att.object_key, settings.s3.max_bytes)
        except ObjectMissing:
            return _err(404, "object_not_in_this_store", store_id=store.store_id)
        except StoreError:
            return _err(502, "store_error", retryable=True)
        if hashlib.sha256(data).hexdigest() != att.sha256:
            return _err(502, "integrity_mismatch")
        disposition = "inline" if att.content_type in PREVIEW_TYPES else "attachment"
        return Response(
            data,
            media_type=att.content_type,
            headers={
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(att.filename)}",
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "default-src 'none'; img-src 'self'; sandbox",
                "ETag": f'"{att.sha256}"',
            },
        )

    # --- WebSocket ----------------------------------------------------------------------------

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, room: str = Query(default=""), after: int = Query(default=0, ge=0)) -> None:
        room = room or settings.room_default
        if not ROOM_RE.match(room):
            await websocket.close(code=1008)
            return
        if not state.get("ready"):
            await websocket.close(code=1013)
            return
        db: ChatDb = state["db"]
        await websocket.accept()
        queue = hub.subscribe(room)  # subscribe before reading history: nothing falls between
        instruments.ws_connections.add(1, {"env": settings.env_id})
        inflight: set[asyncio.Task[None]] = set()
        send_lock = asyncio.Lock()

        async def send(doc: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))

        async def reply(doc: dict[str, Any]) -> None:
            # Write results may arrive after the socket closed; the client resends on reconnect.
            with suppress(Exception):
                await send(doc)

        async def handle_send(cmd: dict[str, Any]) -> None:
            idem_key = _uuid(cmd.get("idem_key"))
            author = _clean_text(cmd.get("author"), MAX_AUTHOR)
            body = _clean_text(cmd.get("body"), MAX_BODY)
            att_raw = cmd.get("attachment_id")
            attachment_id = _uuid(att_raw) if att_raw else None
            if idem_key is None or author is None or body is None or (att_raw and attachment_id is None):
                await reply({"type": "nack", "idem_key": cmd.get("idem_key"), "code": "invalid", "retryable": False})
                return
            t0 = time.monotonic()
            outcome = "ok"
            with instruments.tracer.start_as_current_span("chat.send", attributes={"bravo.room": room}) as span:
                try:
                    message, created = await db.send_message(
                        room=room, author=author, body=body, idem_key=idem_key, attachment_id=attachment_id
                    )
                    await reply({"type": "ack", "idem_key": str(idem_key), "message": message, "created": created})
                    return
                except WritesBlocked:
                    outcome = "blocked"
                    await reply({"type": "nack", "idem_key": str(idem_key), "code": "writes_blocked",
                                 "retryable": True, "status": 503, "retry_after_s": settings.admin.retry_after_s})
                    return
                except WriteTimeout:
                    outcome, code, retryable = "timeout", "write_timeout", True
                except IdempotencyConflict:
                    outcome, code, retryable = "conflict", "conflict", False
                except AttachmentNotFound:
                    outcome, code, retryable = "attachment_missing", "attachment_missing", False
                except Exception as exc:
                    log.warning("send failed: %s", type(exc).__name__)
                    span.set_status(Status(StatusCode.ERROR))
                    outcome, code, retryable = "error", "db_error", True
                finally:
                    elapsed = (time.monotonic() - t0) * 1000.0
                    instruments.write_ms.record(elapsed, {"kind": "message", "outcome": outcome})
                    instruments.writes.add(1, {"kind": "message", "outcome": outcome})
                    span.set_attribute("bravo.write.outcome", outcome)
                await reply({"type": "nack", "idem_key": str(idem_key), "code": code, "retryable": retryable})

        async def sender() -> None:
            db_ok, _ = db_status()
            await send({"type": "hello", "env": env_doc(), "instance": settings.instance_id, "room": room,
                        "limits": limits_doc(), "db_ok": db_ok})
            if after > 0:
                items = await db.fetch_after(room, after, settings.backlog_limit)
            else:
                items = await db.fetch_recent(room, settings.backlog_limit)
            await send({"type": "messages", "items": items, "backlog": True})
            next_status = 0.0
            while True:
                timeout = max(0.0, next_status - time.monotonic())
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    db_ok, age = db_status()
                    await send({"type": "status", "db_ok": db_ok, "poll_age_ms": age,
                                "attachments_state": attachments_state()})
                    next_status = time.monotonic() + STATUS_INTERVAL_S
                    continue
                if item is None:  # slow consumer: let it reconnect with its last id
                    await websocket.close(code=1013)
                    return
                batch = [item]
                while not queue.empty() and len(batch) < 200:
                    nxt = queue.get_nowait()
                    if nxt is None:
                        await websocket.close(code=1013)
                        return
                    batch.append(nxt)
                await send({"type": "messages", "items": batch, "backlog": False})

        async def receiver() -> None:
            while True:
                raw = await websocket.receive_text()
                if len(raw) > 16_384:
                    await websocket.close(code=1009)
                    return
                try:
                    cmd = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(cmd, dict) or cmd.get("type") != "send":
                    continue
                if len(inflight) >= MAX_INFLIGHT_PER_SOCKET:
                    await send({"type": "nack", "idem_key": cmd.get("idem_key"), "code": "busy", "retryable": True})
                    continue
                task = asyncio.create_task(handle_send(cmd))
                inflight.add(task)
                task.add_done_callback(inflight.discard)

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    if isinstance(exc, WriteTimeout):  # history read waited too long (write pause)
                        with suppress(Exception):
                            await websocket.close(code=1013)
                    else:
                        log.warning("websocket task failed: %s", type(exc).__name__)
        finally:
            # In-flight writes are not cancelled: each ends within WRITE_WAIT_S on its own.
            for t in tasks:
                t.cancel()
            hub.unsubscribe(room, queue)
            instruments.ws_connections.add(-1, {"env": settings.env_id})
            with suppress(Exception):
                await websocket.close()

    return app
