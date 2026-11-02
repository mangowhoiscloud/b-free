"""Normalise kube-bench JSON (``kube-bench run --benchmark <explicit> --json``) to control IDs.

Results are labelled 근사(approximate). A topic that has items on one platform only is 미검증 on the other side.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

import yaml

from ..controls import STATUS_KO, aggregate
from ..result import UNKNOWN


def load_mapping() -> dict[str, Any]:
    return yaml.safe_load(resources.files(__package__).joinpath("mapping.yaml").read_text(encoding="utf-8"))


def _results(report: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for control in report.get("Controls") or []:
        for test in control.get("tests") or []:
            for r in test.get("results") or []:
                out[str(r.get("test_number"))] = str(r.get("status", "")).upper()
    return out


def normalize(report: dict[str, Any], platform: str, mapping: dict[str, Any] | None = None) -> dict[str, Any]:
    m = mapping or load_mapping()
    if platform not in m["benchmarks"]:
        raise ValueError(f"unknown platform {platform!r}; expected one of {sorted(m['benchmarks'])}")
    found = _results(report)
    smap = m["status_map"]
    topics: dict[str, Any] = {}
    for name, t in sorted(m["topics"].items()):
        ids = list(t.get(platform) or [])
        if not ids:
            topics[name] = {"control_id": t["control_id"], "verdict": UNKNOWN, "status": STATUS_KO[UNKNOWN],
                            "reason": "no_items_on_this_platform", "items": []}
            continue
        items = [{"id": i, "kube_bench": found.get(i), "verdict": smap.get(found.get(i) or "", UNKNOWN)}
                 for i in ids]
        missing = [i["id"] for i in items if i["kube_bench"] is None]
        verdict = aggregate([i["verdict"] for i in items])
        topics[name] = {"control_id": t["control_id"], "verdict": verdict, "status": STATUS_KO[verdict],
                        "reason": "items_missing_from_report" if missing else None, "items": items}
    return {"platform": platform, "benchmark": m["benchmarks"][platform], "label": m["label"],
            "mapping_version": m["version"], "kube_bench": m["kube_bench"], "topics": topics}


def compare(src: dict[str, Any], dst: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for name in sorted(set(src["topics"]) | set(dst["topics"])):
        s, d = src["topics"].get(name), dst["topics"].get(name)
        sv = s["verdict"] if s else UNKNOWN
        dv = d["verdict"] if d else UNKNOWN
        one_sided = bool(s and d and (not s["items"]) != (not d["items"]))
        rows.append({"topic": name, "control_id": (s or d)["control_id"], "label": src["label"],
                     "src": sv, "dst": dv, "src_status": STATUS_KO[sv], "dst_status": STATUS_KO[dv],
                     "note": "한쪽에만 있는 항목: 미검증" if one_sided else None,
                     "src_benchmark": src["benchmark"], "dst_benchmark": dst["benchmark"]})
    return rows
