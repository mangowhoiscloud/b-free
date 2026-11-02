"""Cloudflare edge items (read-only API): Load Balancing subscription and its endpoints, Zero Trust Free.

The API token comes from the environment (a Secret piped from platform OpenBao, D-26); it is never logged or
returned. The subscription price is read from ``GET /accounts/{id}/subscriptions`` (needs Billing Read). Without
that permission the amount stays "연결 안 됨": Cloudflare publishes the Load Balancing price only in the dashboard.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import requests

from ..redact import redact
from .aws_inventory import monthly_segments
from .model import Line, iso, now_utc

API = "https://api.cloudflare.com/client/v4"
TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
ZT_PLAN_URL = "https://www.cloudflare.com/plans/zero-trust-services/"


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC) if s else None


class CloudflareInventory:
    def __init__(self, account_id: str, zone_id: str | None, zero_trust_plan: str | None = None,
                 zero_trust_since: str | None = None) -> None:
        self.account_id = account_id
        self.zone_id = zone_id
        self.zero_trust_plan = zero_trust_plan
        self.zero_trust_since = zero_trust_since
        self.lines: list[Line] = []
        self.not_connected: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.now = now_utc()
        self._token = os.environ.get(TOKEN_ENV, "").strip()

    def _get(self, path: str) -> tuple[Any, str | None]:
        try:
            r = requests.get(API + path, headers={"Authorization": f"Bearer {self._token}"}, timeout=20)
            body = r.json()
        except (requests.RequestException, ValueError) as exc:
            return None, redact(f"{type(exc).__name__}")
        if not body.get("success"):
            errs = "; ".join(f"{e.get('code')} {e.get('message')}" for e in body.get("errors", []))
            return None, redact(f"HTTP {r.status_code} {errs}")[:200]
        return body.get("result"), None

    def collect(self) -> CloudflareInventory:
        self._zero_trust()
        if not self._token:
            self.not_connected.append({"provider": "cloudflare", "what": "Cloudflare API(LB 구독 · 끝점)",
                                       "reason": f"수집기에 읽기용 API 토큰({TOKEN_ENV})이 없음"})
            return self
        pools, perr = self._get(f"/accounts/{self.account_id}/load_balancers/pools")
        lbs, lerr = self._get(f"/zones/{self.zone_id}/load_balancers") if self.zone_id else (None, "zone_id 설정 없음")
        if perr or lerr:
            self.not_connected.append({"provider": "cloudflare", "what": "Load Balancing 풀 · LB 목록",
                                       "reason": perr or lerr})
        pools = pools or []
        lbs = lbs or []
        endpoints = [(p.get("name"), o.get("name"), o.get("enabled"), o.get("weight"))
                     for p in pools for o in p.get("origins", [])]
        created = [t for t in (_ts(x.get("created_on")) for x in [*pools, *lbs]) if t]
        start = min(created) if created else None
        subs, serr = self._get(f"/accounts/{self.account_id}/subscriptions")
        sub = None
        if subs:
            sub = next((s for s in subs if "load" in str((s.get("rate_plan") or {}).get("id", "")).lower()
                        or "load_balancing" in str(s.get("product", "")).lower()), None)
        price = float(sub["price"]) if sub and sub.get("price") is not None else None
        line = Line(
            line_id="cloudflare:load-balancing", provider="cloudflare", service="Load Balancing",
            resource_type="subscription", resource_id=(sub or {}).get("id") or "load_balancing",
            resource=f"Cloudflare Load Balancing 구독 (LB {len(lbs)}개, 풀 {len(pools)}개, 끝점 {len(endpoints)}개)",
            environment="edge", component="edge", component_source="inferred:edge", basis_code="flat",
            unit_price=price, unit=f"{(sub or {}).get('currency') or 'USD'}/{(sub or {}).get('frequency') or 'month'}",
            quantity_unit="month",
            price_source=({"name": "Cloudflare API subscriptions", "ref": (sub or {}).get("rate_plan"),
                           "url": f"{API}/accounts/{{account_id}}/subscriptions", "fetched_at": iso(self.now)}
                          if sub else None),
            segments=monthly_segments(start, self.now, 1.0) if start else [],
            running={"from": iso(start), "to": None, "state": "subscribed"},
            detail={"load_balancers": [{"name": x.get("name"), "created_on": x.get("created_on"),
                                        "steering": x.get("steering_policy")} for x in lbs],
                    "endpoints": [{"pool": p, "endpoint": o, "enabled": e, "weight": w} for p, o, e, w in endpoints]},
            note="월 정액을 구독 관측 시작(가장 이른 LB · 풀 생성 시각)부터 시간 비율로 배분. 청구 단위는 월")
        if price is None:
            line.status = "not_connected"
            line.reason = (f"구독 금액을 읽지 못함({serr or '구독 목록에 Load Balancing 없음'}). "
                           "토큰에 Billing Read 권한이 필요. 공식 문서는 금액을 대시보드에서만 보임")
        self.lines.append(line)
        return self

    def _zero_trust(self) -> None:
        if self.zero_trust_plan != "free":
            return
        since = _ts(self.zero_trust_since)
        line = Line(
            line_id="cloudflare:zero-trust", provider="cloudflare", service="Zero Trust", resource_type="subscription",
            resource_id="zero_trust_free", resource="Cloudflare Zero Trust Free (Tunnel · Access · 서비스 토큰)",
            environment="edge", component="edge", component_source="inferred:edge", basis_code="flat",
            unit_price=0.0, unit="USD/month", quantity_unit="month",
            price_source={"name": "Cloudflare Zero Trust 요금제(Free)", "ref": "Free", "url": ZT_PLAN_URL},
            segments=monthly_segments(since, self.now, 1.0) if since else [],
            running={"from": iso(since), "to": None, "state": "active"},
            note="Free 요금제라 0원. 활성 시각은 배포 기록(CLOUD-E2E-CONTRACT 7절)")
        if not since:
            line.fixed_subtotal = 0.0
        self.lines.append(line)
