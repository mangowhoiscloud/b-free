"""Unit prices from the providers' own price APIs (read-only, cached 24 h).

AWS   pricing:GetProducts (endpoint us-east-1). On-Demand term, first tier.
GCP   Cloud Billing Catalog API ``services/{id}/skus`` (list prices, USD), first tier above the free tier.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config

from .model import CACHE, iso, now_utc

PRICE_TTL_S = 24 * 3600
AWS_PRICING_REGION = "us-east-1"
AWS_PRICING_URL = "https://api.pricing.us-east-1.amazonaws.com (pricing:GetProducts)"
GCP_CATALOG_URL = "https://cloudbilling.googleapis.com/v1/services/{service}/skus"

# AWS usage type prefixes per region (the Pricing API's own codes).
AWS_UT_PREFIX = {"ap-northeast-2": "APN2", "ap-northeast-1": "APN1", "us-east-1": "", "us-west-2": "USW2"}

GCP_SERVICES = {
    "compute": "6F81-5844-456A",  # Compute Engine (instances, disks, Cloud NAT, load balancing proxies)
    "gke": "CCD8-9BF1-090E",  # Kubernetes Engine (cluster management fee)
    "networking": "E505-1604-58F8",  # Networking (Cloud NAT uptime and IP usage)
    "artifact_registry": "149C-F9EC-3994",
    "kms": "EE2F-D110-890C",
}


@dataclass(frozen=True)
class Price:
    usd: float
    unit: str  # provider's unit: Hrs, GB-Mo, h, GiBy.mo, mo ...
    source: dict[str, Any]  # name, ref (usage type or sku id), description, effective date, url, fetched_at


class PriceError(Exception):
    pass


def ut(region: str, suffix: str) -> str:
    prefix = AWS_UT_PREFIX.get(region)
    if prefix is None:
        raise PriceError(f"no AWS usage-type prefix known for region {region}")
    return f"{prefix}-{suffix}" if prefix else suffix


class AwsPrices:
    def __init__(self, session: boto3.Session | None = None) -> None:
        self._session = session or boto3.Session()
        self._client = None
        self._mx = threading.Lock()

    def _pricing(self):
        with self._mx:
            if self._client is None:
                self._client = self._session.client(
                    "pricing",
                    region_name=AWS_PRICING_REGION,
                    config=Config(retries={"max_attempts": 4, "mode": "standard"}),
                )
            return self._client

    def lookup(self, service_code: str, filters: dict[str, str], *, want_unit: str | None = None) -> Price:
        key = "aws-price:" + service_code + ":" + json.dumps(filters, sort_keys=True) + f":{want_unit}"
        value, _, _ = CACHE.get_or_fill(key, PRICE_TTL_S, lambda: self._lookup(service_code, filters, want_unit))
        if isinstance(value, PriceError):
            raise value
        return value

    def _lookup(self, service_code: str, filters: dict[str, str], want_unit: str | None) -> Price | PriceError:
        fl = [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()]
        resp = self._pricing().get_products(ServiceCode=service_code, Filters=fl, MaxResults=100)
        found: list[tuple[float, str, dict[str, Any]]] = []
        for raw in resp.get("PriceList", []):
            p = json.loads(raw) if isinstance(raw, str) else raw
            attrs = p.get("product", {}).get("attributes", {})
            for term in p.get("terms", {}).get("OnDemand", {}).values():
                for dim in term.get("priceDimensions", {}).values():
                    if str(dim.get("beginRange", "0")) not in ("0", "0.0"):
                        continue
                    if want_unit and dim.get("unit") != want_unit:
                        continue
                    usd = dim.get("pricePerUnit", {}).get("USD")
                    if usd is None:
                        continue
                    found.append(
                        (
                            float(usd),
                            dim.get("unit", ""),
                            {
                                "name": "AWS Pricing API",
                                "ref": attrs.get("usagetype"),
                                "sku": p.get("product", {}).get("sku"),
                                "description": dim.get("description"),
                                "effective_date": term.get("effectiveDate"),
                                "url": AWS_PRICING_URL,
                                "service_code": service_code,
                                "fetched_at": iso(now_utc()),
                            },
                        )
                    )
        if not found:
            return PriceError(f"AWS Pricing API returned no On-Demand price for {service_code} {filters}")
        if len({f[0] for f in found}) > 1:
            return PriceError(f"AWS Pricing API returned {len(found)} different prices for {service_code} {filters}")
        usd, unit, src = found[0]
        return Price(usd, unit, src)


class GcpCatalog:
    """SKUs of a few services, filtered to the regions we use, cached for a day."""

    def __init__(self, authed_get, quota_project: str | None) -> None:
        self._get = authed_get  # callable(url, params) -> dict (raises on HTTP error)
        self._quota_project = quota_project

    def skus(self, service: str) -> list[dict[str, Any]]:
        sid = GCP_SERVICES[service]
        value, _, _ = CACHE.get_or_fill(f"gcp-skus:{sid}", PRICE_TTL_S, lambda: self._fetch(sid))
        return value

    def _fetch(self, sid: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token = None
        while True:
            params = {"pageSize": "5000", "currencyCode": "USD"}
            if token:
                params["pageToken"] = token
            body = self._get(GCP_CATALOG_URL.format(service=sid), params)
            for s in body.get("skus", []):
                regions = s.get("serviceRegions", [])
                if not (set(regions) & {"asia-northeast1", "global"}):
                    continue
                out.append({k: s.get(k) for k in ("skuId", "description", "category", "serviceRegions", "pricingInfo")})
            token = body.get("nextPageToken")
            if not token:
                return out

    def find(self, service: str, *, region: str, must: list[str], exclude: list[str] = ()) -> Price:
        cands = []
        for s in self.skus(service):
            d = s.get("description") or ""
            if not all(m.lower() in d.lower() for m in must):
                continue
            if any(x.lower() in d.lower() for x in exclude):
                continue
            regions = s.get("serviceRegions") or []
            if region not in regions and "global" not in regions:
                continue
            cands.append(s)
        if len(cands) != 1:
            raise PriceError(f"GCP catalog: {len(cands)} SKUs match {must} (excluding {list(exclude)}) in {region}")
        s = cands[0]
        info = (s.get("pricingInfo") or [{}])[0]
        expr = info.get("pricingExpression", {})
        tiers = sorted(expr.get("tieredRates", []), key=lambda t: float(t.get("startUsageAmount", 0) or 0))
        if not tiers:
            raise PriceError(f"GCP catalog SKU {s.get('skuId')} has no rate")
        # First paid tier: a free tier (price 0 up to N units) is reported in the source, not hidden.
        paid = next((t for t in tiers if _money(t.get("unitPrice", {})) > 0), tiers[0])
        free_units = float(paid.get("startUsageAmount", 0) or 0)
        return Price(
            _money(paid.get("unitPrice", {})),
            expr.get("usageUnit", ""),
            {
                "name": "GCP Cloud Billing Catalog API",
                "ref": s.get("skuId"),
                "description": s.get("description"),
                "effective_date": info.get("effectiveTime"),
                "free_tier_units": free_units or None,
                "url": GCP_CATALOG_URL.format(service=GCP_SERVICES[service]),
                "fetched_at": iso(now_utc()),
            },
        )


def _money(m: dict[str, Any]) -> float:
    return int(m.get("units", 0) or 0) + (m.get("nanos", 0) or 0) / 1e9
