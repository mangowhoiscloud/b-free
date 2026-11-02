"""Front switch for the representative name (T17, contract section 6, D-16).

``b-free-chat.mangowhois.ai`` (customer demo app; ``b-free`` is the platform and is refused)
reaches one environment through Cloudflare. Infra (``infra/modules/edge/
cloudflare``) creates one tunnel per environment (``bravo-<app>-<env>``) and either

* CNAME mode (``lb_enabled = false``, now): a proxied CNAME ``<app>.<zone>`` -> ``<tunnel id>.cfargotunnel.com``
  of the active environment. A shift is a single flip of that record. Partial weights do not exist
  in this mode: the 10 % and 50 % steps are recorded as not applied with reason "LB 구독 필요".
  Propagation time = API call -> first probe of ``consecutive`` probes in a row that return the target
  environment id from ``GET <front>/api/config`` (``env.id``: ``aws`` on the source, ``onprem`` on the
  destination, apps/demo).
* LB mode (``lb_enabled = true``): a load balancer of the same name with one pool per environment and
  random steering. A shift walks pool weights 10 -> 50 -> 100 % to the target. Each step holds for
  ``hold_s`` while a metrics source collects samples; an error rate or p95 above the thresholds, or
  fewer samples than ``min_samples``, reverts the target to 0 % at once.
  NOT EXERCISED: the account has no Load Balancing subscription (``infra/envs/edge`` ``lb_enabled =
  false``), so this path has never run against Cloudflare. It refuses to start unless the LB object
  exists (it never falls back to pretending).

Every external change is described as (resource ID, action, call, observe) so the caller can wrap it
in the effect ledger (``stages.EffectRunner``). The Cloudflare token comes from the platform OpenBao
(``secret/platform/cloudflare``) through ``vault.SecretBroker`` for the life of one run.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .vault import SecretBroker

CF_API = "https://api.cloudflare.com/client/v4"
CF_SECRET_PATH = "platform/cloudflare"  # noqa: S105 - OpenBao path, not a secret
LB_REQUIRED = "LB 구독 필요"
# CLOUD-E2E-CONTRACT.md 6: b-free.<zone> is the platform itself (console, API, MCP). The Executor
# switches only the customer app front (b-free-chat). Names here are never flipped, whatever the env says.
PROTECTED_APPS = frozenset({"b-free"})

# ---- configuration ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrontConfig:
    app: str                                   # "b-free-chat" -> b-free-chat.<zone>, tunnels bravo-b-free-chat-<env>
    env_ids: dict[str, str]                    # environment key -> app env id served at /api/config
    lb_enabled: bool
    probe_path: str = "/api/config"
    probe_interval_s: float = 0.5
    probe_timeout_s: float = 5.0
    consecutive: int = 5                       # probes in a row that must show the target
    propagation_timeout_s: float = 180.0
    hold_s: float = 60.0                       # LB mode: observation window per step
    min_samples: int = 30                      # LB mode: below this the step is not judged as healthy
    max_error_rate: float = 0.01
    max_p95_ms: float = 800.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FrontConfig:
        e = os.environ if env is None else env
        app = (e.get("BRAVO_FRONT_APP") or "").strip()
        ids_raw = (e.get("BRAVO_FRONT_ENV_IDS") or "").strip()
        if not app or not ids_raw:
            raise ValueError("BRAVO_FRONT_APP and BRAVO_FRONT_ENV_IDS (e.g. src=aws,dst=onprem) are required")
        if app in PROTECTED_APPS:
            raise ValueError(f"refusing to switch {app!r}: it is the platform front, not a customer app")
        env_ids = dict(part.split("=", 1) for part in ids_raw.split(",") if "=" in part)

        def f(name: str, default: float) -> float:
            raw = e.get(name)
            return float(raw) if raw not in (None, "") else default

        return cls(
            app=app,
            env_ids={k.strip(): v.strip() for k, v in env_ids.items()},
            lb_enabled=(e.get("BRAVO_FRONT_LB_ENABLED") or "0").strip() in ("1", "true"),
            probe_interval_s=f("BRAVO_FRONT_PROBE_INTERVAL_S", 0.5),
            consecutive=int(f("BRAVO_FRONT_CONSECUTIVE", 5)),
            propagation_timeout_s=f("BRAVO_FRONT_PROPAGATION_TIMEOUT_S", 180.0),
            hold_s=f("BRAVO_FRONT_HOLD_S", 60.0),
            min_samples=int(f("BRAVO_FRONT_MIN_SAMPLES", 30)),
            max_error_rate=f("BRAVO_FRONT_MAX_ERROR_RATE", 0.01),
            max_p95_ms=f("BRAVO_FRONT_MAX_P95_MS", 800.0),
        )

    def tunnel_name(self, env_key: str) -> str:
        return f"bravo-{self.app}-{env_key}"

    def other_env(self, env_key: str) -> str:
        others = [k for k in self.env_ids if k != env_key]
        if len(others) != 1:
            raise ValueError(f"expected exactly two environments, got {sorted(self.env_ids)}")
        return others[0]


# ---- pure planning and judgement -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedStep:
    weight: int                 # percent of traffic to the target environment
    applied: bool               # False: recorded only (CNAME mode partial weights)
    reason: str | None = None


def plan_steps(lb_enabled: bool, steps: Sequence[int] = (10, 50, 100)) -> list[PlannedStep]:
    """Weights to walk. In CNAME mode only 100 (and 0) exist; others are recorded as not applied."""
    clean = [int(w) for w in steps]
    if not clean or any(w < 0 or w > 100 for w in clean) or clean != sorted(set(clean)):
        raise ValueError("steps must be strictly increasing percentages within 0..100")
    if lb_enabled:
        return [PlannedStep(w, True) for w in clean]
    return [PlannedStep(w, True) if w in (0, 100) else PlannedStep(w, False, LB_REQUIRED) for w in clean]


@dataclass(frozen=True, slots=True)
class Sample:
    t: float                    # seconds since the change was sent (monotonic)
    ok: bool                    # HTTP 2xx and a parseable body
    latency_ms: float
    env_id: str | None = None
    status: int | None = None


def p95(values: Sequence[float]) -> float | None:
    """Nearest-rank 95th percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(0.95 * len(ordered))) - 1]


@dataclass(frozen=True, slots=True)
class Judgement:
    healthy: bool
    samples: int
    error_rate: float | None
    p95_ms: float | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"healthy": self.healthy, "samples": self.samples, "error_rate": self.error_rate,
                "p95_ms": self.p95_ms, "reasons": list(self.reasons)}


def judge(samples: Sequence[Sample], cfg: FrontConfig) -> Judgement:
    """A step is healthy only with enough samples, error rate <= max and p95 <= max."""
    n = len(samples)
    reasons: list[str] = []
    if n < cfg.min_samples:
        reasons.append(f"INSUFFICIENT_SAMPLES({n}<{cfg.min_samples})")
    errors = sum(1 for s in samples if not s.ok)
    rate = None if n == 0 else errors / n
    lat = p95([s.latency_ms for s in samples if s.ok])
    if rate is not None and rate > cfg.max_error_rate:
        reasons.append(f"ERROR_RATE({rate:.4f}>{cfg.max_error_rate})")
    if lat is not None and lat > cfg.max_p95_ms:
        reasons.append(f"P95({lat:.1f}ms>{cfg.max_p95_ms}ms)")
    return Judgement(not reasons, n, rate, lat, tuple(reasons))


@dataclass(frozen=True, slots=True)
class Propagation:
    converged: bool
    propagation_ms: float | None    # change sent -> first probe of the final run of `consecutive` hits
    probes: int
    first_target_ms: float | None   # first single probe that already showed the target

    def as_dict(self) -> dict[str, Any]:
        return {"converged": self.converged, "propagation_ms": self.propagation_ms, "probes": self.probes,
                "first_target_ms": self.first_target_ms}


def measure_propagation(samples: Sequence[Sample], target_env_id: str, consecutive: int) -> Propagation:
    """From probes ordered by time: when did the front start answering from the target for good."""
    run_start: float | None = None
    run_len = 0
    first: float | None = None
    for s in samples:
        hit = s.ok and s.env_id == target_env_id
        if hit:
            if first is None:
                first = s.t
            if run_len == 0:
                run_start = s.t
            run_len += 1
            if run_len >= consecutive:
                return Propagation(True, round(run_start * 1000, 1) if run_start is not None else None,
                                   len(samples), round(first * 1000, 1))
        else:
            run_len = 0
            run_start = None
    return Propagation(False, None, len(samples), None if first is None else round(first * 1000, 1))


def lb_pool_weights(target_pool: str, other_pool: str, weight_pct: int) -> dict[str, float]:
    """Random-steering pool weights for `weight_pct` % to the target."""
    w = round(weight_pct / 100.0, 2)
    return {target_pool: w, other_pool: round(1.0 - w, 2)}


# ---- probing -----------------------------------------------------------------------------------


class MetricsSource(Protocol):
    """Samples of user-facing requests during a step (LB-mode judgement)."""

    async def collect(self, seconds: float) -> list[Sample]: ...


class HttpProbe:
    """Real HTTPS requests to the front name; the body's env.id tells which environment answered."""

    def __init__(self, url: str, timeout_s: float, interval_s: float) -> None:
        self.url = url
        self.interval_s = interval_s
        self._http = httpx.AsyncClient(timeout=timeout_s, headers={"Cache-Control": "no-cache"})

    async def close(self) -> None:
        await self._http.aclose()

    async def once(self, t0: float) -> Sample:
        start = time.monotonic()
        try:
            resp = await self._http.get(self.url, params={"_probe": f"{start:.6f}"})
            latency = (time.monotonic() - start) * 1000
            env_id = None
            if resp.status_code == 200:
                try:
                    env_id = ((resp.json() or {}).get("env") or {}).get("id")
                except ValueError:
                    env_id = None
            return Sample(start - t0, resp.status_code == 200 and env_id is not None, latency, env_id,
                          resp.status_code)
        except httpx.HTTPError:
            return Sample(start - t0, False, (time.monotonic() - start) * 1000, None, None)

    async def until_converged(self, t0: float, target_env_id: str, consecutive: int, timeout_s: float) -> tuple[
        Propagation, list[Sample]
    ]:
        samples: list[Sample] = []
        while time.monotonic() - t0 < timeout_s:
            samples.append(await self.once(t0))
            result = measure_propagation(samples, target_env_id, consecutive)
            if result.converged:
                return result, samples
            await asyncio.sleep(self.interval_s)
        return measure_propagation(samples, target_env_id, consecutive), samples

    async def collect(self, seconds: float) -> list[Sample]:
        t0 = time.monotonic()
        out: list[Sample] = []
        while time.monotonic() - t0 < seconds:
            out.append(await self.once(t0))
            await asyncio.sleep(self.interval_s)
        return out


# ---- Cloudflare API ----------------------------------------------------------------------------


class CloudflareError(RuntimeError):
    """A Cloudflare API call failed. Carries the path and Cloudflare error codes, never the token."""


class Cloudflare:
    def __init__(self, token: str, account_id: str, zone_id: str, timeout_s: float = 20.0) -> None:
        self.account_id = account_id
        self.zone_id = zone_id
        self._http = httpx.AsyncClient(
            base_url=CF_API, timeout=timeout_s, headers={"Authorization": f"Bearer {token}"}
        )

    def __repr__(self) -> str:
        return f"Cloudflare(account={self.account_id}, zone={self.zone_id})"

    @classmethod
    async def from_broker(cls, broker: SecretBroker) -> Cloudflare:
        token = await broker.field(CF_SECRET_PATH, "api_token")
        account = await broker.field(CF_SECRET_PATH, "account_id")
        zone = await broker.field(CF_SECRET_PATH, "zone_id")
        return cls(token, account, zone)

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise CloudflareError(f"{method} {path}: {type(exc).__name__}") from None
        try:
            body = resp.json()
        except ValueError:
            raise CloudflareError(f"{method} {path}: HTTP {resp.status_code}, non-JSON body") from None
        if not body.get("success"):
            codes = [{"code": e.get("code"), "message": e.get("message")} for e in body.get("errors") or []]
            raise CloudflareError(f"{method} {path}: HTTP {resp.status_code} {codes}")
        return body.get("result")

    async def zone_name(self) -> str:
        return str((await self._call("GET", f"/zones/{self.zone_id}"))["name"])

    async def tunnel(self, name: str) -> dict[str, Any] | None:
        result = await self._call(
            "GET", f"/accounts/{self.account_id}/cfd_tunnel", params={"name": name, "is_deleted": "false"}
        )
        items = [t for t in result or [] if t.get("name") == name]
        if len(items) > 1:
            raise CloudflareError(f"more than one live tunnel named {name}")
        if not items:
            return None
        t = items[0]
        return {"id": t["id"], "name": t["name"], "status": t.get("status"),
                "connections": len(t.get("connections") or [])}

    async def dns_record(self, name: str, rtype: str = "CNAME") -> dict[str, Any] | None:
        result = await self._call("GET", f"/zones/{self.zone_id}/dns_records", params={"type": rtype, "name": name})
        items = list(result or [])
        if len(items) > 1:
            raise CloudflareError(f"more than one {rtype} record named {name}")
        if not items:
            return None
        r = items[0]
        return {"id": r["id"], "name": r["name"], "content": r["content"], "proxied": r.get("proxied"),
                "modified_on": r.get("modified_on")}

    async def set_dns_content(self, record_id: str, content: str) -> dict[str, Any]:
        r = await self._call("PATCH", f"/zones/{self.zone_id}/dns_records/{record_id}", json={"content": content})
        return {"id": r["id"], "content": r["content"], "modified_on": r.get("modified_on")}

    async def load_balancer(self, name: str) -> dict[str, Any] | None:
        result = await self._call("GET", f"/zones/{self.zone_id}/load_balancers")
        items = [lb for lb in result or [] if lb.get("name") == name]
        if not items:
            return None
        lb = items[0]
        return {"id": lb["id"], "name": lb["name"], "random_steering": lb.get("random_steering") or {}}

    async def pools(self) -> dict[str, str]:
        result = await self._call("GET", f"/accounts/{self.account_id}/load_balancers/pools")
        return {p["name"]: p["id"] for p in result or []}

    async def set_pool_weights(self, lb_id: str, weights: dict[str, float]) -> dict[str, Any]:
        body = {"random_steering": {"default_weight": 0, "pool_weights": weights}}
        lb = await self._call("PATCH", f"/zones/{self.zone_id}/load_balancers/{lb_id}", json=body)
        return {"id": lb["id"], "pool_weights": (lb.get("random_steering") or {}).get("pool_weights")}


# ---- switch ------------------------------------------------------------------------------------

Observation = tuple[bool, dict[str, Any]]
EffectApply = Callable[
    [str, str, Callable[[], Awaitable[dict[str, Any] | None]], Callable[[], Awaitable[Observation]]],
    Awaitable[dict[str, Any]],
]


@dataclass(slots=True)
class FrontState:
    mode: str                               # "cname" | "lb" | "unconfigured"
    front: str
    active_env: str | None
    detail: dict[str, Any] = field(default_factory=dict)


class FrontSwitch:
    def __init__(self, cfg: FrontConfig, cf: Cloudflare, zone_name: str) -> None:
        self.cfg = cfg
        self.cf = cf
        self.zone = zone_name
        self.front = f"{cfg.app}.{zone_name}"

    @classmethod
    async def open(cls, cfg: FrontConfig, broker: SecretBroker) -> FrontSwitch:
        cf = await Cloudflare.from_broker(broker)
        return cls(cfg, cf, await cf.zone_name())

    async def close(self) -> None:
        await self.cf.close()

    def resource_dns(self) -> str:
        return f"edge/cloudflare/dns:{self.front}"

    def resource_lb(self) -> str:
        return f"edge/cloudflare/lb:{self.front}"

    async def tunnels(self) -> dict[str, dict[str, Any] | None]:
        return {env: await self.cf.tunnel(self.cfg.tunnel_name(env)) for env in self.cfg.env_ids}

    async def state(self) -> FrontState:
        tunnels = await self.tunnels()
        by_target = {f"{t['id']}.cfargotunnel.com": env for env, t in tunnels.items() if t}
        detail: dict[str, Any] = {"tunnels": tunnels, "lb_enabled_config": self.cfg.lb_enabled}
        if self.cfg.lb_enabled:
            lb = await self.cf.load_balancer(self.front)
            if lb is None:
                return FrontState("unconfigured", self.front, None, {**detail, "reason": "LB object not found"})
            pools = await self.cf.pools()
            names = {v: k for k, v in pools.items()}
            weights = {names.get(pid, pid): w for pid, w in (lb["random_steering"].get("pool_weights") or {}).items()}
            detail["pool_weights"] = weights
            active = max(weights, key=lambda k: weights[k]) if weights else None
            env = None if active is None else active.removeprefix(f"bravo-{self.cfg.app}-")
            return FrontState("lb", self.front, env, detail)
        rec = await self.cf.dns_record(self.front)
        if rec is None:
            return FrontState("unconfigured", self.front, None, {**detail, "reason": "front CNAME not found"})
        detail["record"] = rec
        return FrontState("cname", self.front, by_target.get(rec["content"]), detail)

    async def shift(
        self, target_env: str, steps: Sequence[int], apply: EffectApply, probe: HttpProbe,
        record_step: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> dict[str, Any]:
        """Walk the plan. `apply` wraps each external change in the effect ledger; `record_step` stores
        a stage row (name, detail). Returns the summary; raises nothing for a judged revert."""
        if target_env not in self.cfg.env_ids:
            raise ValueError(f"unknown environment {target_env!r}")
        target_id = self.cfg.env_ids[target_env]
        tunnels = await self.tunnels()
        tunnel = tunnels.get(target_env)
        if tunnel is None:
            raise CloudflareError(f"tunnel {self.cfg.tunnel_name(target_env)} not found")
        if not tunnel["connections"]:
            # Sending users to a tunnel with no connector would answer 1033 errors: refuse, record why.
            raise CloudflareError(f"tunnel {tunnel['name']} has no active connection (status {tunnel['status']})")
        summary: dict[str, Any] = {"front": self.front, "target_env": target_env, "steps": [],
                                   "target_tunnel": tunnel}
        if not self.cfg.lb_enabled:
            summary["mode"] = "cname"
            rec = await self.cf.dns_record(self.front)
            if rec is None:
                raise CloudflareError(f"front CNAME {self.front} not found")
            new_content = f"{tunnel['id']}.cfargotunnel.com"
            for step in plan_steps(False, steps):
                if not step.applied:
                    entry = {"weight": step.weight, "applied": False, "reason": step.reason}
                    summary["steps"].append(entry)
                    await record_step(f"front.step_{step.weight}", entry)
                    continue
                if step.weight != 100:
                    raise ValueError("CNAME mode can only move all traffic (100)")
                result: dict[str, Any] = {}

                async def flip(result: dict[str, Any] = result) -> dict[str, Any]:
                    result["t0"] = time.monotonic()
                    changed = await self.cf.set_dns_content(rec["id"], new_content)
                    return {"from": rec["content"], "to": changed["content"], "modified_on": changed["modified_on"],
                            "tunnel_connections": tunnel["connections"]}

                async def observe_flip(result: dict[str, Any] = result) -> Observation:
                    prop, samples = await probe.until_converged(
                        result["t0"], target_id, self.cfg.consecutive, self.cfg.propagation_timeout_s
                    )
                    result["prop"] = prop
                    tail = [{"t_ms": round(s.t * 1000, 1), "status": s.status, "env": s.env_id} for s in samples[-8:]]
                    return prop.converged, {"propagation": prop.as_dict(), "last_probes": tail}

                evidence = await apply(self.resource_dns(), f"CNAME_TO_{target_env.upper()}", flip, observe_flip)
                entry = {"weight": 100, "applied": True, **evidence}
                summary["steps"].append(entry)
                await record_step("front.step_100", entry)
            summary["propagation_ms"] = (summary["steps"][-1].get("observed") or {}).get(
                "propagation", {}).get("propagation_ms")
            return summary

        # LB mode (not exercised: no LB subscription; see module docstring)
        summary["mode"] = "lb"
        lb = await self.cf.load_balancer(self.front)
        if lb is None:
            raise CloudflareError(f"load balancer {self.front} not found (lb_enabled=true but no LB object)")
        pools = await self.cf.pools()
        other = self.cfg.other_env(target_env)
        tp, op = pools.get(self.cfg.tunnel_name(target_env)), pools.get(self.cfg.tunnel_name(other))
        if not tp or not op:
            raise CloudflareError("LB pools for both environments are required")
        for step in plan_steps(True, steps):
            weights = lb_pool_weights(tp, op, step.weight)

            async def set_weights(weights: dict[str, float] = weights) -> dict[str, Any]:
                return await self.cf.set_pool_weights(lb["id"], weights)

            async def observe_weights(weights: dict[str, float] = weights) -> Observation:
                now = await self.cf.load_balancer(self.front)
                got = (now or {}).get("random_steering", {}).get("pool_weights")
                return got == weights, {"pool_weights": got}

            evidence = await apply(
                self.resource_lb(), f"WEIGHT_{target_env.upper()}_{step.weight}", set_weights, observe_weights
            )
            samples = await probe.collect(self.cfg.hold_s)
            verdict = judge(samples, self.cfg)
            entry = {"weight": step.weight, "applied": True, "judgement": verdict.as_dict(), **evidence}
            summary["steps"].append(entry)
            await record_step(f"front.step_{step.weight}", entry)
            if not verdict.healthy:
                revert = lb_pool_weights(tp, op, 0)

                async def call_revert(revert: dict[str, float] = revert) -> dict[str, Any]:
                    return await self.cf.set_pool_weights(lb["id"], revert)

                async def observe_revert(revert: dict[str, float] = revert) -> Observation:
                    now = await self.cf.load_balancer(self.front)
                    got = (now or {}).get("random_steering", {}).get("pool_weights")
                    return got == revert, {"pool_weights": got}

                ev = await apply(self.resource_lb(), f"REVERT_{target_env.upper()}_0", call_revert, observe_revert)
                summary["reverted"] = {"at_weight": step.weight, "reasons": list(verdict.reasons), **ev}
                await record_step("front.revert_0", summary["reverted"])
                return summary
        return summary
