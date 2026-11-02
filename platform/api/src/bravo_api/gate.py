"""Source-parity gate (04 2, CONTRACT.md 6-7). Deterministic rules, no LLM (NFR-5).

Per control (source, target):
    PASS/PASS -> pass
    PASS/FAIL -> BLOCK ``regression``
    FAIL/FAIL -> BLOCK ``fail_both`` (failure, not equivalence)
    any UNKNOWN -> BLOCK ``unknown``
    anything else (FAIL/PASS, unexpected values) -> BLOCK ``not_equivalent`` (fail closed;
    the 04 table does not list it, so it is not treated as a pass)
Readiness: ``lag_bytes`` at most the threshold, ``schema_hash.src == schema_hash.dst``.
Executor unreachable or malformed -> every input UNKNOWN -> BLOCK ``executor_unreachable``.
Same input -> same verdict: inputs are canonicalised and hashed (``input_digest``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

RULE_VERSION = "gate-rules/2026-11-02.1"
_VALUES = frozenset({"PASS", "FAIL", "UNKNOWN"})


class Verdict(StrEnum):
    PASS = "PASS"
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True)
class GatePolicy:
    required_controls: tuple[str, ...]
    max_lag_bytes: int


@dataclass(frozen=True, slots=True)
class ControlResult:
    control_id: str
    src: str
    dst: str
    verdict: str  # PASS | BLOCK
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "src": self.src,
            "dst": self.dst,
            "verdict": self.verdict,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class GateResult:
    verdict: Verdict
    reasons: tuple[str, ...]
    controls: tuple[ControlResult, ...]
    readiness: dict[str, Any]
    input_digest: str
    rule_version: str = RULE_VERSION
    source_errors: tuple[str, ...] = field(default=())


def _norm(value: Any) -> str:
    return value if isinstance(value, str) and value in _VALUES else "UNKNOWN"


def judge_control(src: str, dst: str) -> tuple[str, str | None]:
    if src == "UNKNOWN" or dst == "UNKNOWN":
        return "BLOCK", "unknown"
    if src == "PASS" and dst == "PASS":
        return "PASS", None
    if src == "PASS" and dst == "FAIL":
        return "BLOCK", "regression"
    if src == "FAIL" and dst == "FAIL":
        return "BLOCK", "fail_both"
    return "BLOCK", "not_equivalent"


def canonical_inputs(
    controls: list[dict[str, Any]] | None, readiness: dict[str, Any] | None, policy: GatePolicy
) -> dict[str, Any]:
    by_id: dict[str, dict[str, str]] = {}
    for item in controls or []:
        if not isinstance(item, dict) or not isinstance(item.get("control_id"), str):
            continue
        cid = item["control_id"]
        entry = {"src": _norm(item.get("src")), "dst": _norm(item.get("dst"))}
        # A duplicated control id is contradictory evidence: fail closed.
        by_id[cid] = {"src": "UNKNOWN", "dst": "UNKNOWN"} if cid in by_id else entry
    for cid in policy.required_controls:
        by_id.setdefault(cid, {"src": "UNKNOWN", "dst": "UNKNOWN", "missing": "1"})
    r = readiness if isinstance(readiness, dict) else {}
    raw_schema = r.get("schema_hash")
    schema: dict[str, Any] = raw_schema if isinstance(raw_schema, dict) else {}
    lag = r.get("lag_bytes")
    return {
        "controls": {cid: by_id[cid] for cid in sorted(by_id)},
        "readiness": {
            "lag_bytes": lag if isinstance(lag, int) and not isinstance(lag, bool) else None,
            "schema_hash": {"src": schema.get("src"), "dst": schema.get("dst")},
            "sub_state": r.get("sub_state") if isinstance(r.get("sub_state"), str) else None,
        },
        "policy": {"required_controls": sorted(policy.required_controls), "max_lag_bytes": policy.max_lag_bytes},
        "rule_version": RULE_VERSION,
    }


def evaluate(
    controls: list[dict[str, Any]] | None,
    readiness: dict[str, Any] | None,
    policy: GatePolicy,
    source_errors: tuple[str, ...] = (),
) -> GateResult:
    """Pure function: the verdict depends only on the arguments."""
    canon = canonical_inputs(controls, readiness, policy)
    canon["source_errors"] = sorted(source_errors)
    digest = hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    reasons: set[str] = set(source_errors)
    results: list[ControlResult] = []
    for cid, c in canon["controls"].items():
        verdict, reason = judge_control(c["src"], c["dst"])
        if c.get("missing") and not source_errors:
            reason = "control_missing"  # Executor answered but did not report this control
        results.append(ControlResult(cid, c["src"], c["dst"], verdict, reason))
        if reason:
            reasons.add(f"{cid}:{reason}")

    rd = canon["readiness"]
    if rd["lag_bytes"] is None:
        reasons.add("readiness:lag_unknown")
    elif rd["lag_bytes"] > policy.max_lag_bytes:
        reasons.add("readiness:replication_lag")
    src_hash, dst_hash = rd["schema_hash"]["src"], rd["schema_hash"]["dst"]
    if not isinstance(src_hash, str) or not isinstance(dst_hash, str) or not src_hash:
        reasons.add("readiness:schema_hash_unknown")
    elif src_hash != dst_hash:
        reasons.add("readiness:schema_mismatch")

    verdict = Verdict.PASS if not reasons and results else Verdict.BLOCK
    if not results:
        reasons.add("no_controls")
    return GateResult(
        verdict=verdict,
        reasons=tuple(sorted(reasons)),
        controls=tuple(results),
        readiness=rd,
        input_digest=digest,
        source_errors=tuple(sorted(source_errors)),
    )
