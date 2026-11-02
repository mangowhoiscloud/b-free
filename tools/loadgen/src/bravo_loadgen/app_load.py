"""Open-loop chat load through the customer app's public surface (spec §5.3, T18).

Writes: ``POST /api/rooms/<room>/messages`` (the HTTP form of the WebSocket ``send``). The request ID
is the ``Idempotency-Key`` and the body carries the marker ``load message <request id>``. Every
attempt has its own attempt ID (header ``X-Bravo-Attempt-Id``, recorded in the event log). 503 (write
blocked or waiting, ``Retry-After``), any other 5xx, 429, timeouts and transport errors are retried with
the same key; 200 is a replay of an already committed key. Anything else ends the request as failed.

Reads: what the app serves for reading a room. A browser that opens or reconnects to a room opens
``/ws?room=<room>&after=<last id>`` and gets ``hello`` and then the ``messages`` backlog, read from the
write DB. One read = connect, ``hello``, backlog, close. ``after`` trails the newest acked write by
``read_window`` ids, so each read returns at most that many rows. Reads are not retried.

Requests start at fixed times whatever the latency (open loop): a write pause shows up as latency and
retries, not as a lower offered rate. Which environment answered comes from the app itself: the
``X-Served-By`` response header (HTTP) and ``hello.env.id`` (WebSocket).

One Python process tops out near one core well below 50 + 150 req/s (TLS for every WebSocket read).
``workers`` splits both schedules over that many processes, interleaved so that the combined schedule
stays uniform, and the results are merged. The client's own load is reported (``client``) so that a
saturated client is never mistaken for a slow app.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import re
import resource
import signal
import ssl
import sys
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from .generator import body_marker
from .metrics import LoadMetrics
from .stats import percentile

_ERR_TOKEN = re.compile(r"^[a-z_]{1,40}$")


@dataclass(frozen=True, slots=True)
class AppLoadConfig:
    base_url: str
    write_rate: float
    read_rate: float
    duration_s: float
    room: str
    author: str = "loadgen"
    attempt_timeout_s: float = 15.0  # spec §5.3 client timeout
    max_attempts: int = 40
    request_deadline_s: float = 120.0
    drain_timeout_s: float = 60.0
    read_timeout_s: float = 15.0
    read_window: int = 20
    max_connections: int = 512
    # Open requests per process. Above it a due request is not sent and counts as failed (client_saturated):
    # a collapsed target must not turn the client into thousands of sockets (11/3 02:44: ~9,400 on the Mac)
    # or into an OOM kill (11/3 03:09: 1.6 GiB in 22 s in the platform Job). A healthy run needs a few dozen.
    max_inflight: int = 200


@dataclass(frozen=True, slots=True)
class MetricsConfig:
    endpoint: str | None
    run_id: str
    plan_id: str | None
    interval_s: float


@dataclass(slots=True)
class OpStats:
    sent: int = 0
    ok: int = 0
    replay: int = 0  # writes: 200 for an already committed key
    failed: int = 0  # logical requests that ended without success
    retried_requests: int = 0
    attempts: int = 0
    attempt_errors: Counter[str] = field(default_factory=Counter)
    latencies_ms: list[float] = field(default_factory=list)  # logical request, scheduled -> success
    ok_at: list[float] = field(default_factory=list)  # seconds since the common start
    served_by: Counter[str] = field(default_factory=Counter)
    items: int = 0  # reads: rows returned

    def merge(self, o: OpStats) -> None:
        for name in ("sent", "ok", "replay", "failed", "retried_requests", "attempts", "items"):
            setattr(self, name, getattr(self, name) + getattr(o, name))
        self.attempt_errors.update(o.attempt_errors)
        self.served_by.update(o.served_by)
        self.latencies_ms.extend(o.latencies_ms)
        self.ok_at.extend(o.ok_at)

    def summary(self, window_s: float) -> dict[str, Any]:
        lat = self.latencies_ms
        at = sorted(self.ok_at)
        gap = max((b - a for a, b in zip(at, at[1:], strict=False)), default=0.0)
        return {
            "sent": self.sent,
            "ok": self.ok,
            "replay": self.replay,
            "failed": self.failed,
            "retried_requests": self.retried_requests,
            "attempts": self.attempts,
            "attempt_errors": dict(self.attempt_errors.most_common()),
            "offered_per_s": round(self.sent / window_s, 2) if window_s > 0 else None,
            "achieved_ok_per_s": round(self.ok / window_s, 2) if window_s > 0 else None,
            "latency_ms": {
                "p50": _r(percentile(lat, 50)),
                "p95": _r(percentile(lat, 95)),
                "p99": _r(percentile(lat, 99)),
                "max": _r(max(lat)) if lat else None,
            },
            "longest_ok_gap_ms": round(gap * 1000, 1),
            "served_by": dict(self.served_by),
            **({"rows_returned_mean": round(self.items / self.ok, 2)} if self.ok and self.items else {}),
        }


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


@dataclass(slots=True)
class Second:
    write_ok: int = 0
    write_err_attempts: int = 0
    read_ok: int = 0
    read_err: int = 0
    write_ms: list[float] = field(default_factory=list)
    read_ms: list[float] = field(default_factory=list)
    served_by: Counter[str] = field(default_factory=Counter)

    def merge(self, o: Second) -> None:
        self.write_ok += o.write_ok
        self.write_err_attempts += o.write_err_attempts
        self.read_ok += o.read_ok
        self.read_err += o.read_err
        self.write_ms.extend(o.write_ms)
        self.read_ms.extend(o.read_ms)
        self.served_by.update(o.served_by)

    def doc(self, t: int) -> dict[str, Any]:
        return {
            "t": t,
            "write_ok": self.write_ok,
            "write_err_attempts": self.write_err_attempts,
            "write_p95_ms": _r(percentile(self.write_ms, 95)),
            "read_ok": self.read_ok,
            "read_err": self.read_err,
            "read_p95_ms": _r(percentile(self.read_ms, 95)),
            "served_by": dict(self.served_by),
        }


@dataclass(slots=True)
class RequestRecord:
    request_id: str
    outcome: str  # acked | failed
    attempts: int
    id: int | None = None
    served_by: str | None = None
    replay: bool = False
    latency_ms: float | None = None
    last_error: str | None = None


@dataclass(slots=True)
class WorkerResult:
    writes: OpStats
    reads: OpStats
    records: list[RequestRecord]
    seconds: dict[int, Second]
    send_window_s: float
    cpu_s: float
    max_inflight_w: int
    max_inflight_r: int
    sched_lag_ms: list[float]
    loop_lag_max_ms: float


def _event(kind: str, **fields: Any) -> None:
    sys.stderr.write(json.dumps({"event": kind, "ts": round(time.time(), 3), **fields}) + "\n")


def _ws_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    scheme = {"https": "wss", "http": "ws"}[parts.scheme]
    return urlunsplit((scheme, parts.netloc, "/ws", "", ""))


def _cpu_s() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime + ru.ru_stime


class AppLoad:
    """One process: its share of both schedules, starting at ``start_wall`` (epoch seconds)."""

    def __init__(self, cfg: AppLoadConfig, metrics: LoadMetrics, start_wall: float,
                 phase: float = 0.0) -> None:
        self.cfg = cfg
        self.metrics = metrics
        self.start_wall = start_wall
        self.phase = phase  # fraction of one interval: interleaves the workers' schedules
        self.writes = OpStats()
        self.reads = OpStats()
        self.records: list[RequestRecord] = []
        self.seconds: dict[int, Second] = defaultdict(Second)
        self._stop = asyncio.Event()
        self._t0 = 0.0
        self._last_id = 0
        self._write_url = f"{cfg.base_url.rstrip('/')}/api/rooms/{quote(cfg.room)}/messages"
        self._ws_base = _ws_url(cfg.base_url)
        self._inflight_w = 0
        self._inflight_r = 0
        self.max_inflight_w = 0
        self.max_inflight_r = 0
        self.sched_lag_ms: list[float] = []
        self.loop_lag_max_ms = 0.0
        # One TLS context per process for every WebSocket read (a new context per connection costs CPU and memory).
        self._ssl = ssl.create_default_context() if self._ws_base.startswith("wss:") else None

    def stop(self) -> None:
        self._stop.set()

    def _sec(self, now: float) -> Second:
        return self.seconds[int(now - self._t0)]

    def _refuse(self, op: str) -> None:
        st = self.writes if op == "write" else self.reads
        st.failed += 1
        st.attempt_errors["client_saturated"] += 1
        self.metrics.attempt(op, False, "client_saturated", "unknown", 0.0)
        now = time.monotonic()
        if op == "write":
            self._sec(now).write_err_attempts += 1
            self.records.append(RequestRecord(str(uuid.uuid4()), "failed", 0, None, None, False, None,
                                              "client_saturated"))
        else:
            self._sec(now).read_err += 1

    # --- writes --------------------------------------------------------------------------------

    async def _write(self, client: httpx.AsyncClient, scheduled: float) -> None:
        cfg, st = self.cfg, self.writes
        rid = uuid.uuid4()
        body = {"author": cfg.author, "body": body_marker(rid)}
        deadline = scheduled + cfg.request_deadline_s
        attempts = 0
        last_code = None
        served = "unknown"
        self._inflight_w += 1
        self.max_inflight_w = max(self.max_inflight_w, self._inflight_w)
        try:
            while True:
                attempts += 1
                st.attempts += 1
                aid = uuid.uuid4()
                t0 = time.monotonic()
                retry_after = None
                try:
                    r = await client.post(
                        self._write_url, json=body,
                        headers={"Idempotency-Key": str(rid), "X-Bravo-Attempt-Id": str(aid)},
                    )
                    status = r.status_code
                    served = r.headers.get("x-served-by", "unknown")
                    if status in (200, 201):
                        msg_id = int(r.json()["message"]["id"])
                        now = time.monotonic()
                        lat = (now - scheduled) * 1000
                        self.metrics.attempt("write", True, str(status), served, (now - t0) * 1000)
                        self.metrics.request("write", "acked", served, lat)
                        st.ok += 1
                        st.latencies_ms.append(lat)
                        st.ok_at.append(now - self._t0)
                        st.served_by[served] += 1
                        sec = self._sec(now)
                        sec.write_ok += 1
                        sec.write_ms.append(lat)
                        sec.served_by[f"w:{served}"] += 1
                        if status == 200:
                            st.replay += 1
                        self._last_id = max(self._last_id, msg_id)
                        self.records.append(RequestRecord(str(rid), "acked", attempts, msg_id, served,
                                                          status == 200, round(lat, 1), last_code))
                        if attempts > 1 or status == 200:
                            _event("acked_after_retry", request_id=str(rid), attempt_id=str(aid), attempt=attempts,
                                   id=msg_id, replay=status == 200, served_by=served, latency_ms=round(lat, 1))
                        return
                    code = str(status)
                    try:
                        err = r.json().get("error")
                        if isinstance(err, str) and _ERR_TOKEN.match(err):
                            code = f"{status}:{err}"
                    except (ValueError, AttributeError):
                        pass
                    retryable = status >= 500 or status == 429
                    ra = r.headers.get("retry-after")
                    if ra and ra.isdigit():
                        retry_after = min(float(ra), 5.0)
                except httpx.TimeoutException:
                    code, retryable = "timeout", True
                except httpx.TransportError as exc:
                    code, retryable = f"transport:{type(exc).__name__}", True
                except (ValueError, KeyError, TypeError):  # 2xx without the documented body
                    code, retryable = "bad_body", True
                now = time.monotonic()
                self.metrics.attempt("write", False, code, served, (now - t0) * 1000)
                st.attempt_errors[code] += 1
                self._sec(now).write_err_attempts += 1
                if attempts == 1:
                    st.retried_requests += 1
                last_code = code
                _event("attempt_failed", request_id=str(rid), attempt_id=str(aid), attempt=attempts, code=code,
                       served_by=served)
                if not retryable or attempts >= cfg.max_attempts or now >= deadline:
                    st.failed += 1
                    self.metrics.request("write", "failed", served, (now - scheduled) * 1000)
                    self.records.append(RequestRecord(str(rid), "failed", attempts, None, served, False, None, code))
                    _event("request_failed", request_id=str(rid), attempts=attempts, code=code)
                    return
                backoff = retry_after if retry_after is not None else min(0.1 * 2 ** (attempts - 1), 2.0)
                await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            # Still open after the drain: a failed request, kept in the request log (its row may have committed).
            st.failed += 1
            st.attempt_errors["cancelled_after_drain"] += 1
            self.records.append(RequestRecord(str(rid), "failed", attempts, None, served, False, None,
                                              "cancelled_after_drain"))
            raise
        finally:
            self._inflight_w -= 1

    # --- reads ---------------------------------------------------------------------------------

    async def _read(self, scheduled: float) -> None:
        cfg, st = self.cfg, self.reads
        after = max(0, self._last_id - cfg.read_window)
        url = f"{self._ws_base}?room={quote(cfg.room)}&after={after}"
        served = "unknown"
        code = "ok"
        st.attempts += 1
        self._inflight_r += 1
        self.max_inflight_r = max(self.max_inflight_r, self._inflight_r)
        t0 = now = time.monotonic()
        try:
            async with asyncio.timeout(cfg.read_timeout_s):
                async with ws_connect(url, ssl=self._ssl, open_timeout=cfg.read_timeout_s, close_timeout=2,
                                      compression=None, max_size=2**22, user_agent_header="bravo-loadgen") as ws:
                    hello = json.loads(await ws.recv())
                    if hello.get("type") != "hello":
                        raise ValueError("first frame is not hello")
                    served = str((hello.get("env") or {}).get("id") or "unknown")
                    backlog = json.loads(await ws.recv())
                    if backlog.get("type") != "messages" or backlog.get("backlog") is not True:
                        raise ValueError("second frame is not the backlog")
                    items = backlog.get("items") or []
                    if any(it.get("room") != cfg.room or int(it.get("id", 0)) <= after for it in items):
                        raise ValueError("backlog rows outside the requested room/after")
                    now = time.monotonic()
                    st.items += len(items)
        except InvalidStatus as exc:
            code = f"http_{exc.response.status_code}"
        except ConnectionClosed as exc:
            code = f"close_{exc.rcvd.code}" if exc.rcvd is not None else "close_abnormal"
        except TimeoutError:
            code = "timeout"
        except InvalidHandshake as exc:
            code = f"handshake:{type(exc).__name__}"
        except OSError as exc:
            code = f"transport:{type(exc).__name__}"
        except (ValueError, KeyError, TypeError):
            code = "bad_frame"
        finally:
            self._inflight_r -= 1
        if code == "ok":
            lat = (now - scheduled) * 1000
            self.metrics.attempt("read", True, "ok", served, (now - t0) * 1000)
            self.metrics.request("read", "ok", served, lat)
            st.ok += 1
            st.latencies_ms.append(lat)
            st.ok_at.append(now - self._t0)
            st.served_by[served] += 1
            sec = self._sec(now)
            sec.read_ok += 1
            sec.read_ms.append(lat)
            sec.served_by[f"r:{served}"] += 1
            return
        now = time.monotonic()
        self.metrics.attempt("read", False, code, served, (now - t0) * 1000)
        self.metrics.request("read", "failed", served, (now - scheduled) * 1000)
        st.attempt_errors[code] += 1
        st.failed += 1
        self._sec(now).read_err += 1

    async def _prime_last_id(self) -> None:
        """One read of the newest rows so that the first reads do not fetch the whole backlog."""
        try:
            async with asyncio.timeout(self.cfg.read_timeout_s):
                async with ws_connect(f"{self._ws_base}?room={quote(self.cfg.room)}", ssl=self._ssl, compression=None,
                                      max_size=2**22, user_agent_header="bravo-loadgen") as ws:
                    await ws.recv()
                    backlog = json.loads(await ws.recv())
                    ids = [int(it["id"]) for it in backlog.get("items") or []]
                    self._last_id = max(ids, default=0)
        except Exception as exc:  # noqa: BLE001 - the run itself measures the errors
            _event("prime_failed", error=type(exc).__name__)

    # --- schedule ------------------------------------------------------------------------------

    async def _loop_lag(self) -> None:
        while not self._stop.is_set():
            t = time.monotonic()
            await asyncio.sleep(0.1)
            self.loop_lag_max_ms = max(self.loop_lag_max_ms, (time.monotonic() - t - 0.1) * 1000)

    async def run(self) -> WorkerResult:
        cfg = self.cfg
        limits = httpx.Limits(max_connections=cfg.max_connections, max_keepalive_connections=cfg.max_connections)
        timeout = httpx.Timeout(cfg.attempt_timeout_s)
        cpu0 = _cpu_s()
        async with httpx.AsyncClient(limits=limits, timeout=timeout, headers={"User-Agent": "bravo-loadgen"}) as client:
            await self._prime_last_id()
            tasks: set[asyncio.Task[None]] = set()
            lag_task = asyncio.create_task(self._loop_lag())
            start = time.monotonic() + max(0.0, self.start_wall - time.time())
            self._t0 = start
            w_int = 1.0 / cfg.write_rate if cfg.write_rate > 0 else None
            r_int = 1.0 / cfg.read_rate if cfg.read_rate > 0 else None
            nw = nr = 0
            while not self._stop.is_set():
                due_w = start + (nw + self.phase) * w_int if w_int else float("inf")
                due_r = start + (nr + self.phase) * r_int if r_int else float("inf")
                due = min(due_w, due_r)
                if due - start >= cfg.duration_s:
                    break
                delay = due - time.monotonic()
                if delay > 0:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=delay)
                        break
                    except TimeoutError:
                        pass
                self.sched_lag_ms.append(max(0.0, (time.monotonic() - due) * 1000))
                saturated = self._inflight_w + self._inflight_r >= cfg.max_inflight
                if due_w <= due_r:
                    self.writes.sent += 1
                    nw += 1
                    if saturated:
                        self._refuse("write")
                        continue
                    t = asyncio.create_task(self._write(client, due_w))
                else:
                    self.reads.sent += 1
                    nr += 1
                    if saturated:
                        self._refuse("read")
                        continue
                    t = asyncio.create_task(self._read(due_r))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            send_window = time.monotonic() - start
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=cfg.drain_timeout_s + cfg.read_timeout_s)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for t in done:
                    if t.exception() is not None:
                        _event("task_error", error=repr(t.exception()))
            self._stop.set()
            lag_task.cancel()
            await asyncio.gather(lag_task, return_exceptions=True)
        # Writes that never ended (cancelled after the drain) are failed requests: nothing silent.
        unaccounted = self.writes.sent - self.writes.ok - self.writes.failed
        if unaccounted > 0:  # not expected: cancelled writes record themselves above
            self.writes.failed += unaccounted
            self.writes.attempt_errors["unaccounted"] += unaccounted
            _event("unaccounted_writes", count=unaccounted)
        unaccounted_r = self.reads.sent - self.reads.ok - self.reads.failed
        if unaccounted_r > 0:
            self.reads.failed += unaccounted_r
            self.reads.attempt_errors["cancelled_after_drain"] += unaccounted_r
        return WorkerResult(self.writes, self.reads, self.records, dict(self.seconds), send_window,
                            _cpu_s() - cpu0, self.max_inflight_w, self.max_inflight_r, self.sched_lag_ms,
                            self.loop_lag_max_ms)


# --- several processes -------------------------------------------------------------------------

def _worker_main(cfg: AppLoadConfig, mcfg: MetricsConfig, index: int, workers: int,
                 start_wall: float) -> WorkerResult:
    instance = mcfg.run_id if workers == 1 else f"{mcfg.run_id}/w{index}"
    metrics = LoadMetrics(mcfg.endpoint, mcfg.run_id, mcfg.plan_id, cfg.base_url, mcfg.interval_s,
                          instance_id=instance)
    load = AppLoad(cfg, metrics, start_wall, phase=index / workers)

    async def go() -> WorkerResult:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, load.stop)
        return await load.run()

    try:
        return asyncio.run(go())
    finally:
        metrics.shutdown()


def run_workers(cfg: AppLoadConfig, mcfg: MetricsConfig, workers: int,
                on_signal: list[Any] | None = None) -> tuple[dict[str, Any], list[RequestRecord], list[dict[str, Any]]]:
    """Run ``workers`` processes and merge them: (summary, request log, per-second timeline)."""
    share = replace(cfg, write_rate=cfg.write_rate / workers, read_rate=cfg.read_rate / workers)
    started_at = time.time()
    start_wall = started_at + 2.0 + 0.2 * workers  # every worker is up and primed before the first request
    if workers == 1:
        results = [_worker_main(share, mcfg, 0, 1, start_wall)]
    else:
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            futures = [pool.submit(_worker_main, share, mcfg, i, workers, start_wall) for i in range(workers)]
            if on_signal is not None:
                on_signal.append(pool)
            results = [f.result() for f in futures]
    writes, reads = OpStats(), OpStats()
    seconds: dict[int, Second] = defaultdict(Second)
    records: list[RequestRecord] = []
    for r in results:
        writes.merge(r.writes)
        reads.merge(r.reads)
        records.extend(r.records)
        for t, s in r.seconds.items():
            seconds[t].merge(s)
    window = max(r.send_window_s for r in results)
    lags = [x for r in results for x in r.sched_lag_ms]
    wall = time.time() - started_at
    cpu = sum(r.cpu_s for r in results)
    summary = {
        "mode": "app",
        "target": cfg.base_url,
        "room": cfg.room,
        "started_at": start_wall,
        "duration_s": cfg.duration_s,
        "send_window_s": round(window, 2),
        "target_per_s": {"write": cfg.write_rate, "read": cfg.read_rate},
        "writes": writes.summary(window),
        "reads": reads.summary(window),
        "client": {
            "workers": workers,
            "cpu_s": round(cpu, 1),
            "cpu_cores_mean": round(cpu / wall, 2) if wall > 0 else None,
            "cpu_s_per_worker_max": round(max(r.cpu_s for r in results), 1),
            "wall_s": round(wall, 1),
            "max_inflight_writes_sum": sum(r.max_inflight_w for r in results),
            "max_inflight_reads_sum": sum(r.max_inflight_r for r in results),
            "schedule_lag_ms_p99": _r(percentile(lags, 99)),
            "schedule_lag_ms_max": _r(max(lags)) if lags else None,
            "event_loop_lag_ms_max": round(max(r.loop_lag_max_ms for r in results), 1),
        },
        "metrics_otlp": bool(mcfg.endpoint),
    }
    timeline = [seconds[t].doc(t) for t in sorted(seconds)]
    return summary, records, timeline
