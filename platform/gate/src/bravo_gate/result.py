"""Probe observation: verdict PASS | FAIL | UNKNOWN, a reason code and evidence without secrets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PASS = "PASS"  # noqa: S105 - verdict, not a secret
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
VERDICTS = frozenset({PASS, FAIL, UNKNOWN})


@dataclass(frozen=True, slots=True)
class Obs:
    verdict: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason, "evidence": self.evidence}


def passed(reason: str = "ok", **evidence: Any) -> Obs:
    return Obs(PASS, reason, evidence)


def failed(reason: str, **evidence: Any) -> Obs:
    return Obs(FAIL, reason, evidence)


def unknown(reason: str, **evidence: Any) -> Obs:
    return Obs(UNKNOWN, reason, evidence)


def worst(observations: list[Obs], *, empty_reason: str = "no_probe") -> Obs:
    """FAIL beats UNKNOWN beats PASS. Evidence of every part is kept in order."""
    if not observations:
        return unknown(empty_reason)
    parts = [o.as_dict() for o in observations]
    for verdict in (FAIL, UNKNOWN):
        hit = [o for o in observations if o.verdict == verdict]
        if hit:
            return Obs(verdict, ",".join(sorted({o.reason for o in hit})), {"parts": parts})
    return Obs(PASS, "ok", {"parts": parts})


def error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]
