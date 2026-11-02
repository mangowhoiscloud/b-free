"""SHIFT step judgement from the load generator's metrics (contract 15 §10.4, tools/loadgen/README.md "지표 계약").

When a load run is active, a step is judged from the user load itself: the load generator's
``bravo_loadgen_attempts_total`` and ``bravo_loadgen_attempt_duration_milliseconds_bucket`` series in the platform
Prometheus, writes only, by ``served_by`` (the app's X-Served-By). Counts are the difference of two instant values
(t1 = weights confirmed, t2 = now); a series absent at t1 counts from 0. The load generator pushes every 5 s, so a
window shorter than 15 s is not judged. When no run is active the Executor's own write probe is used (front.py).

Selector: the plan's ``bravo_plan_id`` when a run carries it, else any run (``run_id!=""``). Which one was used is
recorded with each step.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

ACTIVE_MAX_AGE_S = 15.0
MIN_WINDOW_S = 15.0
Key = tuple[str, str, str]  # (op, result, served_by)


class MetricsUnavailable(RuntimeError):
    """Prometheus could not be read; the step falls back to the probe or is not judged."""


@dataclass(frozen=True, slots=True)
class LoadWindow:
    writes: int
    errors: int
    by_env: dict[str, int]
    target_samples: int
    target_errors: int


def window_counts(c1: Mapping[Key, float], c2: Mapping[Key, float], target_env_id: str) -> LoadWindow:
    """Writes between two instant values. ``served_by`` unknown or empty with an error counts against the target
    (conservative, same rule as the probe)."""
    writes = errors = t_samples = t_errors = 0
    by_env: dict[str, int] = {}
    for key, v2 in c2.items():
        op, result, served_by = key
        if op != "write":
            continue
        d = max(0, int(round(v2 - c1.get(key, 0.0))))
        if d == 0:
            continue
        writes += d
        env = served_by or "unknown"
        by_env[env] = by_env.get(env, 0) + d
        failed = result != "ok"
        errors += d if failed else 0
        if env == target_env_id or (env in ("unknown", "none") and failed):
            t_samples += d
            t_errors += d if failed else 0
    return LoadWindow(writes, errors, by_env, t_samples, t_errors)


class LoadgenMetrics:
    def __init__(self, url: str, plan_id: str | None, timeout_s: float = 5.0) -> None:
        self.url = url.rstrip("/")
        self.plan_id = plan_id
        self.selector = 'run_id!=""'
        self._http = httpx.AsyncClient(timeout=timeout_s)

    @classmethod
    def from_env(cls, plan_id: str | None, env: Mapping[str, str] | None = None) -> LoadgenMetrics | None:
        url = ((os.environ if env is None else env).get("BRAVO_FRONT_PROM_URL") or "").strip()
        return cls(url, plan_id) if url else None

    async def close(self) -> None:
        await self._http.aclose()

    async def _query(self, q: str, at: float | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": q}
        if at is not None:
            params["time"] = f"{at:.3f}"
        try:
            r = await self._http.get(f"{self.url}/api/v1/query", params=params)
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MetricsUnavailable(f"prometheus query failed: {type(exc).__name__}") from exc
        if r.status_code != 200 or body.get("status") != "success":
            raise MetricsUnavailable(f"prometheus HTTP {r.status_code}: {str(body.get('error'))[:120]}")
        return list((body.get("data") or {}).get("result") or [])

    async def _age(self, selector: str) -> float | None:
        res = await self._query(f"time() - max(timestamp(bravo_loadgen_attempts_total{{{selector}}}))")
        return float(res[0]["value"][1]) if res else None

    async def choose(self) -> dict[str, Any] | None:
        """Pick the selector of an active run (latest sample at most 15 s old); None when no run is active."""
        candidates = ([f'bravo_plan_id="{self.plan_id}"'] if self.plan_id else []) + ['run_id!=""']
        for sel in candidates:
            age = await self._age(sel)
            if age is not None and age <= ACTIVE_MAX_AGE_S:
                self.selector = sel
                return {"selector": sel, "age_s": round(age, 1)}
        return None

    async def counts(self, at: float) -> dict[Key, float]:
        res = await self._query(f"sum by (op, result, served_by) (bravo_loadgen_attempts_total{{{self.selector}}})", at)
        out: dict[Key, float] = {}
        for r in res:
            m = r.get("metric") or {}
            out[(m.get("op", ""), m.get("result", ""), m.get("served_by", ""))] = float(r["value"][1])
        return out

    async def write_p95(self, at: float, window_s: float, served_by: str) -> float | None:
        w = max(int(math.ceil(window_s)), int(MIN_WINDOW_S))
        res = await self._query(
            "histogram_quantile(0.95, sum by (le) (increase(bravo_loadgen_attempt_duration_milliseconds_bucket"
            f'{{{self.selector}, op="write", result="ok", served_by="{served_by}"}}[{w}s])))', at)
        if not res:
            return None
        v = float(res[0]["value"][1])
        return None if math.isnan(v) else round(v, 1)
