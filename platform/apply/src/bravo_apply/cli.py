"""``bravo-apply`` (``python -m bravo_apply``).

  tools-verify                         check every pinned tool (version + sha256); exit 1 on any mismatch
  prepare --binding F --out DIR        prepare phase without the DB; writes DIR/prepare.json, sbom, trivy, stages.jsonl
  apply   --binding F --out DIR        apply phase without the DB; writes DIR/apply.json, signature.json, stages.jsonl
  worker [--once] [--interval S]       DB mode (BRAVO_APPLY_DATABASE_URL, role bravo_apply)

Binding file (CLI mode): {"change_id", "role", "target_environment", "artifact" (frozen rehearsal artifact or null),
"bound" (optional: recorded digests to check), "approval" ({approval_id, image_digest, diff_digest,
policy_diff_digest}, apply only), "prepared" ({image_digest, image_repository}, apply only; or --prepared FILE),
"provenance" (free text: who decided and why; a CLI run has no API approval record)}.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import config, digests, tools
from .pipeline import Change, apply, prepare
from .runtime import MemoryRecorder, Stop


def _change(b: dict, prepared: dict | None) -> Change:
    artifact = b.get("artifact")
    bound = b.get("bound") or {**digests.bound_values(artifact),
                               "artifact_digest": digests.artifact_digest(artifact) if artifact else None}
    return Change(change_id=b["change_id"], role=b["role"], target_env=b["target_environment"], artifact=artifact,
                  bound=bound, approval=b.get("approval"), prepared=prepared or b.get("prepared"),
                  provenance=b.get("provenance") or "CLI run (no API approval record)")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="bravo-apply")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tools-verify")
    for name in ("prepare", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--binding", required=True)
        p.add_argument("--out", required=True)
        if name == "apply":
            p.add_argument("--prepared", help="prepare.json from the prepare phase")
    w = sub.add_parser("worker")
    w.add_argument("--once", action="store_true")
    w.add_argument("--interval", type=float, default=2.0)
    w.add_argument("--out", default=str(Path.home() / ".cache" / "bravo-apply" / "evidence"))
    args = ap.parse_args(argv)
    cfg = config.load()

    if args.cmd == "tools-verify":
        checks = tools.verify(tools.load_lock(cfg.lock_path))
        json.dump(checks, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if all(c["ok"] for c in checks) else 1

    if args.cmd == "worker":
        from .worker import Worker

        dsn = os.environ.get("BRAVO_APPLY_DATABASE_URL")
        if not dsn:
            print("bravo-apply worker: BRAVO_APPLY_DATABASE_URL is not set (platform DB not connected)", file=sys.stderr)
            return 2
        wk = Worker(cfg, dsn, Path(args.out))
        if args.once:
            print(json.dumps({"processed": wk.run_once()}))
            return 0
        wk.loop(args.interval)
        return 0

    binding = json.loads(Path(args.binding).read_text())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prepared = json.loads(Path(args.prepared).read_text()) if getattr(args, "prepared", None) else None
    ch = _change(binding, prepared)
    rec = MemoryRecorder(out)
    started = datetime.now(UTC).isoformat()
    try:
        res = (prepare if args.cmd == "prepare" else apply)(cfg, ch, rec, out)
        status = "prepared" if args.cmd == "prepare" else "applied"
        reason = None
    except Stop as stop:
        res, reason = {}, stop.reason
        status = "prepare_failed" if args.cmd == "prepare" else stop.status
    summary = {"mode": "cli", "phase": args.cmd, "change_id": ch.change_id, "target": ch.target_env,
               "provenance": ch.provenance, "started_at": started, "ended_at": datetime.now(UTC).isoformat(),
               "status": status, "reason": reason,
               "stages": [{k: r[k] for k in ("seq", "name", "result", "ms")} for r in rec.rows],
               "result": {k: v for k, v in res.items() if k not in ("scan", "build")} if res else None}
    (out / "result.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if reason is None else 1
