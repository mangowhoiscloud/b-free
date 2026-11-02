"""Run statistics and the NFR-2 verdict (pure functions)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile, q in [0, 100]."""
    if not values:
        return None
    if not 0 <= q <= 100:
        raise ValueError("q must be within [0, 100]")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass(slots=True)
class RunStats:
    sent: int = 0
    acked: int = 0
    acked_on_conflict: int = 0
    retried_requests: int = 0
    retry_attempts: int = 0
    failed: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    first_error_at: float | None = None
    last_error_at: float | None = None
    longest_gap_ms: float = 0.0
    _last_ack_at: float | None = None

    def record_ack(self, now: float, latency_ms: float, on_conflict: bool) -> None:
        self.acked += 1
        if on_conflict:
            self.acked_on_conflict += 1
        self.latencies_ms.append(latency_ms)
        if self._last_ack_at is not None:
            self.longest_gap_ms = max(self.longest_gap_ms, (now - self._last_ack_at) * 1000)
        self._last_ack_at = now

    def summary(self) -> dict[str, object]:
        lat = self.latencies_ms
        return {
            "sent": self.sent,
            "acked": self.acked,
            "acked_on_conflict": self.acked_on_conflict,
            "retried": self.retried_requests,
            "retry_attempts": self.retry_attempts,
            "failed": self.failed,
            "max_latency_ms": round(max(lat), 1) if lat else None,
            "p50_latency_ms": _round(percentile(lat, 50)),
            "p99_latency_ms": _round(percentile(lat, 99)),
            "longest_ack_gap_ms": round(self.longest_gap_ms, 1),
        }


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


@dataclass(frozen=True, slots=True)
class Nfr2Result:
    acked: int
    found_once: int
    lost: list[str]
    duplicated: list[str]
    failed: int

    @property
    def ok(self) -> bool:
        return not self.lost and not self.duplicated and self.failed == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "acked": self.acked,
            "found_exactly_once": self.found_once,
            "lost": len(self.lost),
            "duplicated": len(self.duplicated),
            "failed_requests": self.failed,
            "lost_sample": self.lost[:10],
            "duplicated_sample": self.duplicated[:10],
        }


def evaluate_nfr2(acked_keys: Sequence[UUID], counts: Mapping[UUID, int], failed: int) -> Nfr2Result:
    """Every acked idem_key must be present exactly once in the final DB, and no request may fail."""
    lost = [str(k) for k in acked_keys if counts.get(k, 0) == 0]
    dup = [str(k) for k in acked_keys if counts.get(k, 0) > 1]
    once = sum(1 for k in acked_keys if counts.get(k, 0) == 1)
    return Nfr2Result(acked=len(acked_keys), found_once=once, lost=lost, duplicated=dup, failed=failed)
