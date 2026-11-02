"""kube-bench runs -> deploy/platform/control/config/gate-cis-evidence.json (the gate's CIS evidence, NOT judged).

    python3 -I docs/evidence/kube-bench/cis-summary.py > deploy/platform/control/config/gate-cis-evidence.json

Per environment: the newest run of each node in RUNS, totals, every FAIL item with the recorded judgement
(fixed / exception / not_applicable from <dir>/judgements.json; "unjudged" when there is none), collected_at
(file name time, UTC) and expires_at (+7 days). The gate attaches it to every report as ``cis_benchmark`` with
``judged: false``: kube-bench benchmarks lag the distributions (approximate), WARN items need a person.
"""

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
VALID = timedelta(days=7)
# environment id -> (folder, run file prefixes, one per node)
RUNS = {
    "target-onprem": ("onprem-20261103", ["onprem-server-", "onprem-agent-"]),
    "source-aws": ("source-20261103", ["source-eks-"]),
    "target-gcp": ("gke-20261103", ["gke-"]),
}


def stamp(name: str) -> datetime:
    m = re.search(r"(\d{8}T\d{6}Z)", name)
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def fails(report: dict) -> list[dict]:
    out = []
    for c in report.get("Controls") or []:
        for t in c.get("tests") or []:
            for r in t.get("results") or []:
                if r.get("status") == "FAIL":
                    out.append({"id": str(r.get("test_number")), "desc": r.get("test_desc")})
    return out


envs = {}
for env_id, (folder, prefixes) in RUNS.items():
    d = HERE / folder
    judged = {}
    jf = d / "judgements.json"
    if jf.is_file():
        judged = json.loads(jf.read_text(encoding="utf-8")).get("items", {})
    runs, items, totals = [], {}, {"pass": 0, "fail": 0, "warn": 0, "info": 0}
    for prefix in prefixes:
        files = sorted(p for p in d.glob(f"{prefix}*.json") if p.name != "judgements.json")
        if not files:
            continue
        f = files[-1]  # newest run of this node
        doc = json.loads(f.read_text(encoding="utf-8"))
        t = doc["report"].get("Totals") or {}
        for k in totals:
            totals[k] += int(t.get(f"total_{k}", 0))
        runs.append({"file": f"docs/evidence/kube-bench/{folder}/{f.name}", "node": doc["job"].get("node"),
                     "targets": doc["job"]["command"][3], "collected_at": stamp(f.name).isoformat()})
        for it in fails(doc["report"]):
            j = judged.get(it["id"], {})
            items.setdefault(it["id"], {**it, "judgement": j.get("judgement", "unjudged"),
                                        "reason": j.get("reason"), "compensating": j.get("compensating"),
                                        "nodes": []})["nodes"].append(doc["job"].get("node"))
    if not runs:
        continue
    collected = min(datetime.fromisoformat(r["collected_at"]) for r in runs)
    envs[env_id] = {
        "benchmark": json.loads((d / Path(runs[0]["file"]).name).read_text(encoding="utf-8"))["job"]["command"][5],
        "kube_bench": "v0.16.0", "collected_at": collected.isoformat(),
        "expires_at": (collected + VALID).isoformat(), "totals": totals, "runs": runs,
        "fails": sorted(items.values(), key=lambda x: [int(p) for p in x["id"].split(".")]),
    }
json.dump({"label": "근사(approximate)", "judged": False, "generated_by": "docs/evidence/kube-bench/cis-summary.py",
           "environments": envs}, sys.stdout, ensure_ascii=False, indent=1)
print()
