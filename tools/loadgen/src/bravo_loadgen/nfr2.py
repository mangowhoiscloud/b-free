"""NFR-2 for an app-mode run: compare the run's request log with the rows of its room in one DB.

    python -m bravo_loadgen.nfr2 --requests requests.jsonl --rows rows.json

``requests.jsonl`` is the loadgen's request log (``--requests-file``): one line per logical write with
``request_id`` and ``outcome`` (acked | failed). ``rows.json`` is ``{"rows": {<body>: <count>}}`` for the
run's room, read from the final DB (deploy/cloud/load_nfr2.sh reads it inside executor-0 as
``executor_observe``). The room is unique per run, so every row in it belongs to this run.

Prints one JSON line. Exit 0 only when NFR-2 holds: every acked request is in the DB exactly once, no
request failed, and no row exists that the run did not send.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from .generator import body_marker
from .stats import evaluate_nfr2


def check(requests: list[dict[str, Any]], rows: dict[str, int]) -> dict[str, Any]:
    acked = [uuid.UUID(r["request_id"]) for r in requests if r["outcome"] == "acked"]
    failed_ids = {uuid.UUID(r["request_id"]) for r in requests if r["outcome"] != "acked"}
    per_key = {k: rows.get(body_marker(k), 0) for k in acked}
    result = evaluate_nfr2(acked, per_key, failed=len(failed_ids), sent=len(requests))
    known = {body_marker(k) for k in acked} | {body_marker(k) for k in failed_ids}
    committed_after_failure = sum(1 for k in failed_ids if rows.get(body_marker(k), 0) > 0)
    foreign = sorted(b for b in rows if b not in known)
    out = result.as_dict()
    out["rows_in_room"] = sum(rows.values())
    out["failed_but_committed"] = committed_after_failure
    out["rows_not_sent_by_run"] = len(foreign)
    out["rows_not_sent_sample"] = foreign[:5]
    out["ok"] = bool(result.ok and not foreign)
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="bravo-loadgen-nfr2", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--requests", required=True, type=Path)
    p.add_argument("--rows", required=True, type=Path)
    args = p.parse_args(argv)
    requests = [json.loads(line) for line in args.requests.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = json.loads(args.rows.read_text(encoding="utf-8"))["rows"]
    out = check(requests, {str(k): int(v) for k, v in rows.items()})
    print(json.dumps(out))
    sys.exit(0 if out["ok"] else 1)


if __name__ == "__main__":
    main()
