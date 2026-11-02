"""Line items, time arithmetic and the TTL cache shared by the cost modules."""

from __future__ import annotations

import calendar
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

# Basis labels (the console shows the Korean label; code is for logic).
BASIS = {
    "billed": "청구",            # Cost Explorer (or GCP billing export) actual
    "estimated": "추정",         # list price x observed running time from the resource's own timestamps
    "flat": "정액(구독)",         # subscription; amount only when an official price page is cited
    "api_equivalent": "API 가격 환산(청구 아님)",  # recorded tokens x API price; never billed, never in the total
}
NOT_CONNECTED = "연결 안 됨"
TAG_PENDING = "태그 활성화 대기"
DAY_TZ = timedelta(hours=9)  # daily buckets in KST/JST (the team and the judges); stated in the response
GCP_MONTH_HOURS = 730.0      # GCP list prices per month assume 730 hours (cloud.google.com pricing pages)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_ts(value: str | datetime | date | None) -> datetime | None:
    """ISO date or datetime (``Z`` or offset) -> aware UTC datetime. A bare date means 00:00 UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    s = str(value).strip().replace("Z", "+00:00")
    if len(s) == 10:
        return datetime.fromisoformat(s).replace(tzinfo=UTC)
    dt = datetime.fromisoformat(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") if dt else None


def overlap_hours(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    s, e = max(a0, b0), min(a1, b1)
    return max(0.0, (e - s).total_seconds() / 3600.0)


def aws_month_hours(at: datetime) -> float:
    """AWS prorates per-month prices by the seconds in that calendar month (EBS pricing example)."""
    return calendar.monthrange(at.year, at.month)[1] * 24.0


@dataclass
class Segment:
    """``rate`` units of quantity per hour between ``start`` and ``end`` (UTC)."""

    start: datetime
    end: datetime
    rate: float

    def quantity(self, w0: datetime, w1: datetime) -> float:
        return self.rate * overlap_hours(self.start, self.end, w0, w1)


@dataclass
class Line:
    line_id: str
    provider: str            # aws | gcp | cloudflare | llm
    service: str
    resource_type: str
    resource_id: str
    resource: str            # display name
    environment: str | None  # source-aws | platform | target-gcp | target-onprem | edge | llm
    component: str | None    # NFR-8 component tag value: source | platform | target | edge | llm
    component_source: str    # "tag" or "inferred:<rule>"
    basis_code: str          # billed | estimated | flat | api_equivalent
    unit_price: float | None
    unit: str | None         # e.g. "USD/hour", "USD/GB-month"
    quantity_unit: str | None
    price_source: dict[str, Any] | None
    segments: list[Segment] = field(default_factory=list)
    running: dict[str, Any] | None = None
    note: str | None = None
    status: str = "ok"       # ok | not_connected
    reason: str | None = None
    in_total: bool = True
    fixed_quantity: float | None = None   # quantity that is not time-allocated (recorded tokens of one run)
    fixed_subtotal: float | None = None   # recorded amount of one run (API-price equivalent)
    plan_id: str | None = None            # agent runs carry their plan

    def quantity(self, w0: datetime, w1: datetime) -> float | None:
        if self.fixed_quantity is not None:
            at = self.running.get("_at") if self.running else None
            return self.fixed_quantity if at is None or w0 <= at < w1 else 0.0
        if not self.segments:
            return None
        return sum(s.quantity(w0, w1) for s in self.segments)

    def subtotal(self, w0: datetime, w1: datetime) -> float | None:
        if self.fixed_subtotal is not None:
            at = self.running.get("_at") if self.running else None
            return self.fixed_subtotal if at is None or w0 <= at < w1 else 0.0
        q = self.quantity(w0, w1)
        if q is None or self.unit_price is None:
            return None
        return q * self.unit_price

    def first_at(self) -> datetime | None:
        if self.running and self.running.get("_at"):
            return self.running["_at"]
        return min((s.start for s in self.segments), default=None)


class TTLCache:
    """Process-wide cache with one lock per key, so concurrent requests never repeat a billed call."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, datetime, Any]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get_or_fill(self, key: str, ttl_s: float, fill: Callable[[], Any]) -> tuple[Any, datetime, bool]:
        """Value, the time it was filled, and whether this call filled it."""
        with self._lock(key):
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < ttl_s:
                return hit[2], hit[1], False
            value = fill()
            filled = now_utc()
            self._data[key] = (time.monotonic(), filled, value)
            return value, filled, True

    def clear(self) -> None:
        with self._guard:
            self._data.clear()


CACHE = TTLCache()
