"""Chat traffic through the demo app's WebSocket API (apps/demo, `/ws`), for cutover runs.

One client per app URL. Each message has a client idem_key; a retryable nack or a dropped
socket resends the same key (the app and the DB deduplicate). At the end every acked
idem_key must be in the final DB exactly once (NFR-2), checked through --verify-dsn.

Run with the demo app's environment (it has websockets and psycopg):
    uv run --project ../../apps/demo python scripts/demo_traffic.py \
        --url ws://localhost:8090/ws --url ws://localhost:8091/ws --rate 5 --duration 40 \
        --verify-dsn "$DEMO_VERIFY_DSN"
Prints one JSON line. Exit 0 only when nothing failed and NFR-2 holds.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg
import websockets


STOP: dict[str, float] = {}  # SIGTERM/SIGINT: stop sending now, then drain outstanding acks


@dataclass
class Pending:
    idem_key: str
    created: float
    attempts: int = 1


@dataclass
class Stats:
    sent: int = 0
    acked: int = 0
    resent: int = 0
    reconnects: int = 0
    nack_final: int = 0
    latencies: list[float] = field(default_factory=list)
    acked_keys: list[str] = field(default_factory=list)
    served_by: dict[str, int] = field(default_factory=dict)


async def client(url: str, room: str, rate: float, duration: float, drain: float, st: Stats) -> None:
    pending: dict[str, Pending] = {}
    stop_at = time.monotonic() + duration
    drain_until = stop_at + drain

    def stopping() -> bool:
        return time.monotonic() >= min(stop_at, STOP.get("at", float("inf")))
    interval = 1.0 / rate
    author = f"traffic-{url.rsplit(':', 1)[-1].split('/')[0]}"
    sep = "&" if "?" in url else "?"
    while time.monotonic() < drain_until and (not stopping() or pending):
        try:
            async with websockets.connect(f"{url}{sep}room={room}", open_timeout=5, close_timeout=1) as ws:
                for p in pending.values():  # resend everything not yet acked, same keys
                    p.attempts += 1
                    st.resent += 1
                    await ws.send(json.dumps({"type": "send", "idem_key": p.idem_key, "author": author, "body": "demo"}))
                next_send = time.monotonic()

                async def receive() -> None:
                    async for raw in ws:
                        doc: dict[str, Any] = json.loads(raw)
                        if doc.get("type") == "ack":
                            p = pending.pop(str(doc.get("idem_key")), None)
                            if p is not None:
                                st.acked += 1
                                st.latencies.append((time.monotonic() - p.created) * 1000)
                                st.acked_keys.append(p.idem_key)
                        elif doc.get("type") == "hello":
                            env = doc.get("env", {}).get("id", "?")
                            st.served_by[env] = st.served_by.get(env, 0) + 1
                        elif doc.get("type") == "nack":
                            key = str(doc.get("idem_key"))
                            if not doc.get("retryable") and key in pending:
                                pending.pop(key)
                                st.nack_final += 1
                            elif key in pending:
                                pending[key].attempts += 1
                                st.resent += 1
                                await asyncio.sleep(0.2)
                                await ws.send(json.dumps(
                                    {"type": "send", "idem_key": key, "author": author, "body": "demo"}))

                rx = asyncio.create_task(receive())
                try:
                    while time.monotonic() < drain_until and (not stopping() or pending):
                        if rx.done():
                            rx.result()  # surface the disconnect
                            break
                        if not stopping() and time.monotonic() >= next_send:
                            key = str(uuid.uuid4())
                            pending[key] = Pending(key, time.monotonic())
                            st.sent += 1
                            await ws.send(json.dumps({"type": "send", "idem_key": key, "author": author, "body": "demo"}))
                            next_send += interval
                        await asyncio.sleep(0.01)
                finally:
                    rx.cancel()
                    with contextlib.suppress(BaseException):
                        await rx
                if stopping() and not pending:
                    return
        except (OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError):
            st.reconnects += 1
            await asyncio.sleep(0.2)


async def main_async(args: argparse.Namespace) -> int:
    st = Stats()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: STOP.setdefault("at", time.monotonic()))
    await asyncio.gather(*(client(u, args.room, args.rate, args.duration, args.drain, st) for u in args.url))
    failed = st.sent - st.acked - st.nack_final
    out: dict[str, Any] = {
        "sent": st.sent, "acked": st.acked, "resent": st.resent, "reconnects": st.reconnects,
        "nack_final": st.nack_final, "unacked": failed, "hello_by_env": st.served_by,
        "max_latency_ms": round(max(st.latencies), 1) if st.latencies else None,
    }
    ok = failed == 0 and st.nack_final == 0
    if args.verify_dsn:
        async with await psycopg.AsyncConnection.connect(args.verify_dsn, autocommit=True, prepare_threshold=None) as c:
            cur = await c.execute(
                "SELECT idem_key::text, count(*) FROM messages WHERE idem_key = ANY(%s::uuid[]) GROUP BY 1",
                (st.acked_keys,),
            )
            counts = {k: n for k, n in await cur.fetchall()}
        lost = [k for k in st.acked_keys if counts.get(k, 0) == 0]
        dup = [k for k in st.acked_keys if counts.get(k, 0) > 1]
        out["nfr2"] = {"ok": not lost and not dup, "lost": len(lost), "duplicated": len(dup)}
        ok = ok and not lost and not dup
    print(json.dumps(out))
    return 0 if ok else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", action="append", required=True, help="ws://host:port/ws (repeat per app instance)")
    p.add_argument("--room", default="cutover-demo")
    p.add_argument("--rate", type=float, default=5.0, help="messages/s per URL")
    p.add_argument("--duration", type=float, default=40.0)
    p.add_argument("--drain", type=float, default=30.0, help="seconds to wait for outstanding acks")
    p.add_argument("--verify-dsn", default=None)
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
