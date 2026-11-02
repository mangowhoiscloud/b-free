"""Cost Explorer actuals (청구), month to date, cached one hour (each Cost Explorer request bills USD 0.01).

Requests per fill: ListCostAllocationTags (1), GetCostAndUsage by SERVICE (1), and by TAG ``component`` only when
the tag is Active (1). The period is always the current UTC month to tomorrow, so the cache key changes once a day
and the console's polling never multiplies the calls. Activating the tag is a billing setting this module never
makes; ``deploy/platform/scripts/cost-tag.sh`` does it (operator, approved by the lead).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ..redact import redact
from .model import CACHE, TAG_PENDING, Line, Segment, iso, now_utc

CE_REGION = "us-east-1"
CE_TTL_S = 3600
USD_PER_REQUEST = 0.01
METRIC = "UnblendedCost"
ENV_BY_COMPONENT = {"source": "source-aws", "platform": "platform", "target": "target-aws"}


def month_period(today: date | None = None) -> tuple[date, date]:
    t = today or datetime.now(UTC).date()
    return t.replace(day=1), t + timedelta(days=1)


def fetch(start: date, end: date, tag_key: str = "component", session: boto3.Session | None = None) -> dict[str, Any]:
    ce = (session or boto3.Session()).client(
        "ce", region_name=CE_REGION, config=Config(retries={"max_attempts": 3, "mode": "standard"})
    )
    requests = 0
    out: dict[str, Any] = {
        "period": {"start": start.isoformat(), "end_exclusive": end.isoformat()},
        "metric": METRIC,
        "status": "ok",
        "reason": None,
    }
    try:
        tags = ce.list_cost_allocation_tags(TagKeys=[tag_key])
        requests += 1
        found = [t for t in tags.get("CostAllocationTags", []) if t.get("TagKey") == tag_key]
        status = found[0].get("Status") if found else "NOT_REGISTERED"
        out["tag"] = {
            "key": tag_key,
            "status": status,
            "active": status == "Active",
            "label": "활성" if status == "Active" else TAG_PENDING,
            "last_updated": found[0].get("LastUpdatedDate") if found else None,
            "reason": None
            if status == "Active"
            else (
                "비용 할당 태그로 등록은 되었으나 비활성"
                if found
                else "청구 데이터에 아직 이 사용자 태그가 없음(태그 단 자원이 청구에 잡힌 뒤 최대 24시간). "
                "그때까지 활성화 요청은 'Tag keys not found'로 거절되고, 자원별 추정을 씀"
            ),
        }
        period = {"Start": start.isoformat(), "End": end.isoformat()}
        resp = ce.get_cost_and_usage(
            TimePeriod=period, Granularity="DAILY", Metrics=[METRIC], GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}]
        )
        requests += 1
        by_day: dict[str, dict[str, float]] = {}
        estimated = False
        for r in resp["ResultsByTime"]:
            estimated = estimated or bool(r.get("Estimated"))
            day = r["TimePeriod"]["Start"]
            for g in r.get("Groups", []):
                amt = float(g["Metrics"][METRIC]["Amount"])
                if amt:
                    by_day.setdefault(day, {})[g["Keys"][0]] = round(amt, 6)
        groups: dict[str, float] = {}
        for d in by_day.values():
            for k, v in d.items():
                groups[k] = round(groups.get(k, 0.0) + v, 6)
        out["by_service"] = {
            "scope": "계정 전체(같은 계정의 다른 프로젝트 포함). 합계에 넣지 않는 참고 값",
            "estimated": estimated,
            "total": round(sum(groups.values()), 4),
            "groups": dict(sorted(groups.items(), key=lambda kv: -kv[1])),
            "daily": by_day,
        }
        if status == "Active":
            resp = ce.get_cost_and_usage(
                TimePeriod=period, Granularity="DAILY", Metrics=[METRIC], GroupBy=[{"Type": "TAG", "Key": tag_key}]
            )
            requests += 1
            daily: dict[str, dict[str, float]] = {}
            for r in resp["ResultsByTime"]:
                for g in r.get("Groups", []):
                    value = g["Keys"][0].split("$", 1)[-1] or "(untagged)"
                    amt = float(g["Metrics"][METRIC]["Amount"])
                    if amt:
                        daily.setdefault(r["TimePeriod"]["Start"], {})[value] = round(amt, 6)
            out["by_component_daily"] = daily
        else:
            out["by_component_daily"] = None
    except (ClientError, BotoCoreError) as exc:
        code = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else type(exc).__name__
        out["status"] = "not_connected"
        out["reason"] = redact(f"Cost Explorer {code}: {exc}")[:300]
    out["requests_on_fill"] = requests
    out["charge_usd_on_fill"] = round(requests * USD_PER_REQUEST, 2)
    return out


def cached_actuals(session: boto3.Session | None = None, today: date | None = None) -> dict[str, Any]:
    start, end = month_period(today)
    value, filled_at, filled_now = CACHE.get_or_fill(
        f"ce:{start}:{end}", CE_TTL_S, lambda: fetch(start, end, session=session), stale_ok=True
    )
    return {
        **value,
        "cached_at": iso(filled_at),
        "ttl_s": CE_TTL_S,
        "from_cache": not filled_now,
        "requests_this_response": value.get("requests_on_fill", 0) if filled_now else 0,
    }


def billed_lines(actuals: dict[str, Any]) -> list[Line]:
    """Per-component actual lines, one per component, daily segments (UTC days, as Cost Explorer reports)."""
    daily = actuals.get("by_component_daily")
    if not daily:
        return []
    per_comp: dict[str, list[Segment]] = {}
    for day, groups in daily.items():
        d0 = datetime.fromisoformat(day).replace(tzinfo=UTC)
        for comp, amt in groups.items():
            per_comp.setdefault(comp, []).append(Segment(d0, min(d0 + timedelta(days=1), now_utc()), amt / 24.0))
    out = []
    for comp, segs in sorted(per_comp.items()):
        out.append(
            Line(
                line_id=f"aws:billed:{comp}",
                provider="aws",
                service="Cost Explorer",
                resource_type="billed_component",
                resource_id=f"component={comp}",
                resource=f"AWS 청구 component={comp}",
                environment=ENV_BY_COMPONENT.get(comp),
                component=None if comp == "(untagged)" else comp,
                component_source="tag",
                basis_code="billed",
                unit_price=1.0,
                unit="USD/USD",
                quantity_unit="USD",
                price_source={"name": "AWS Cost Explorer GetCostAndUsage", "ref": f"TAG component, {METRIC}, DAILY"},
                segments=segs,
                running={"from": None, "to": None, "state": "billed"},
                note="하루 금액을 그 날 24시간에 고르게 나눠 단계에 배분(Cost Explorer는 일 단위, 최대 24시간 지연)",
            )
        )
    return out
