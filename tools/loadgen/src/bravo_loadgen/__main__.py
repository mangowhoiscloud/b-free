"""CLI. Settings come from flags or LOADGEN_* environment variables.

Two modes:
  app (``--app-url``): writes and reads through the customer app's public URL (spec §5.3). Prints one
      JSON summary line; ``--requests-file`` / ``--timeline-file`` keep the request log and the
      per-second timeline; ``--otlp-endpoint`` pushes live metrics (README "지표 계약").
  db  (``--dsn``): writes only, straight into PgBouncer as app_writer (the earlier T18 path).
      Prints {"summary": {...}, "nfr2": {...} | null}.
Exit code: 0 when nothing failed (and, in db mode with --verify, NFR-2 holds), 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .app_load import AppLoadConfig, MetricsConfig, run_workers
from .generator import LoadConfig, LoadGenerator, count_keys
from .stats import evaluate_nfr2


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _f(name: str, default: float) -> float:
    return float(_env(name, str(default)) or default)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bravo-loadgen", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--app-url", default=_env("LOADGEN_APP_URL"),
                   help="app mode: customer app base URL, e.g. https://b-free-chat-src.mangowhois.ai")
    p.add_argument("--dsn", default=_env("LOADGEN_DSN"), help="db mode: write DSN (PgBouncer). env LOADGEN_DSN")
    p.add_argument("--verify-dsn", default=_env("LOADGEN_VERIFY_DSN"),
                   help="db mode: DSN of the final DB for NFR-2 (default: --dsn)")
    p.add_argument("--rate", type=float, default=_f("LOADGEN_RATE", 20), help="db mode: writes/s")
    p.add_argument("--write-rate", type=float, default=_f("LOADGEN_WRITE_RATE", 50), help="app mode: writes/s")
    p.add_argument("--read-rate", type=float, default=_f("LOADGEN_READ_RATE", 150), help="app mode: reads/s")
    p.add_argument("--duration", type=float, default=_f("LOADGEN_DURATION_S", 60), help="seconds")
    p.add_argument("--concurrency", type=int, default=int(_env("LOADGEN_CONCURRENCY", "8") or 8), help="db mode")
    p.add_argument("--attempt-timeout", type=float, default=_f("LOADGEN_ATTEMPT_TIMEOUT_S", 15))
    p.add_argument("--max-attempts", type=int, default=int(_env("LOADGEN_MAX_ATTEMPTS", "40") or 40))
    p.add_argument("--request-deadline", type=float, default=_f("LOADGEN_REQUEST_DEADLINE_S", 120),
                   help="app mode: give a write up after this many seconds from its scheduled time")
    p.add_argument("--drain-timeout", type=float, default=_f("LOADGEN_DRAIN_TIMEOUT_S", 60))
    p.add_argument("--read-timeout", type=float, default=_f("LOADGEN_READ_TIMEOUT_S", 15), help="app mode")
    p.add_argument("--read-window", type=int, default=int(_env("LOADGEN_READ_WINDOW", "20") or 20),
                   help="app mode: a read asks for rows after (newest acked id - window)")
    p.add_argument("--run-id", default=_env("LOADGEN_RUN_ID"), help="default: a new UUID")
    p.add_argument("--plan-id", default=_env("LOADGEN_PLAN_ID"), help="app mode: OTLP resource bravo.plan_id")
    p.add_argument("--room", default=_env("LOADGEN_ROOM"),
                   help="default: db mode 'load', app mode 'load-<first 8 of run id>' (one room per run)")
    p.add_argument("--author", default=_env("LOADGEN_AUTHOR", "loadgen"))
    p.add_argument("--otlp-endpoint", default=_env("LOADGEN_OTLP_ENDPOINT"),
                   help="app mode: OTLP/HTTP base URL of the platform Collector (e.g. http://127.0.0.1:14318)")
    p.add_argument("--max-inflight", type=int, default=int(_env("LOADGEN_MAX_INFLIGHT", "200") or 200),
                   help="app mode: open requests per worker; above it a due request fails as client_saturated")
    p.add_argument("--workers", type=int, default=int(_env("LOADGEN_WORKERS", "1") or 1),
                   help="app mode: processes sharing the schedule (one process tops out near one core)")
    p.add_argument("--metrics-interval", type=float, default=_f("LOADGEN_METRICS_INTERVAL_S", 5))
    p.add_argument("--requests-file", default=_env("LOADGEN_REQUESTS_FILE"), help="app mode: request log JSONL")
    p.add_argument("--timeline-file", default=_env("LOADGEN_TIMELINE_FILE"), help="app mode: per-second JSONL")
    p.add_argument("--acked-file", default=_env("LOADGEN_ACKED_FILE"), help="db mode: acked keys JSONL")
    p.add_argument("--verify", action="store_true", default=_env("LOADGEN_VERIFY", "0") == "1",
                   help="db mode: check NFR-2 against the final DB after the run")
    args = p.parse_args(argv)
    if bool(args.app_url) == bool(args.dsn):
        p.error("exactly one of --app-url (app mode) or --dsn (db mode) is required")
    if args.write_rate < 0 or args.read_rate < 0 or args.rate <= 0 or args.write_rate + args.read_rate <= 0:
        p.error("rates must be positive")
    if args.concurrency < 1 or args.max_attempts < 1 or args.workers < 1:
        p.error("concurrency and max-attempts must be positive")
    args.run_id = args.run_id or str(uuid.uuid4())
    if not args.room:
        args.room = f"load-{args.run_id[:8]}" if args.app_url else "load"
    return args


def _write_acked(path: Path, acked: list[tuple[uuid.UUID, int]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for key, msg_id in acked:
            fh.write(json.dumps({"idem_key": str(key), "id": msg_id}) + "\n")


def _write_jsonl(path: Path, docs: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for d in docs:
            fh.write(json.dumps(d) + "\n")


def _main_app(args: argparse.Namespace) -> int:
    cfg = AppLoadConfig(
        base_url=args.app_url,
        write_rate=args.write_rate,
        read_rate=args.read_rate,
        duration_s=args.duration,
        room=args.room,
        author=args.author,
        attempt_timeout_s=args.attempt_timeout,
        max_attempts=args.max_attempts,
        request_deadline_s=args.request_deadline,
        drain_timeout_s=args.drain_timeout,
        read_timeout_s=args.read_timeout,
        read_window=args.read_window,
        max_inflight=args.max_inflight,
    )
    mcfg = MetricsConfig(args.otlp_endpoint, args.run_id, args.plan_id, args.metrics_interval)
    pools: list[Any] = []

    def forward(signum: int, _frame: object) -> None:  # stop sending in every worker, then drain
        for pool in pools:
            for proc in list(getattr(pool, "_processes", {}).values()):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(proc.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    summary, records, timeline = run_workers(cfg, mcfg, args.workers, on_signal=pools)
    summary["run_id"] = args.run_id
    summary["plan_id"] = args.plan_id
    if args.requests_file:
        _write_jsonl(Path(args.requests_file), [asdict(r) for r in records])
    if args.timeline_file:
        _write_jsonl(Path(args.timeline_file), timeline)
    print(json.dumps(summary))
    sys.stdout.flush()
    return 0 if summary["writes"]["failed"] == 0 and summary["reads"]["failed"] == 0 else 1


async def _main_db(args: argparse.Namespace) -> int:
    gen = LoadGenerator(
        LoadConfig(
            dsn=args.dsn,
            rate=args.rate,
            duration_s=args.duration,
            concurrency=args.concurrency,
            attempt_timeout_s=args.attempt_timeout,
            max_attempts=args.max_attempts,
            drain_timeout_s=args.drain_timeout,
            room=args.room,
            author=args.author,
        )
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, gen.stop)
    result = await gen.run()
    if args.acked_file:
        await asyncio.to_thread(_write_acked, Path(args.acked_file), result.acked)
    nfr2 = None
    if args.verify:
        keys = [k for k, _ in result.acked]
        counts = await count_keys(args.verify_dsn or args.dsn, keys)
        nfr2 = evaluate_nfr2(keys, counts, result.stats.failed, result.stats.sent)
    print(json.dumps({"summary": result.stats.summary(), "nfr2": None if nfr2 is None else nfr2.as_dict()}))
    sys.stdout.flush()
    if nfr2 is not None:
        return 0 if nfr2.ok else 1
    return 0 if result.stats.failed == 0 else 1


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    sys.exit(_main_app(args) if args.app_url else asyncio.run(_main_db(args)))


if __name__ == "__main__":
    main()
