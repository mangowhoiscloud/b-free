"""T1 ending scene: cost by ``component`` tag through Cost Explorer (read-only).

Calls (all read-only; Cost Explorer bills each API request at USD 0.01, so the CLI runs on demand only):
    ce:ListCostAllocationTags   is the tag a cost allocation tag, and is it Active?
    ce:GetCostAndUsage          grouped by TAG component, only when the tag is Active
    ce:GetCostAndUsage          grouped by SERVICE (always; account-wide, so other projects are included)
Activating a cost allocation tag is a billing setting. This module never changes it; it reports the state.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import boto3
from botocore.config import Config

CE_REGION = "us-east-1"  # Cost Explorer endpoint
USD_PER_REQUEST = 0.01


def _amount(group_or_total: dict[str, Any], metric: str) -> tuple[float, str]:
    m = group_or_total[metric]
    return round(float(m["Amount"]), 4), m.get("Unit", "USD")


def cost_report(start: date, end: date, tag_key: str = "component", *, session: boto3.Session | None = None,
                metric: str = "UnblendedCost") -> dict[str, Any]:
    """Cost for [start, end) by tag (if the tag is active) and by service. Real values only."""
    if end <= start:
        raise ValueError("end must be after start")
    ce = (session or boto3.Session()).client("ce", region_name=CE_REGION,
                                             config=Config(retries={"max_attempts": 3, "mode": "standard"}))
    requests = 0
    out: dict[str, Any] = {
        "period": {"start": start.isoformat(), "end_exclusive": end.isoformat()},
        "metric": metric,
        "queried_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    tags = ce.list_cost_allocation_tags(TagKeys=[tag_key])
    requests += 1
    found = [t for t in tags.get("CostAllocationTags", []) if t.get("TagKey") == tag_key]
    status = found[0].get("Status") if found else None
    out["tag"] = {
        "key": tag_key,
        "status": status or "NOT_REGISTERED",
        "type": found[0].get("Type") if found else None,
        "last_updated": found[0].get("LastUpdatedDate") if found else None,
        "reason": None if status == "Active" else (
            "The tag key is listed but inactive. Activation is a billing setting (Billing console > Cost "
            "allocation tags, or ce:UpdateCostAllocationTagsStatus) and needs the account owner's approval."
            if status else
            "Billing does not list this user tag yet. AWS lists a user-defined tag only after resources "
            "carrying it appear in billing data (up to 24 hours), and it must then be activated (a billing "
            "setting that needs approval). Cost by tag is not available until then."),
    }

    period = {"Start": start.isoformat(), "End": end.isoformat()}
    if status == "Active":
        resp = ce.get_cost_and_usage(TimePeriod=period, Granularity="MONTHLY", Metrics=[metric],
                                     GroupBy=[{"Type": "TAG", "Key": tag_key}])
        requests += 1
        by_tag: dict[str, float] = {}
        unit = "USD"
        for r in resp["ResultsByTime"]:
            for g in r.get("Groups", []):
                value = g["Keys"][0].split("$", 1)[-1] or "(untagged)"
                amt, unit = _amount(g["Metrics"], metric)
                by_tag[value] = round(by_tag.get(value, 0.0) + amt, 4)
        out["by_tag"] = {"unit": unit, "groups": dict(sorted(by_tag.items(), key=lambda kv: -kv[1]))}
    else:
        out["by_tag"] = None

    resp = ce.get_cost_and_usage(TimePeriod=period, Granularity="MONTHLY", Metrics=[metric],
                                 GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}])
    requests += 1
    by_service: dict[str, float] = {}
    unit = "USD"
    estimated = False
    for r in resp["ResultsByTime"]:
        estimated = estimated or bool(r.get("Estimated"))
        for g in r.get("Groups", []):
            amt, unit = _amount(g["Metrics"], metric)
            by_service[g["Keys"][0]] = round(by_service.get(g["Keys"][0], 0.0) + amt, 4)
    out["by_service"] = {
        "scope": "whole account (Bravo and any other project in the same account)",
        "unit": unit,
        "estimated": estimated,
        "total": round(sum(by_service.values()), 4),
        "groups": dict(sorted(by_service.items(), key=lambda kv: -kv[1])),
    }
    out["cost_explorer_requests"] = requests
    out["cost_explorer_request_charge_usd"] = round(requests * USD_PER_REQUEST, 2)
    return out


def default_period(today: date | None = None) -> tuple[date, date]:
    """Month to date, end exclusive = tomorrow (Cost Explorer data lags up to 24 hours)."""
    t = today or datetime.now(UTC).date()
    return t.replace(day=1), t + timedelta(days=1)
