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
    r.add_argument("--write-probes", action="store_true",
                   help="allow OBJ-03/OBJ-04 PUT probes on stores with write_probes: true (probe keys are deleted)")
    kb = sub.add_parser("kube-bench", help="run kube-bench as a Job (pinned image, explicit benchmark)")
    kb.add_argument("--context", required=True, help="kubectl context")
    kb.add_argument("--platform", required=True, choices=("eks", "k3s", "rke2", "gke"))
    kb.add_argument("--out", required=True, help="file for the raw kube-bench JSON + job facts")
    kb.add_argument("--keep", action="store_true", help="keep the scan namespace, Job and RBAC")
    kc = sub.add_parser("kube-bench-compare", help="normalise two kube-bench outputs to control IDs (근사)")
    kc.add_argument("--src", required=True)
    kc.add_argument("--dst", required=True)
    a = p.parse_args(argv)
    if a.cmd == "kube-bench":
        from kubernetes import client, config

        from .kube_bench import normalize
        from .kube_bench.runner import run_job

        cfg = client.Configuration()
        config.load_kube_config(context=a.context, client_configuration=cfg)
        res = run_job(client.ApiClient(cfg), a.platform, keep=a.keep)
        res.update({"context": a.context, "platform": a.platform})
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        norm = normalize(res["report"], a.platform)
        print(json.dumps({"job": res["job"], "totals": res["report"].get("Totals"),
                          "topics": {k: {"control_id": v["control_id"], "verdict": v["verdict"],
                                         "status": v["status"], "items": v["items"]}
                                     for k, v in norm["topics"].items()},
                          "label": norm["label"], "benchmark": norm["benchmark"]}, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "kube-bench-compare":
        from .kube_bench import compare as kb_compare
        from .kube_bench import normalize

        def load(path: str) -> dict:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            return normalize(d["report"], d["platform"])

        print(json.dumps(kb_compare(load(a.src), load(a.dst)), ensure_ascii=False, indent=2))
        return 0
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
    rep = compare(s, t, allow_writes=a.write_probes)
    print(json.dumps(rep if a.report else contract_rows(rep, a.level), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
