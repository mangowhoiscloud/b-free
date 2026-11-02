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

    host = os.environ.get("BRAVO_COLLECTOR_HOST", "0.0.0.0")  # noqa: S104 - container listen address
    port = int(os.environ.get("BRAVO_COLLECTOR_PORT", "8083"))
    uvicorn.run(create_app(), host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())
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
    print("usage: bravo-collector {serve|snapshot|inventory} ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
