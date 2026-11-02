"""Assemble the receipt (``cost/v1``): line items, period and plan phases, daily series, totals.

``build_receipt`` is pure (lines in, JSON out) and is what the tests exercise. ``receipt_for_request`` reads the
config, fills the caches (inventory 5 min, prices 24 h, Cost Explorer 1 h) and calls it.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .model import BASIS, CACHE, DAY_TZ, NOT_CONNECTED, TAG_PENDING, Line, iso, now_utc, parse_ts

SCHEMA = "cost/v1"
CONFIG_ENV = "BRAVO_COST_CONFIG"
USAGE_DIR_ENV = "BRAVO_LLM_USAGE_DIR"
INVENTORY_TTL_S = 300
PHASES = ("before", "during", "after")
PHASE_KO = {"before": "이전 전", "during": "이전 중", "after": "이전 후"}


class CostRequestError(ValueError):
    pass


def load_config(path: str | None = None) -> dict[str, Any]:
    p = path or os.environ.get(CONFIG_ENV, "").strip()
    if not p:
        return {}
    data = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise CostRequestError(f"{CONFIG_ENV} must hold a mapping")
    return data


# --- collection ------------------------------------------------------------------------------------------


def collect_inventory(cfg: dict[str, Any]) -> dict[str, Any]:
    """All priced lines with their sources. Each provider fails alone into ``not_connected``."""
    lines: list[Line] = []
    not_connected: list[dict[str, Any]] = []
    notes: list[str] = []
    sources: list[dict[str, Any]] = []

    aws_cfg = cfg.get("aws") or {}
    if aws_cfg.get("regions"):
        from .aws_inventory import AwsInventory

        try:
            inv = AwsInventory(list(aws_cfg["regions"])).collect()
            lines += inv.lines
            not_connected += inv.not_connected
            notes += inv.notes
            sources.append(
                {
                    "name": "AWS Describe/List (EC2, EBS, EKS, NAT, ELB, S3, KMS, ECR, Secrets Manager, RDS, "
                    "CloudTrail) + CloudWatch",
                    "status": "partial" if inv.not_connected else "ok",
                }
            )
        except Exception as exc:  # noqa: BLE001 - credentials missing etc.: a reason, never a value
            not_connected.append({"provider": "aws", "what": "AWS 인벤토리", "reason": _reason(exc)})
            sources.append({"name": "AWS Describe/List", "status": "not_connected"})
    else:
        not_connected.append({"provider": "aws", "what": "AWS 인벤토리", "reason": "비용 설정에 aws.regions 없음"})

    gcp_cfg = cfg.get("gcp") or {}
    if gcp_cfg.get("project"):
        from .gcp_inventory import GcpInventory

        try:
            ginv = GcpInventory(gcp_cfg["project"], list(gcp_cfg.get("regions") or ["asia-northeast1"])).collect()
            lines += ginv.lines
            not_connected += ginv.not_connected
            notes += ginv.notes
            sources.append(
                {
                    "name": f"GCP REST (GKE, Compute, Artifact Registry, KMS) project {gcp_cfg['project']}",
                    "status": "partial" if len(ginv.not_connected) > 1 else "ok",
                }
            )
        except Exception as exc:  # noqa: BLE001
            not_connected.append(
                {
                    "provider": "gcp",
                    "what": f"GCP 인벤토리({gcp_cfg['project']})",
                    "reason": "GCP 자격 없음 또는 거부: " + _reason(exc),
                }
            )
            sources.append({"name": "GCP REST", "status": "not_connected"})
    else:
        not_connected.append({"provider": "gcp", "what": "GCP 인벤토리", "reason": "비용 설정에 gcp.project 없음"})

    cf_cfg = cfg.get("cloudflare")
    if isinstance(cf_cfg, dict):
        from .cloudflare import CloudflareInventory

        # Account and zone ids are not secrets but stay out of Git (gitignored tfvars): environment first.
        account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip() or cf_cfg.get("account_id") or ""
        zone = os.environ.get("CLOUDFLARE_ZONE_ID", "").strip() or cf_cfg.get("zone_id")
        cinv = CloudflareInventory(account, zone, cf_cfg.get("zero_trust_plan"), cf_cfg.get("zero_trust_since"))
        if account:
            cinv.collect()
        else:
            cinv._zero_trust()
            cinv.not_connected.append(
                {
                    "provider": "cloudflare",
                    "what": "Cloudflare API(LB 구독 · 끝점)",
                    "reason": "CLOUDFLARE_ACCOUNT_ID가 없음",
                }
            )
        lines += cinv.lines
        not_connected += cinv.not_connected
        sources.append(
            {
                "name": "Cloudflare API (load_balancers, pools, subscriptions)",
                "status": "partial" if cinv.not_connected else "ok",
            }
        )

    from .llm import subscription_lines, usage_lines

    llm_cfg = cfg.get("llm") or {}
    lines += subscription_lines(list(llm_cfg.get("subscriptions") or []))
    runs, run_src = usage_lines(os.environ.get(USAGE_DIR_ENV, "").strip() or llm_cfg.get("usage_dir"))
    lines += runs
    if run_src["status"] != "ok":
        not_connected.append({"provider": "llm", "what": "에이전트 실행별 토큰 사용량", "reason": run_src["reason"]})
    sources.append({"name": "에이전트 실행 기록(Harbor result.json)", **run_src})
    return {
        "lines": lines,
        "not_connected": not_connected,
        "notes": notes,
        "sources": sources,
        "observed_at": now_utc(),
    }


def _reason(exc: Exception) -> str:
    from ..redact import redact

    return redact(f"{type(exc).__name__}: {exc}")[:300]


# --- pure assembly ---------------------------------------------------------------------------------------


def _r(v: float | None, n: int = 6) -> float | None:
    return None if v is None else round(v, n)


def _line_json(line: Line, w0: datetime, w1: datetime, phases: dict[str, tuple[datetime, datetime]] | None) -> dict:
    q, st = line.quantity(w0, w1), line.subtotal(w0, w1)
    running = {k: v for k, v in (line.running or {}).items() if not k.startswith("_")} or None
    out = {
        "line_id": line.line_id,
        "provider": line.provider,
        "service": line.service,
        "resource_type": line.resource_type,
        "resource_id": line.resource_id,
        "resource": line.resource,
        "environment": line.environment,
        "component": line.component,
        "component_source": line.component_source,
        "unit_price": line.unit_price,
        "unit": line.unit,
        "price_source": line.price_source,
        "quantity": _r(q),
        "quantity_unit": line.quantity_unit,
        "subtotal": _r(st),
        "basis": BASIS[line.basis_code],
        "basis_code": line.basis_code,
        "in_total": line.in_total,
        "running": running,
        "status": line.status,
        "reason": line.reason,
        "note": line.note,
        "plan_id": line.plan_id,
        "detail": line.detail,
    }
    if phases:
        out["phases"] = {
            ph: {"quantity": _r(line.quantity(a, b)), "subtotal": _r(line.subtotal(a, b))}
            for ph, (a, b) in phases.items()
        }
    return out


def _sum(values: list[float | None]) -> float:
    return round(sum(v for v in values if v is not None), 6)


def build_receipt(
    lines: list[Line],
    *,
    start: datetime,
    end: datetime,
    plan: dict[str, Any] | None = None,
    actuals: dict[str, Any] | None = None,
    not_connected: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
    sources: list[dict[str, Any]] | None = None,
    observed_at: datetime | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    if end <= start:
        raise CostRequestError("end must be after start")
    generated_at = generated_at or now_utc()
    plan_id = (plan or {}).get("plan_id")
    if plan_id:
        lines = [x for x in lines if x.plan_id in (None, plan_id)]  # runs of other plans are not this plan's cost

    phases: dict[str, tuple[datetime, datetime]] | None = None
    if plan and plan.get("created_at"):
        created = max(start, min(end, plan["created_at"]))
        done = plan.get("done_at")
        done = max(created, min(end, done)) if done else end
        phases = {"before": (start, created), "during": (created, done), "after": (done, end)}

    billed_active = any(x.basis_code == "billed" for x in lines)
    if billed_active:  # tag actuals replace the per-resource AWS estimates in the total (no double count)
        for x in lines:
            if x.provider == "aws" and x.basis_code == "estimated":
                x.in_total = False

    rows = [_line_json(x, start, end, phases) for x in lines]
    rows = [
        r
        for r in rows
        if not (r["quantity"] == 0 and r["status"] == "ok" and r["subtotal"] in (0, None) and r["basis_code"] != "flat")
    ]
    keep = {r["line_id"] for r in rows}
    used = [x for x in lines if x.line_id in keep]
    counted = [x for x in used if x.in_total]

    def total_of(sel: list[Line], a: datetime, b: datetime) -> float:
        return _sum([x.subtotal(a, b) for x in sel])

    by_component: dict[str, float] = {}
    by_env: dict[str, float] = {}
    for x in counted:
        v = x.subtotal(start, end)
        if v is None:
            continue
        by_component[x.component or "(태그 없음)"] = round(by_component.get(x.component or "(태그 없음)", 0) + v, 6)
        by_env[x.environment or "(미상)"] = round(by_env.get(x.environment or "(미상)", 0) + v, 6)

    totals = {
        "total": total_of(counted, start, end),
        "estimated": total_of([x for x in counted if x.basis_code == "estimated"], start, end),
        "billed": total_of([x for x in counted if x.basis_code == "billed"], start, end) if billed_active else None,
        "flat": total_of([x for x in counted if x.basis_code == "flat"], start, end),
        "api_equivalent_not_billed": total_of([x for x in used if x.basis_code == "api_equivalent"], start, end),
        "unpriced_lines": sum(1 for x in counted if x.subtotal(start, end) is None),
        "by_component": dict(sorted(by_component.items(), key=lambda kv: -kv[1])),
        "by_environment": dict(sorted(by_env.items(), key=lambda kv: -kv[1])),
        "migration_extra": None,
        "note": "total = 청구 + 추정 + 정액(금액이 있는 것). API 가격 환산은 넣지 않음. unpriced_lines가 0보다 크면 "
        "금액을 모르는 줄이 있어 합계는 하한",
    }

    summary = None
    migration_extra = None
    if phases:
        summary = {}
        for ph, (a, b) in phases.items():
            hours = max(0.0, (b - a).total_seconds() / 3600)
            t = total_of(counted, a, b)
            comp: dict[str, float] = {}
            for x in counted:
                v = x.subtotal(a, b)
                if v:
                    comp[x.component or "(태그 없음)"] = round(comp.get(x.component or "(태그 없음)", 0) + v, 6)
            summary[ph] = {
                "label": PHASE_KO[ph],
                "start": iso(a),
                "end": iso(b),
                "hours": round(hours, 3),
                "total": t,
                "daily": round(t / hours * 24, 6) if hours else None,
                "by_component": comp,
            }
        during = summary["during"]
        src = during["by_component"].get("source", 0.0)
        extra = round(during["total"] - src, 6)
        migration_extra = {
            "value": extra,
            "formula": "이전 중 전체 − 이전 중 출발지(component=source, 원래 내던 비용)",
            "inputs": {
                "during_total": during["total"],
                "during_source": round(src, 6),
                "during_hours": during["hours"],
            },
            "explain": f"{during['total']:.4f} − {src:.4f} = {extra:.4f} USD",
            "lower_bound": totals["unpriced_lines"] > 0,
        }
        totals["migration_extra"] = extra

    # Daily series in KST/JST calendar days (UTC+9).
    daily = []
    d0 = (start + DAY_TZ).replace(hour=0, minute=0, second=0, microsecond=0) - DAY_TZ
    while d0 < end:
        d1 = d0 + timedelta(days=1)
        a, b = max(d0, start), min(d1, end)
        comp: dict[str, float] = {}
        for x in counted:
            v = x.subtotal(a, b)
            if v:
                comp[x.component or "(태그 없음)"] = round(comp.get(x.component or "(태그 없음)", 0) + v, 6)
        daily.append(
            {"date": (d0 + DAY_TZ).date().isoformat(), "by_component": comp, "total": _sum(list(comp.values()))}
        )
        d0 = d1

    price_times = sorted(
        r["price_source"]["fetched_at"] for r in rows if r.get("price_source") and r["price_source"].get("fetched_at")
    )
    tag = (actuals or {}).get("tag") or {
        "key": "component",
        "label": TAG_PENDING,
        "active": False,
        "reason": "Cost Explorer를 읽지 못함",
    }
    nc = list(not_connected or [])
    if not tag.get("active"):
        nc.append(
            {
                "provider": "aws",
                "what": "component 태그별 청구(실제 금액)",
                "reason": f"{TAG_PENDING}: {tag.get('reason')}",
            }
        )
    for r in rows:
        if r["status"] != "ok":
            nc.append({"provider": r["provider"], "what": r["resource"], "reason": r["reason"]})
    return {
        "schema_version": SCHEMA,
        "generated_at": iso(generated_at),
        "inventory_observed_at": iso(observed_at),
        "currency": "USD",
        "period": {
            "start": iso(start),
            "end": iso(end),
            "hours": round((end - start).total_seconds() / 3600, 3),
            "days": round((end - start).total_seconds() / 86400, 4),
        },
        "day_timezone": "+09:00",
        "plan": (
            {
                **{k: v for k, v in plan.items() if k not in ("created_at", "done_at")},
                "created_at": iso(plan.get("created_at")),
                "done_at": iso(plan.get("done_at")),
            }
            if plan
            else None
        ),
        "phases": ({ph: {"start": iso(a), "end": iso(b)} for ph, (a, b) in phases.items()} if phases else None),
        "summary": summary,
        "migration_extra": migration_extra,
        "totals": totals,
        "tag": tag,
        "lines": rows,
        "daily": daily,
        "actuals": {k: v for k, v in (actuals or {}).items() if k != "by_component_daily"} or None,
        "price_list_at": price_times[0] if price_times else None,
        "bill_received_at": (actuals or {}).get("cached_at") if (actuals or {}).get("status") == "ok" else None,
        "sources": list(sources or []),
        "not_connected": nc,
        "not_connected_label": NOT_CONNECTED,
        "notes": list(notes or [])
        + [
            "추정 = 정가(가격 API) × 자원 자체 시각(생성 · 시작 · 종료)으로 본 실행 시간. 지금 Describe에 보이지 않는 "
            "삭제된 자원은 추정에서 빠지고, component 태그가 활성화되면 청구 줄이 그 몫을 채움",
        ],
    }


# --- request entry point ---------------------------------------------------------------------------------


def receipt_for_request(
    *,
    start: str | None = None,
    end: str | None = None,
    plan_id: str | None = None,
    plan_created_at: str | None = None,
    plan_done_at: str | None = None,
    plan_state: str | None = None,
    workload: str | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    now = now_utc()
    try:
        t_end = min(parse_ts(end) or now, now)
        t_start = parse_ts(start) or now.replace(day=1, hour=0, minute=0, second=0)
        created = parse_ts(plan_created_at)
        done = parse_ts(plan_done_at)
    except ValueError as exc:
        raise CostRequestError(f"bad timestamp: {exc}") from exc
    if created and (start is None or created < t_start):
        t_start = created - timedelta(hours=24)  # a plan's receipt keeps one day of "before" for the comparison
    if done and created and done < created:
        raise CostRequestError("plan_done_at is before plan_created_at")
    if t_end <= t_start:
        raise CostRequestError("end must be after start")
    if (t_end - t_start).days > 62:
        raise CostRequestError("period longer than 62 days")

    cfg = load_config(config_path)
    inv, inv_at, _ = CACHE.get_or_fill("cost-inventory", INVENTORY_TTL_S, lambda: collect_inventory(cfg))
    actuals = None
    if (cfg.get("aws") or {}).get("regions"):
        from .actuals import billed_lines, cached_actuals

        actuals = cached_actuals()
        attempts = (cfg.get("aws") or {}).get("cost_tag_activation_attempts")
        if attempts and actuals.get("tag"):
            actuals["tag"] = {**actuals["tag"], "activation_attempts": attempts}
        lines = list(inv["lines"]) + billed_lines(actuals)
    else:
        lines = list(inv["lines"])
    plan = None
    if plan_id or created:
        plan = {"plan_id": plan_id, "state": plan_state, "workload": workload, "created_at": created, "done_at": done}
    sources = list(inv["sources"]) + [
        {"name": "AWS Pricing API / GCP Cloud Billing Catalog API (가격, 24시간 캐시)", "status": "ok"},
        {
            "name": "AWS Cost Explorer (1시간 캐시, 요청당 USD 0.01)",
            "status": (actuals or {}).get("status", "not_connected"),
        },
    ]
    # Lines are shared with the cache: build on copies so the total switch never leaks between requests.
    import copy

    return build_receipt(
        copy.deepcopy(lines),
        start=t_start,
        end=t_end,
        plan=plan,
        actuals=actuals,
        not_connected=inv["not_connected"],
        notes=inv["notes"],
        sources=sources,
        observed_at=inv["observed_at"],
    )
