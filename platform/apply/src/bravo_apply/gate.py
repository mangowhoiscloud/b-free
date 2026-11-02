"""Post-apply gate re-check: read the platform/gate report for the destination's (source, target) pair.

The worker only records what the gate service answered. The official GATE verdict and every state transition
belong to platform/api, which re-runs its own gate after the change is applied (migration 0004).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .runtime import Result, Step


def recheck(gate_url: str | None, pair: dict[str, str] | None, timeout: float = 120) -> Step:
    if not gate_url:
        return Step(Result.NOT_CONNECTED, {"reason": "gate_unconfigured", "why": "BRAVO_APPLY_GATE_URL is not set"},
                    {"status": "not_connected", "reason": "gate_unconfigured"})
    if not pair:
        return Step(Result.NOT_CONNECTED, {"reason": "gate_pair_unconfigured"}, {"status": "not_connected",
                                                                                  "reason": "gate_pair_unconfigured"})
    q = urllib.parse.urlencode({"source": pair["source"], "target": pair["target"]})
    url = f"{gate_url}/v1/controls/report?{q}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - operator-configured platform URL
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return Step(Result.NOT_CONNECTED, {"reason": f"gate_http_{exc.code}", "url": url},
                    {"status": "not_connected", "reason": f"gate_http_{exc.code}"})
    except (OSError, ValueError) as exc:
        return Step(Result.NOT_CONNECTED, {"reason": "gate_unreachable", "url": url, "error": str(exc)[-200:]},
                    {"status": "not_connected", "reason": "gate_unreachable"})
    summary = {
        "status": "read",
        "gate_verdict": body.get("gate_verdict"),
        "blocked_controls": body.get("blocked_controls"),
        "controls": [{k: c.get(k) for k in ("control_id", "src", "dst", "verdict", "reason")} for c in body.get("controls") or []],
        "input_digest": body.get("input_digest"),
        "rule_version": body.get("rule_version"),
        "collected_at": body.get("collected_at"),
        "expires_at": body.get("expires_at"),
        "source": pair["source"],
        "target": pair["target"],
    }
    return Step(Result.OK, {k: summary[k] for k in ("gate_verdict", "blocked_controls", "input_digest", "collected_at")}, summary)
