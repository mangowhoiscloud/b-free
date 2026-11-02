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
Two sources, one control id (Executor ``/v1/controls`` and the platform/gate report, rows tagged ``source``),
merged per side (src, dst) by the lead's rule of 2026-11-02 (contract 15 §9.5):
    both the same -> that value; one UNKNOWN (or unreadable) and the other PASS/FAIL -> the other, and the
    UNKNOWN source is listed in ``missing_sources``; PASS vs FAIL -> UNKNOWN, reason ``sources_disagree``
    (still blocks, 5a). Both sources are kept in ``sources``. The row's ``evidence_digest`` is the digest of the
    source that measured the merged ``dst`` (only the gate has digests), so an exception binds the gate's evidence.
    The same source twice (or untagged duplicates) is contradictory: UNKNOWN.
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

RULE_VERSION = "gate-rules/2026-11-02.3"
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
    sources: tuple[dict[str, Any], ...] | None = None  # both reports when two sources gave this control
    missing_sources: tuple[str, ...] = ()  # sources that answered UNKNOWN where the other measured

    def as_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "src": self.src,
            "dst": self.dst,
            "verdict": self.verdict,
            "reason": self.reason,
            "evidence_digest": self.evidence_digest,
            "exception_id": self.exception_id,
            "sources": [dict(x) for x in self.sources] if self.sources is not None else None,
            "missing_sources": list(self.missing_sources),
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


def _merge(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """One control's canonical entry from its reports (module docstring rule)."""
    if len(reports) == 1:
        r = reports[0]
        entry = {"src": r["src"], "dst": r["dst"]}
        if r["evidence_digest"] is not None:
            entry["evidence_digest"] = r["evidence_digest"]
        return entry
    names = [r["source"] for r in reports]
    if len(reports) != 2 or None in names or names[0] == names[1]:
        return {"src": "UNKNOWN", "dst": "UNKNOWN"}  # same source twice / untagged: contradictory, fail closed
    a, b = sorted(reports, key=lambda r: r["source"])
    entry: dict[str, Any] = {"sources": {r["source"]: {k: r[k] for k in ("src", "dst", "evidence_digest")}
                                         for r in (a, b)}}
    missing: set[str] = set()
    disagree = False
    for side in ("src", "dst"):
        va, vb = a[side], b[side]
        if va == vb:
            entry[side] = va
        elif va == "UNKNOWN":
            entry[side] = vb
            missing.add(a["source"])
        elif vb == "UNKNOWN":
            entry[side] = va
            missing.add(b["source"])
        else:  # PASS vs FAIL: the sources disagree; UNKNOWN, so it blocks (5a)
            entry[side] = "UNKNOWN"
            disagree = True
    entry["missing_sources"] = sorted(missing)
    if disagree:
        entry["disagree"] = "1"
    digests = {r["evidence_digest"] for r in (a, b)
               if r["evidence_digest"] is not None and r["dst"] == entry["dst"] != "UNKNOWN"}
    if len(digests) == 1:
        entry["evidence_digest"] = digests.pop()
    return entry


def canonical_inputs(
    controls: list[dict[str, Any]] | None, readiness: dict[str, Any] | None, policy: GatePolicy
) -> dict[str, Any]:
    reports: dict[str, list[dict[str, Any]]] = {}
    for item in controls or []:
        if not isinstance(item, dict) or not isinstance(item.get("control_id"), str):
            continue
        source = item.get("source")
        reports.setdefault(item["control_id"], []).append({
            "source": source if isinstance(source, str) and source else None,
            "src": _norm(item.get("src")),
            "dst": _norm(item.get("dst")),
            "evidence_digest": _digest(item.get("evidence_digest")),
        })
    by_id: dict[str, dict[str, Any]] = {cid: _merge(rs) for cid, rs in reports.items()}
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


def _sources(c: dict[str, Any]) -> tuple[dict[str, Any], ...] | None:
    if "sources" not in c:
        return None
    return tuple({"source": name, **vals} for name, vals in sorted(c["sources"].items()))


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
                                         c["evidence_digest"], ex.exception_id, sources=_sources(c),
                                         missing_sources=tuple(c.get("missing_sources") or ())))
            continue
        verdict, reason = judge_control(c["src"], c["dst"])
        if c.get("missing") and not source_errors:
            reason = "control_missing"  # Executor answered but did not report this control
        if c.get("disagree"):
            reason = "sources_disagree"
        results.append(ControlResult(cid, c["src"], c["dst"], verdict, reason, c.get("evidence_digest"),
                                     sources=_sources(c), missing_sources=tuple(c.get("missing_sources") or ())))
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
