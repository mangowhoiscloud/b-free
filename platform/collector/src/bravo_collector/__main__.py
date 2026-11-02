"""``python -m bravo_collector serve | snapshot --environment ID [--sources ...] | inventory ...``."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .inventory import main_inventory


def _serve() -> int:
    import uvicorn

    from .app import create_app
    from .redact import install_log_filter

    install_log_filter()
    host = os.environ.get("BRAVO_COLLECTOR_HOST", "0.0.0.0")  # noqa: S104 - container listen address
    port = int(os.environ.get("BRAVO_COLLECTOR_PORT", "8083"))
    config = uvicorn.Config(create_app(), host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())
    server = uvicorn.Server(config)
    install_log_filter()  # again, after uvicorn configured its handlers
    server.run()
    return 0


def _snapshot(argv: list[str]) -> int:
    from .environments import load_registry
    from .service import SOURCES, collect

    p = argparse.ArgumentParser(prog="bravo-collector snapshot", description="FR-1 read-only snapshot")
    p.add_argument("--environment", required=True, help="environment id in $BRAVO_ENVIRONMENTS_FILE")
    p.add_argument("--sources", default="", help=f"comma-separated subset of {','.join(SOURCES)}")
    p.add_argument("--environments-file", default=None, help="default: $BRAVO_ENVIRONMENTS_FILE")
    args = p.parse_args(argv)
    env = load_registry(args.environments_file).get(args.environment)
    if env is None:
        print(f"snapshot: unknown environment {args.environment!r}", file=sys.stderr)
        return 2
    wanted = {s.strip() for s in args.sources.split(",") if s.strip()} or None
    snap = collect(env, wanted)
    json.dump(snap.model_dump(mode="json"), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _cost(argv: list[str]) -> int:
    from datetime import date

    from .cost import cost_report, default_period

    p = argparse.ArgumentParser(prog="bravo-collector cost",
                                description="T1: cost by component tag and by service (Cost Explorer, read-only; "
                                            "each Cost Explorer request is billed USD 0.01)")
    p.add_argument("--start", help="YYYY-MM-DD (default: first day of this month, UTC)")
    p.add_argument("--end", help="YYYY-MM-DD, exclusive (default: tomorrow, UTC)")
    p.add_argument("--tag", default="component")
    args = p.parse_args(argv)
    start, end = default_period()
    if args.start:
        start = date.fromisoformat(args.start)
    if args.end:
        end = date.fromisoformat(args.end)
    json.dump(cost_report(start, end, args.tag), sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "serve"
    rest = args[1:]
    if cmd == "serve":
        return _serve()
    if cmd == "snapshot":
        return _snapshot(rest)
    if cmd == "inventory":
        return main_inventory(rest)
    if cmd == "cost":
        return _cost(rest)
    if cmd == "scram-verifier":
        from .scram import scram_sha256_verifier

        password = sys.stdin.readline().rstrip("\n")  # stdin only: never argv
        sys.stdout.write(scram_sha256_verifier(password) + "\n")
        return 0
    print("usage: bravo-collector {serve|snapshot|inventory|cost|scram-verifier} ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
