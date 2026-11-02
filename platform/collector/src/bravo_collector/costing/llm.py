"""LLM cost items: subscriptions as flat fees, and recorded token usage per agent run.

Subscriptions come from the cost config (name, plan, monthly price only with an official page cited). Usage comes
from the Harbor trial results the rehearsal service writes (``<run>/result.json``, ``agent_result.metadata.worker``:
model, provider, tokens; ``tokens.api_price_equivalent_usd`` is the Claude Agent SDK's ``total_cost_usd``).
Subscription runs are not billed per token: that amount is labelled "API 가격 환산(청구 아님)" and never summed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .aws_inventory import monthly_segments
from .model import Line, iso, now_utc

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
              "cached_input_tokens", "reasoning_tokens")
MAX_RUNS = 500


def _ts(s: Any) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(UTC)


def subscription_lines(subs: list[dict[str, Any]]) -> list[Line]:
    now = now_utc()
    out = []
    for s in subs:
        since = _ts(s.get("since"))
        price = s.get("monthly_usd")
        line = Line(
            line_id=f"llm:subscription:{s.get('id') or s.get('name')}", provider="llm", service=s.get("provider", "llm"),
            resource_type="subscription", resource_id=s.get("id") or s.get("name", ""),
            resource=f"{s.get('name')} ({s.get('plan') or '등급 미기록'})", environment="llm", component="llm",
            component_source="inferred:llm", basis_code="flat",
            unit_price=float(price) if price is not None else None, unit="USD/month", quantity_unit="month",
            price_source={"name": "공식 가격 페이지", "url": s.get("price_url"), "checked_at": s.get("price_checked_at")}
            if price is not None else None,
            segments=monthly_segments(since, now, 1.0) if since else [],
            running={"from": iso(since), "to": None, "state": "subscribed"},
            note=s.get("note"), detail={"used_by": s.get("used_by")})
        if price is None:
            line.status = "not_connected"
            line.reason = s.get("price_reason") or "공식 가격 페이지를 인용한 월 금액이 없음(지어내지 않음)"
        out.append(line)
    return out


def usage_lines(usage_dir: str | None) -> tuple[list[Line], dict[str, Any]]:
    """One line per recorded agent run. Returns (lines, source status)."""
    if not usage_dir:
        return [], {"status": "not_connected", "reason": "BRAVO_LLM_USAGE_DIR가 설정되지 않음(실행 기록 위치 없음)"}
    root = Path(usage_dir)
    if not root.is_dir():
        return [], {"status": "not_connected", "reason": f"실행 기록 디렉터리가 없음: {root.name}"}
    lines: list[Line] = []
    skipped = 0
    for path in sorted(root.glob("*/result.json"))[:MAX_RUNS]:
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped += 1
            continue
        worker = (((r.get("agent_result") or {}).get("metadata") or {}).get("worker") or {})
        tokens = worker.get("tokens") or {}
        if not worker:
            continue  # the run stopped before the agent was called: no model usage recorded
        counts = {k: tokens[k] for k in TOKEN_KEYS if isinstance(tokens.get(k), int)}
        total = sum(counts.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                               "cache_read_input_tokens"))
        eq = tokens.get("api_price_equivalent_usd")
        if eq is None and isinstance(worker.get("total_cost_usd"), (int, float)):
            eq = worker["total_cost_usd"]
        started, finished = _ts(r.get("started_at")), _ts(r.get("finished_at"))
        at = finished or started
        plan_id = (((r.get("config") or {}).get("environment") or {}).get("kwargs") or {}).get("plan_id")
        run_id = path.parent.name
        line = Line(
            line_id=f"llm:run:{run_id}", provider="llm", service=worker.get("model") or "unknown model",
            resource_type="agent_run", resource_id=run_id,
            resource=f"에이전트 실행 {run_id[:14]} ({worker.get('provider')}, {worker.get('model')}, "
                     f"턴 {worker.get('num_turns')})",
            environment="llm", component="llm", component_source="inferred:llm", basis_code="api_equivalent",
            unit_price=None, unit=None, quantity_unit="tokens",
            price_source={"name": "Claude Agent SDK ResultMessage.total_cost_usd (SDK 가격표)"} if eq is not None else None,
            running={"from": iso(started), "to": iso(finished), "state": "recorded", "_at": at},
            in_total=False, fixed_quantity=float(total), fixed_subtotal=float(eq) if eq is not None else None,
            plan_id=plan_id,
            detail={"tokens": counts, "cost_basis": tokens.get("cost_basis"), "provider": worker.get("provider"),
                    "num_turns": worker.get("num_turns")},
            note="구독 경로라 청구되지 않음. 같은 토큰을 API로 썼다면의 금액(합계에 넣지 않음)")
        if eq is None:
            line.status = "not_connected"
            line.reason = "이 실행에는 API 가격 환산 금액이 기록되지 않음(공식 토큰 단가 미연결)"
        lines.append(line)
    return lines, {"status": "ok", "runs": len(lines), "unreadable": skipped}
