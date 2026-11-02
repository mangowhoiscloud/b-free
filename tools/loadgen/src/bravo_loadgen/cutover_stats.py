"""NFR-1 / NFR-2 statistics over cutover-under-load runs (deploy/cloud/load_cutover.sh run.json files).

    python -m bravo_loadgen.cutover_stats docs/evidence/load/*-cutover-*/run.json

NFR-1 publishes the raw write pauses, the median and the maximum. Only runs whose cutover ended DONE have
a write pause; other runs are listed with their status and are not dropped silently. Prints one JSON line.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any


def summarize(runs: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    rows = []
    for path, r in runs:
        cut = r.get("cutover") or {}
        nfr2 = r.get("nfr2") or {}
        rows.append({
            "run": path,
            "plan_id": r.get("plan_id"),
            "cutover_status": cut.get("status", "NOT_STARTED"),
            "write_pause_ms": r.get("write_pause_ms"),
            "writes_per_s": ((r.get("load") or {}).get("writes") or {}).get("achieved_ok_per_s"),
            "nfr2_ok": nfr2.get("ok"),
            "lost": nfr2.get("lost"),
            "duplicated": nfr2.get("duplicated"),
            "failed_requests": nfr2.get("failed_requests"),
        })
    pauses = [x["write_pause_ms"] for x in rows if x["cutover_status"] == "DONE" and x["write_pause_ms"] is not None]
    return {
        "runs": rows,
        "done_runs": len(pauses),
        "write_pause_ms": {
            "raw": pauses,
            "median": statistics.median(pauses) if pauses else None,
            "max": max(pauses) if pauses else None,
            "within_5000": sum(1 for p in pauses if p <= 5000),
        },
        "nfr2": {
            "runs_ok": sum(1 for x in rows if x["nfr2_ok"] is True),
            "lost": sum(x["lost"] or 0 for x in rows),
            "duplicated": sum(x["duplicated"] or 0 for x in rows),
            "failed_requests": sum(x["failed_requests"] or 0 for x in rows),
        },
    }


def main(argv: list[str] | None = None) -> None:
    paths = argv if argv is not None else sys.argv[1:]
    if not paths:
        sys.exit("usage: python -m bravo_loadgen.cutover_stats <run.json>...")
    runs = [(p, json.loads(Path(p).read_text(encoding="utf-8"))) for p in paths]
    print(json.dumps(summarize(runs)))


if __name__ == "__main__":
    main()
