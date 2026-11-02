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
  random steering. A shift walks pool weights 10 -> 50 -> 100 % to the target. After each weight is confirmed,
  the Executor sends real chat writes through the front (``POST /api/rooms/<room>/messages``, contract 15
  §10.4) and judges the step from the writes the target answered (``X-Served-By``): at least
  ``min_target_samples`` of them (100: one error is 1.0 %, two are 2.0 %), error rate and p95 within the
  thresholds, and the target answering at least ``min_target_share`` of its planned share. The hold ends
  when the target has enough samples and ``min_hold_s`` has passed, or at ``max_hold_s`` (then
  INSUFFICIENT_SAMPLES). A failed step reverts the target to 0 % at once (S3 1a). Each step's judgement keeps
  ``by_env``: how many writes each environment answered, so the weights are shown to steer traffic.
  It refuses to start unless the LB object exists (it never falls back to pretending).

Every external change is described as (resource ID, action, call, observe) so the caller can wrap it
in the effect ledger (``stages.EffectRunner``). The Cloudflare token comes from the platform OpenBao
(``secret/platform/cloudflare``) through ``vault.SecretBroker`` for the life of one run.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from .loadmetrics import MIN_WINDOW_S, LoadgenMetrics, LoadWindow, MetricsUnavailable, window_counts
from .vault import SecretBroker

CF_API = "https://api.cloudflare.com/client/v4"
CF_SECRET_PATH = "platform/cloudflare"  # noqa: S105 - OpenBao path, not a secret
LB_REQUIRED = "LB 구독 필요"
# CLOUD-E2E-CONTRACT.md 6: b-free.<zone> is the platform itself (console, API, MCP). The Executor
# switches only the customer app front (b-free-chat). Names here are never flipped, whatever the env says.
PROTECTED_APPS = frozenset({"b-free"})
ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")  # apps/demo ROOM_RE
WRITE_AUTHOR = "bravo-shift"

# ---- configuration ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrontConfig:
    app: str                                   # "b-free-chat" -> b-free-chat.<zone>, tunnels bravo-b-free-chat-<env>
    env_ids: dict[str, str]                    # environment key -> app env id (/api/config env.id, X-Served-By)
    lb_enabled: bool
    probe_path: str = "/api/config"            # status and CNAME propagation only, never a step judgement
    probe_interval_s: float = 0.5
    probe_timeout_s: float = 5.0
    consecutive: int = 5                       # probes in a row that must show the target
    propagation_timeout_s: float = 180.0
    # LB-mode step judgement: real chat writes through the front (contract 15 §10.4)
    write_rate: float = 60.0                   # writes per second, open loop (ceiling of every step)
    # Writes per second the target should get at a step: the step rate is min(write_rate, this / weight), so the
    # 50 % and 100 % steps do not load the target harder than the 10 % step needs (2026-11-03: on-prem writes
    # cross the link, one round trip 340 ms, PgBouncer pool 10).
    target_write_rate: float = 20.0
    write_room: str = "shift-probe"
    # Longer than the app's own write deadline (apps/demo WRITE_WAIT_S, 8 s): a slow write then comes back as the
    # app's 503 with X-Served-By, so only edge and transport failures stay unattributed.
    write_timeout_s: float = 10.0
    write_max_in_flight: int = 128
    # A new connection per write. Measured 2026-11-03 03:35 KST at 50 % (docs/evidence/shift/lb-real-writes-*):
    # POSTs on kept-alive connections reached the target 9 of 150 times, POSTs on new connections 57 of 150, GETs
    # 60-62 of 150 either way. Latency is timed from sending the request to the response headers, so the sampler's
    # own TCP and TLS setup is not counted (it added about 400 ms at 60 per second).
    write_keepalive: bool = False
    min_target_samples: int = 100              # writes the target answered; below this the step is not judged
    min_hold_s: float = 5.0
    max_hold_s: float = 40.0
    max_error_rate: float = 0.01
    max_p95_ms: float = 800.0
    # The target must answer at least this fraction of its planned share (10 % step -> 2.5 % of writes).
    # Cloudflare fails a pool over to the other one when its monitor sees it down: writes then all succeed
    # from the old environment, and only this check notices that the target is not serving.
    min_target_share: float = 0.25
    move_hold_s: float = 5.0                   # failback front move: writes recorded after the move, no revert

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

        room = (e.get("BRAVO_FRONT_WRITE_ROOM") or "shift-probe").strip()
        if not ROOM_RE.match(room):
            raise ValueError(f"BRAVO_FRONT_WRITE_ROOM {room!r} is not a room name")
        cfg = cls(
            app=app,
            env_ids={k.strip(): v.strip() for k, v in env_ids.items()},
            lb_enabled=(e.get("BRAVO_FRONT_LB_ENABLED") or "0").strip() in ("1", "true"),
            probe_interval_s=f("BRAVO_FRONT_PROBE_INTERVAL_S", 0.5),
            consecutive=int(f("BRAVO_FRONT_CONSECUTIVE", 5)),
            propagation_timeout_s=f("BRAVO_FRONT_PROPAGATION_TIMEOUT_S", 180.0),
            write_rate=f("BRAVO_FRONT_WRITE_RATE", 60.0),
            target_write_rate=f("BRAVO_FRONT_TARGET_WRITE_RATE", 20.0),
            write_room=room,
            write_timeout_s=f("BRAVO_FRONT_WRITE_TIMEOUT_S", 10.0),
            write_max_in_flight=int(f("BRAVO_FRONT_WRITE_MAX_IN_FLIGHT", 128)),
            write_keepalive=(e.get("BRAVO_FRONT_WRITE_KEEPALIVE") or "0").strip() in ("1", "true"),
            min_target_samples=int(f("BRAVO_FRONT_MIN_TARGET_SAMPLES", 100)),
            min_hold_s=f("BRAVO_FRONT_MIN_HOLD_S", 5.0),
            max_hold_s=f("BRAVO_FRONT_MAX_HOLD_S", 40.0),
            max_error_rate=f("BRAVO_FRONT_MAX_ERROR_RATE", 0.01),
            max_p95_ms=f("BRAVO_FRONT_MAX_P95_MS", 800.0),
            min_target_share=f("BRAVO_FRONT_MIN_TARGET_SHARE", 0.25),
            move_hold_s=f("BRAVO_FRONT_MOVE_HOLD_S", 5.0),
        )
        if cfg.write_rate <= 0 or cfg.min_hold_s < 0 or cfg.max_hold_s < cfg.min_hold_s or cfg.min_target_samples < 1:
            raise ValueError("front judgement settings out of range (rate > 0, min_hold <= max_hold, samples >= 1)")
        return cfg

    def thresholds(self, revert: bool = True) -> dict[str, Any]:
        """The effective judgement settings, recorded on every front stage (contract 15 §10.4) so a screen
        draws the lines this run was judged against, not the documented defaults."""
        return {"min_target_samples": self.min_target_samples, "max_error_rate": self.max_error_rate,
                "max_p95_ms": self.max_p95_ms, "min_hold_s": self.min_hold_s,
                "max_hold_s": self.max_hold_s if revert else self.move_hold_s,
                "min_target_share": self.min_target_share, "write_rate": self.write_rate,
                "target_write_rate": self.target_write_rate, "write_keepalive": self.write_keepalive, "revert": revert}

    def step_rate(self, weight_pct: int) -> float:
        return min(self.write_rate, self.target_write_rate * 100 / max(weight_pct, 1))

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
    samples: int                     # every write of the window
    errors: int
    error_rate: float | None
    p95_ms: float | None
    target_samples: int              # writes the target answered, plus failures no environment answered
    target_errors: int
    target_error_rate: float | None
    target_p95_ms: float | None
    reasons: tuple[str, ...]
    by_env: Mapping[str, int] = field(default_factory=dict)  # which environment answered each write

    def as_dict(self) -> dict[str, Any]:
        return {"healthy": self.healthy, "samples": self.samples, "errors": self.errors,
                "error_rate": self.error_rate, "p95_ms": self.p95_ms, "target_samples": self.target_samples,
                "target_errors": self.target_errors, "target_error_rate": self.target_error_rate,
                "target_p95_ms": self.target_p95_ms, "reasons": list(self.reasons), "by_env": dict(self.by_env)}


def _rate(errors: int, n: int) -> float | None:
    return None if n == 0 else round(errors / n, 6)


def is_target(s: Sample, target_env_id: str) -> bool:
    """A write counts for the target when the target answered it, or when it failed before any environment
    answered (Cloudflare 52x, timeout, connection error): conservative, it may have been the target."""
    return s.env_id == target_env_id or (s.env_id is None and not s.ok)


def target_count(samples: Sequence[Sample], target_env_id: str) -> int:
    return sum(1 for s in samples if is_target(s, target_env_id))


def judge(samples: Sequence[Sample], cfg: FrontConfig, target_env_id: str, weight_pct: int) -> Judgement:
    """Contract 15 §10.4. A step is healthy only when the target answered at least ``min_target_samples``
    writes, the target's error rate <= max, the target's p95 <= max, and the target answered at least
    ``min_target_share`` of its planned share. The overall error rate and p95 are reported, not judged."""
    n = len(samples)
    errors = sum(1 for s in samples if not s.ok)
    tgt = [s for s in samples if is_target(s, target_env_id)]
    t_err = sum(1 for s in tgt if not s.ok)
    t_rate = _rate(t_err, len(tgt))
    t_p95 = p95([s.latency_ms for s in tgt if s.ok])
    reasons: list[str] = []
    if len(tgt) < cfg.min_target_samples:
        reasons.append(f"INSUFFICIENT_SAMPLES({target_env_id}:{len(tgt)}<{cfg.min_target_samples})")
    if t_rate is not None and t_rate > cfg.max_error_rate:
        reasons.append(f"TARGET_ERROR_RATE({t_rate:.4f}>{cfg.max_error_rate})")
    if t_p95 is not None and t_p95 > cfg.max_p95_ms:
        reasons.append(f"TARGET_P95({t_p95:.1f}ms>{cfg.max_p95_ms}ms)")
    by_env: dict[str, int] = {}
    for s in samples:
        key = s.env_id or "none"
        by_env[key] = by_env.get(key, 0) + 1
    if weight_pct > 0 and n:
        share, floor = by_env.get(target_env_id, 0) / n, cfg.min_target_share * weight_pct / 100
        if share < floor:
            reasons.append(f"TARGET_SHARE({target_env_id}:{share:.3f}<{floor:.3f})")
    lat = p95([s.latency_ms for s in samples if s.ok])
    return Judgement(not reasons, n, errors, _rate(errors, n), None if lat is None else round(lat, 1), len(tgt),
                     t_err, t_rate, None if t_p95 is None else round(t_p95, 1), tuple(reasons), by_env)


def judge_window(w: LoadWindow, target_p95_ms: float | None, cfg: FrontConfig, target_env_id: str,
                 weight_pct: int) -> Judgement:
    """The same rules as ``judge`` on the load generator's counts (contract 15 §10.4, source "loadgen")."""
    reasons: list[str] = []
    t_rate = _rate(w.target_errors, w.target_samples)
    if w.target_samples < cfg.min_target_samples:
        reasons.append(f"INSUFFICIENT_SAMPLES({target_env_id}:{w.target_samples}<{cfg.min_target_samples})")
    if t_rate is not None and t_rate > cfg.max_error_rate:
        reasons.append(f"TARGET_ERROR_RATE({t_rate:.4f}>{cfg.max_error_rate})")
    if target_p95_ms is not None and target_p95_ms > cfg.max_p95_ms:
        reasons.append(f"TARGET_P95({target_p95_ms:.1f}ms>{cfg.max_p95_ms}ms)")
    if weight_pct > 0 and w.writes:
        share, floor = w.by_env.get(target_env_id, 0) / w.writes, cfg.min_target_share * weight_pct / 100
        if share < floor:
            reasons.append(f"TARGET_SHARE({target_env_id}:{share:.3f}<{floor:.3f})")
    return Judgement(not reasons, w.writes, w.errors, _rate(w.errors, w.writes), None, w.target_samples,
                     w.target_errors, t_rate, target_p95_ms, tuple(reasons), dict(w.by_env))


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


class WriteSampler:
    """Real chat writes through the front (contract 15 §10.4): the step judgement's only input.

    ``POST https://<front>/api/rooms/<room>/messages`` with a new Idempotency-Key per write, sent open loop
    at ``rate`` per second (a slow answer does not slow the schedule). ACK = HTTP 200/201 with an integer
    ``message.id`` and an ``X-Served-By`` header. Anything else is an error and is not retried."""

    def __init__(self, front: str, cfg: FrontConfig, label: str) -> None:
        self.url = f"https://{front}/api/rooms/{cfg.write_room}/messages"
        self.cfg = cfg
        self.label = label
        self.sent = 0
        self._http = httpx.AsyncClient(
            timeout=cfg.write_timeout_s,
            limits=httpx.Limits(max_connections=cfg.write_max_in_flight,
                                max_keepalive_connections=cfg.write_max_in_flight if cfg.write_keepalive else 0),
            headers={"Cache-Control": "no-cache", "User-Agent": "bravo-executor-shift"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def once(self, t0: float) -> Sample:
        self.sent += 1
        body = {"author": WRITE_AUTHOR, "body": f"shift sample {self.label} {self.sent}"}
        start = time.monotonic()
        marks: dict[str, float] = {}

        async def trace(event: str, _info: dict[str, Any]) -> None:
            if event in ("http11.send_request_headers.started", "http11.receive_response_headers.complete"):
                marks[event] = time.monotonic()

        try:
            # A distinct query per write, so no edge layer can treat the writes as one repeated request.
            resp = await self._http.post(self.url, params={"_s": f"{self.label}-{self.sent}"}, json=body,
                                         headers={"Idempotency-Key": str(uuid.uuid4())},
                                         extensions={"trace": trace})
            sent_at = marks.get("http11.send_request_headers.started", start)
            latency = (marks.get("http11.receive_response_headers.complete", time.monotonic()) - sent_at) * 1000
            env_id = resp.headers.get("x-served-by") or None
            acked = False
            if resp.status_code in (200, 201):
                try:
                    msg = (resp.json() or {}).get("message") or {}
                    acked = isinstance(msg.get("id"), int) and not isinstance(msg.get("id"), bool)
                except (ValueError, AttributeError):
                    acked = False
            return Sample(start - t0, acked and env_id is not None, latency, env_id, resp.status_code)
        except Exception:  # noqa: BLE001 - any failure of one write is one error sample
            return Sample(start - t0, False, (time.monotonic() - start) * 1000, None, None)

    async def collect(self, done: Callable[[list[Sample], float], bool], max_s: float,
                      rate: float | None = None) -> list[Sample]:
        """Send until ``done(answered, elapsed_s)`` or ``max_s``; then wait for the writes already sent."""
        t0 = time.monotonic()
        interval = 1.0 / (rate or self.cfg.write_rate)
        out: list[Sample] = []
        pending: set[asyncio.Task[Sample]] = set()

        def finished(task: asyncio.Task[Sample]) -> None:
            pending.discard(task)
            if not task.cancelled():
                out.append(task.result())

        k = 0
        while True:
            elapsed = time.monotonic() - t0
            if elapsed >= max_s or done(out, elapsed):
                break
            delay = t0 + k * interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if len(pending) >= self.cfg.write_max_in_flight:
                await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            task = asyncio.create_task(self.once(t0))
            pending.add(task)
            task.add_done_callback(finished)
            k += 1
        if pending:
            await asyncio.wait(set(pending), timeout=self.cfg.write_timeout_s + 1.0)
        return sorted(out, key=lambda s: s.t)


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
RecordStep = Callable[[str, dict[str, Any], str], Awaitable[None]]
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

    async def _hold_on_load(self, lm: LoadgenMetrics, target_id: str, weight: int) -> tuple[Judgement, float]:
        """Hold until the user load gave the target enough writes (15 s at least: 5 s pushes), then judge."""
        need = self.cfg.min_target_samples
        start, t1 = time.monotonic(), time.time()
        c1 = await lm.counts(t1)
        while True:
            await asyncio.sleep(5.0)
            elapsed, now = time.monotonic() - start, time.time()
            w = window_counts(c1, await lm.counts(now), target_id)
            if (elapsed >= max(self.cfg.min_hold_s, MIN_WINDOW_S) and w.target_samples >= need) \
                    or elapsed >= self.cfg.max_hold_s:
                break
        return judge_window(w, await lm.write_p95(now, elapsed, target_id), self.cfg, target_id, weight), elapsed

    async def shift(
        self, target_env: str, steps: Sequence[int], apply: EffectApply, probe: HttpProbe,
        record_step: RecordStep, sampler: WriteSampler | None = None, *, revert: bool = True,
        loadgen: LoadgenMetrics | None = None,
    ) -> dict[str, Any]:
        """Walk the plan. `apply` wraps each external change in the effect ledger; `record_step` stores
        a stage row (name, detail, result). Returns the summary; raises nothing for a judged revert.

        LB mode needs `sampler` (real writes, contract 15 §10.4). ``revert=False`` is the failback front move
        (steps [100] to the source): the step is judged and recorded but never reverted, because the side
        traffic leaves is the faulty one."""
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
                    await record_step(f"front.step_{step.weight}", entry, "OK")
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
                await record_step("front.step_100", entry, "OK")
            summary["propagation_ms"] = (summary["steps"][-1].get("observed") or {}).get(
                "propagation", {}).get("propagation_ms")
            return summary

        # LB mode
        summary["mode"] = "lb"
        if sampler is None:
            raise ValueError("LB mode needs a write sampler (contract 15 §10.4)")
        lb = await self.cf.load_balancer(self.front)
        if lb is None:
            raise CloudflareError(f"load balancer {self.front} not found (lb_enabled=true but no LB object)")
        pools = await self.cf.pools()
        other = self.cfg.other_env(target_env)
        tp, op = pools.get(self.cfg.tunnel_name(target_env)), pools.get(self.cfg.tunnel_name(other))
        if not tp or not op:
            raise CloudflareError("LB pools for both environments are required")
        thresholds = self.cfg.thresholds(revert)
        summary["judge"] = {"input": f"POST {sampler.url}", **thresholds}
        need = self.cfg.min_target_samples
        for step in plan_steps(True, steps):
            weights = lb_pool_weights(tp, op, step.weight)

            async def set_weights(weights: dict[str, float] = weights) -> dict[str, Any]:
                return await self.cf.set_pool_weights(lb["id"], weights)

            async def observe_weights(weights: dict[str, float] = weights) -> Observation:
                now = await self.cf.load_balancer(self.front)
                got = (now or {}).get("random_steering", {}).get("pool_weights")
                return got == weights, {"pool_weights": got}

            t_step = time.monotonic()
            evidence = await apply(
                self.resource_lb(), f"WEIGHT_{target_env.upper()}_{step.weight}", set_weights, observe_weights
            )
            await record_step(f"front.set_{step.weight}",
                              {"weight": step.weight, "pool_weights": weights, "thresholds": thresholds,
                               "step_ms": int((time.monotonic() - t_step) * 1000), **evidence}, "OK")
            t_hold = time.monotonic()
            rate = self.cfg.step_rate(step.weight)
            # Source: the user load when a load run is active (contract 15 §10.4), else the write probe.
            load_run: dict[str, Any] | None = None
            if loadgen is not None and revert:
                try:
                    load_run = await loadgen.choose()
                except MetricsUnavailable as exc:
                    load_run = None
                    summary.setdefault("loadgen_errors", []).append(str(exc))
            if load_run is not None and loadgen is not None:
                try:
                    verdict, _ = await self._hold_on_load(loadgen, target_id, step.weight)
                except MetricsUnavailable as exc:  # judged as not healthy: the target is reverted
                    verdict = Judgement(False, 0, 0, None, None, 0, 0, None, None,
                                        (f"LOADGEN_METRICS_UNAVAILABLE({exc})",), {})
                source, rate = "loadgen", 0.0
            elif revert:
                samples = await sampler.collect(
                    lambda got, elapsed: elapsed >= self.cfg.min_hold_s and target_count(got, target_id) >= need,
                    self.cfg.max_hold_s, rate,
                )
                verdict, source = judge(samples, self.cfg, target_id, step.weight), "probe"
            else:
                samples = await sampler.collect(lambda _got, elapsed: False, self.cfg.move_hold_s, rate)
                verdict, source = judge(samples, self.cfg, target_id, step.weight), "probe"
            hold_ms = int((time.monotonic() - t_hold) * 1000)
            judgement = {**verdict.as_dict(), "source": source, "load_run": load_run, "hold_ms": hold_ms,
                         "write_rate": rate, "step_ms": int((time.monotonic() - t_step) * 1000)}
            entry = {"weight": step.weight, "applied": True, "judgement": judgement, "thresholds": thresholds,
                     "hold_ms": hold_ms, "step_ms": judgement["step_ms"]}
            summary["steps"].append(entry)
            await record_step(f"front.step_{step.weight}", entry, "OK" if verdict.healthy else "FAILED")
            if not verdict.healthy and revert:
                revert_to = lb_pool_weights(tp, op, 0)

                async def call_revert(revert_to: dict[str, float] = revert_to) -> dict[str, Any]:
                    return await self.cf.set_pool_weights(lb["id"], revert_to)

                async def observe_revert(revert_to: dict[str, float] = revert_to) -> Observation:
                    now = await self.cf.load_balancer(self.front)
                    got = (now or {}).get("random_steering", {}).get("pool_weights")
                    return got == revert_to, {"pool_weights": got}

                t_rev = time.monotonic()
                ev = await apply(self.resource_lb(), f"REVERT_{target_env.upper()}_0", call_revert, observe_revert)
                summary["reverted"] = {"at_weight": step.weight, "reasons": list(verdict.reasons),
                                       "pool_weights": revert_to, "thresholds": thresholds,
                                       "step_ms": int((time.monotonic() - t_rev) * 1000), **ev}
                await record_step("front.revert_0", summary["reverted"], "OK")
                return summary
            if not verdict.healthy:
                summary["unhealthy"] = {"at_weight": step.weight, "reasons": list(verdict.reasons)}
        return summary
