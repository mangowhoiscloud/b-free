"""Source-parity gate (04 2, CONTRACT.md 6-7). Deterministic rules, no LLM (NFR-5).

Per control (source, target):
    PASS/PASS -> pass
    PASS/FAIL -> BLOCK ``regression``
    FAIL/FAIL -> BLOCK ``fail_both`` (failure, not equivalence)
    any UNKNOWN -> BLOCK ``unknown``
    anything else (FAIL/PASS, unexpected values) -> BLOCK ``not_equivalent`` (fail closed;
    the 04 table does not list it, so it is not treated as a pass)
Control exception (spec S1 5b, contract 15 §9): a row with ``dst == FAIL`` and ``src`` PASS or FAIL whose
approved, unexpired exception matches control, ``src``, ``dst`` and the destination ``evidence_digest`` becomes
``EXCEPTION`` (``exception_approved``) and is left out of the parity verdict. UNKNOWN is never excepted (5a).
Readiness: ``lag_bytes`` at most the threshold, ``schema_hash.src == schema_hash.dst``.
Executor unreachable or malformed -> every input UNKNOWN -> BLOCK ``executor_unreachable``.
Same input -> same verdict: inputs (observations and the exceptions applied) are canonicalised and hashed
(``input_digest``).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

RULE_VERSION = "gate-rules/2026-11-02.2"
_VALUES = frozenset({"PASS", "FAIL", "UNKNOWN"})
EXCEPTION = "EXCEPTION"
EVIDENCE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class Verdict(StrEnum):
    PASS = "PASS"
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True)
class GatePolicy:
    required_controls: tuple[str, ...]
    max_lag_bytes: int


@dataclass(frozen=True, slots=True)
class ControlException:
    """An approved, unexpired control exception for this plan's destination (contract 15 §9.1)."""

    exception_id: str
    control_id: str
    src: str
    dst: str
    evidence_digest: str
    expires_at: str | None = None
    approved_by: str | None = None
    compensating_control: str | None = None

    def binding(self) -> dict[str, str]:
        return {"exception_id": self.exception_id, "control_id": self.control_id, "src": self.src, "dst": self.dst,
                "evidence_digest": self.evidence_digest}


@dataclass(frozen=True, slots=True)
class ControlResult:
    control_id: str
    src: str
    dst: str
    verdict: str  # PASS | BLOCK | EXCEPTION
    reason: str | None
    evidence_digest: str | None = None  # destination evidence digest (platform/gate report), else None
    exception_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "src": self.src,
            "dst": self.dst,
            "verdict": self.verdict,
            "reason": self.reason,
            "evidence_digest": self.evidence_digest,
            "exception_id": self.exception_id,
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
    exceptions: tuple[dict[str, Any], ...] = field(default=())  # applied: binding + expires_at, approved_by


def _norm(value: Any) -> str:
    return value if isinstance(value, str) and value in _VALUES else "UNKNOWN"


def _digest(value: Any) -> str | None:
    return value if isinstance(value, str) and EVIDENCE_DIGEST.match(value) else None


def exceptable(src: str, dst: str) -> bool:
    """A measured FAIL on the destination, with a measured source (5b). UNKNOWN on either side never (5a)."""
    return dst == "FAIL" and src in ("PASS", "FAIL")


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
    by_id: dict[str, dict[str, Any]] = {}
    for item in controls or []:
        if not isinstance(item, dict) or not isinstance(item.get("control_id"), str):
            continue
        cid = item["control_id"]
        entry = {"src": _norm(item.get("src")), "dst": _norm(item.get("dst"))}
        digest = _digest(item.get("evidence_digest"))
        if digest is not None:
            entry["evidence_digest"] = digest
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


def _matching(c: dict[str, Any], cid: str, exceptions: tuple[ControlException, ...]) -> ControlException | None:
    if not exceptable(c["src"], c["dst"]) or "evidence_digest" not in c:
        return None
    for ex in exceptions:
        if (ex.control_id, ex.src, ex.dst, ex.evidence_digest) == (cid, c["src"], c["dst"], c["evidence_digest"]):
            return ex
    return None


def evaluate(
    controls: list[dict[str, Any]] | None,
    readiness: dict[str, Any] | None,
    policy: GatePolicy,
    source_errors: tuple[str, ...] = (),
    exceptions: tuple[ControlException, ...] = (),
) -> GateResult:
    """Pure function: the verdict depends only on the arguments.

    ``exceptions`` are the approved, unexpired exceptions of this plan and destination (the caller filters on
    status, expiry and destination). Only the ones that match a row are applied and enter ``input_digest``.
    """
    canon = canonical_inputs(controls, readiness, policy)
    canon["source_errors"] = sorted(source_errors)
    applied: list[ControlException] = []
    for cid, c in canon["controls"].items():
        ex = _matching(c, cid, exceptions)
        if ex is not None:
            applied.append(ex)
    canon["exceptions"] = sorted((ex.binding() for ex in applied), key=lambda b: b["control_id"])
    digest = hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    by_control = {ex.control_id: ex for ex in applied}
    reasons: set[str] = set(source_errors)
    results: list[ControlResult] = []
    for cid, c in canon["controls"].items():
        ex = by_control.get(cid)
        if ex is not None:
            # 5b: shown separately as an approved exception and left out of the parity verdict.
            results.append(ControlResult(cid, c["src"], c["dst"], EXCEPTION, "exception_approved",
                                         c["evidence_digest"], ex.exception_id))
            continue
        verdict, reason = judge_control(c["src"], c["dst"])
        if c.get("missing") and not source_errors:
            reason = "control_missing"  # Executor answered but did not report this control
        results.append(ControlResult(cid, c["src"], c["dst"], verdict, reason, c.get("evidence_digest")))
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
        exceptions=tuple(
            {**ex.binding(), "expires_at": ex.expires_at, "approved_by": ex.approved_by,
             "compensating_control": ex.compensating_control}
            for ex in sorted(applied, key=lambda e: e.control_id)
        ),
    )


def exception_drift(
    result: GateResult, open_exceptions: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], str, dict[str, Any]]]:
    """Open exceptions (``requested`` / ``approved``) whose control was measured differently in ``result``.

    Returns ``(exception, cause, now)`` with cause ``verdict_changed`` (src/dst differ) or ``evidence_changed``
    (same pair, other destination evidence). A control that is missing or UNKNOWN on either side is not a
    measured change: the exception is not used by that verdict (it blocks as UNKNOWN) and stays as it is.
    """
    rows = {c.control_id: c for c in result.controls}
    out = []
    for ex in open_exceptions:
        row = rows.get(ex["control_id"])
        if row is None or "UNKNOWN" in (row.src, row.dst):
            continue
        now = {"src": row.src, "dst": row.dst, "evidence_digest": row.evidence_digest}
        if (row.src, row.dst) != (ex["src"], ex["dst"]):
            out.append((ex, "verdict_changed", now))
        elif row.evidence_digest != ex["evidence_digest"]:
            out.append((ex, "evidence_changed", now))
    return out
