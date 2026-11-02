"""통제 ID별 비교표 from the gate's /v1/controls/report JSON (one per pair) in a run folder. Stdout: Markdown."""

import json
import sys
from pathlib import Path

LABEL = {"PASS": "충족", "FAIL": "보완 필요", "UNKNOWN": "미검증", "EXCEPTION": "대체 불가"}


def cell(side: str, ev: dict) -> str:
    reason = (ev or {}).get("reason") or ""
    return f"{side} `{reason}`" if reason else side


out = []
for f in sorted(Path(sys.argv[1]).glob("report-*.json")):
    r = json.loads(f.read_text())
    pair = f"{r.get('source')} ↔ {r.get('target')}"
    out.append(f"## {pair}\n")
    out.append(f"게이트 판정 **{r.get('gate_verdict')}**, 막힌 통제 {', '.join(r.get('blocked_controls') or []) or '없음'}. "
               f"`collected_at` {r.get('collected_at')}, `rule_version` {r.get('rule_version')}, "
               f"`input_digest` {str(r.get('input_digest'))[:16]}…\n")
    out.append("| 통제 | 출발지 | 목적지 | 판정 | 이유 |\n|---|---|---|---|---|")
    for c in r.get("controls", []):
        out.append(f"| {c['control_id']} | {c['src']} ({LABEL.get(c['src'], '')}) | {c['dst']} ({LABEL.get(c['dst'], '')}) "
                   f"| {c['verdict']} | {c.get('reason') or ''} |")
    out.append("\n| 질문 | 상위 통제 | 출발지 | 목적지 | 판정 |\n|---|---|---|---|---|")
    for q in r.get("questions", []):
        out.append(f"| {q['control_id']} | {q.get('parent_control')} | {cell(q['src'], q.get('src_evidence'))} "
                   f"| {cell(q['dst'], q.get('dst_evidence'))} | {q['verdict']} |")
    out.append("")
print("\n".join(out))
