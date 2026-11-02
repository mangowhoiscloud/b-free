"""``python -m bravo_gate serve`` | ``python -m bravo_gate run --source ID --target ID [--level ...|--report]``."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .controls import contract_rows
from .environments import load_registry
from .service import compare


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bravo-gate")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    r = sub.add_parser("run")
    r.add_argument("--source", required=True)
    r.add_argument("--target", required=True)
    r.add_argument("--level", choices=("question", "control"), default="question")
    r.add_argument("--report", action="store_true", help="full report with evidence instead of contract rows")
    a = p.parse_args(argv)
    if a.cmd == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(), host=os.environ.get("BRAVO_GATE_HOST", "0.0.0.0"),  # noqa: S104
                    port=int(os.environ.get("BRAVO_GATE_PORT", "8084")), log_level="info")
        return 0
    reg = load_registry()
    s, t = reg.get(a.source), reg.get(a.target)
    if s is None or t is None:
        print(f"unknown environment: {a.source if s is None else a.target}", file=sys.stderr)
        return 2
    rep = compare(s, t)
    print(json.dumps(rep if a.report else contract_rows(rep, a.level), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
