"""Hubble drop counters of a Trial's namespaces, read from the platform Prometheus (P13).

Cilium agents keep a ring buffer of 4,095 flows; on the on-prem nodes that is about 33 s of traffic (measured
2026-11-03 02:3x KST: ``hubble status`` 4,095/4,095 at 62-124 flows/s). ``hubble observe --last 50`` after a 3-minute
Trial therefore reads 0 DROPPED even though the drops happened. The agents also export ``hubble_drop_total`` (labels
``source``/``destination`` = namespace, ``reason``, ``protocol``), which reaches the platform Prometheus through the
observability link; a counter keeps the count after the flows are gone. This module reads it for the Trial's own
namespaces: the value at the end minus the value just before the Trial started (namespaces are new per plan, role and
attempt, so the start value is normally absent = 0).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

SCRAPE_WAIT_S = 75.0  # scrape interval + remote write delay before the last drops are visible
_NS = re.compile(r"^[a-z0-9-]{1,63}$")


def query(env: str, namespaces: list[str]) -> str:
    nss = [n for n in namespaces if _NS.match(n)]
    sel = f'bravo_env="{env}",source=~"{"|".join(nss)}"' if env else f'source=~"{"|".join(nss)}"'
    return f"sum by (source, reason, protocol) (hubble_drop_total{{{sel}}})"


def _vector(body: Any) -> dict[tuple[str, str, str], float]:
    out: dict[tuple[str, str, str], float] = {}
    for r in ((body or {}).get("data") or {}).get("result") or []:
        m = r.get("metric") or {}
        try:
            out[(str(m.get("source", "")), str(m.get("reason", "")), str(m.get("protocol", "")))] = float(r["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


async def collect(prometheus_url: str, env: str, namespaces: list[str], start: float, end: float) -> dict[str, Any]:
    q = query(env, namespaces)
    rec: dict[str, Any] = {"source": "prometheus hubble_drop_total", "query": q, "env": env,
                           "window": [datetime.fromtimestamp(start, UTC).isoformat(),
                                      datetime.fromtimestamp(end, UTC).isoformat()]}
    try:
        async with httpx.AsyncClient(base_url=prometheus_url, timeout=10.0) as http:
            before = await http.get("/api/v1/query", params={"query": q, "time": f"{start - 1:.3f}"})
            after = await http.get("/api/v1/query", params={"query": q, "time": f"{end:.3f}"})
        before.raise_for_status()
        after.raise_for_status()
    except httpx.HTTPError as exc:
        rec["error"] = f"prometheus_unreachable:{type(exc).__name__}"
        return rec
    b, a = _vector(before.json()), _vector(after.json())
    rows = [{"namespace": k[0], "reason": k[1], "protocol": k[2], "drops": int(v - b.get(k, 0.0)),
             "counter_end": int(v)} for k, v in sorted(a.items()) if v - b.get(k, 0.0) > 0]
    rec["rows"] = rows
    rec["total"] = sum(r["drops"] for r in rows)
    rec["policy_denied"] = sum(r["drops"] for r in rows if r["reason"] == "POLICY_DENIED")
    return rec
