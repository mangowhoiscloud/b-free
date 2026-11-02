"""CLI. Settings come from flags or LOADGEN_* environment variables.

On exit prints one JSON summary line to stdout:
{"summary": {sent, acked, retried, failed, max_latency_ms, ...}, "nfr2": {...} | null}
Exit code: 0 when NFR-2 holds (or was not requested and nothing failed), 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import uuid
from pathlib import Path

from .generator import LoadConfig, LoadGenerator, count_keys
from .stats import evaluate_nfr2


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bravo-loadgen", description=__doc__)
    p.add_argument("--dsn", default=_env("LOADGEN_DSN"), help="write DSN (PgBouncer). env LOADGEN_DSN")
    p.add_argument("--verify-dsn", default=_env("LOADGEN_VERIFY_DSN"),
                   help="DSN of the final DB for NFR-2 (default: --dsn, i.e. the pool's current target)")
    p.add_argument("--rate", type=float, default=float(_env("LOADGEN_RATE", "20") or 20), help="requests/s")
    p.add_argument("--duration", type=float, default=float(_env("LOADGEN_DURATION_S", "60") or 60), help="seconds")
    p.add_argument("--concurrency", type=int, default=int(_env("LOADGEN_CONCURRENCY", "8") or 8))
    p.add_argument("--attempt-timeout", type=float, default=float(_env("LOADGEN_ATTEMPT_TIMEOUT_S", "10") or 10))
    p.add_argument("--max-attempts", type=int, default=int(_env("LOADGEN_MAX_ATTEMPTS", "30") or 30))
    p.add_argument("--drain-timeout", type=float, default=float(_env("LOADGEN_DRAIN_TIMEOUT_S", "60") or 60))
    p.add_argument("--room", default=_env("LOADGEN_ROOM", "load"))
    p.add_argument("--author", default=_env("LOADGEN_AUTHOR", "loadgen"))
    p.add_argument("--acked-file", default=_env("LOADGEN_ACKED_FILE"), help="write acked keys as JSONL")
    p.add_argument("--verify", action="store_true", default=_env("LOADGEN_VERIFY", "0") == "1",
                   help="check NFR-2 against the final DB after the run")
    args = p.parse_args(argv)
    if not args.dsn:
        p.error("--dsn or LOADGEN_DSN is required")
    if args.rate <= 0 or args.concurrency < 1 or args.max_attempts < 1:
        p.error("rate, concurrency and max-attempts must be positive")
    return args


def _write_acked(path: Path, acked: list[tuple[uuid.UUID, int]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for key, msg_id in acked:
            fh.write(json.dumps({"idem_key": str(key), "id": msg_id}) + "\n")


async def _main(args: argparse.Namespace) -> int:
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
    sys.exit(asyncio.run(_main(parse_args(argv))))


if __name__ == "__main__":
    main()
