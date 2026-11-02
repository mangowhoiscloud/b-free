"""Stage bookkeeping and the effect ledger protocol (D-19).

``StageRecorder`` times each cutover stage with a monotonic clock and writes one
``run_stages`` row when the stage ends, OK or FAILED.

``EffectRunner`` wraps every external change:

    PREPARED (commit) -> IN_FLIGHT (commit) -> call -> observe -> CONFIRMED_APPLIED
                                               |                 \\-> UNKNOWN (observation disagrees)
                                               |-> FenceRejected -> CONFIRMED_NOT_APPLIED
                                               \\-> other error  -> UNKNOWN (never retried)

CONFIRMED_NOT_APPLIED is used only for a fence rejection: the database rolled the
transaction back, and the stale epoch can never apply it later.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from opentelemetry import trace

from .fence import FenceRejected
from .ledger import EffectState, Ledger

log = logging.getLogger(__name__)

ClockNs = Callable[[], int]
FAULT_EXIT_CODE = 86
WallClock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def error_evidence(exc: BaseException) -> dict[str, Any]:
    """Error summary for the ledger. psycopg messages carry no passwords; keep them short anyway."""
    evidence: dict[str, Any] = {"error": type(exc).__name__, "message": str(exc)[:300]}
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate:
        evidence["sqlstate"] = sqlstate
    return evidence


class StepFailed(RuntimeError):
    """A cutover step did not reach its check. ``code`` is a stable reason code."""

    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


class EffectUnconfirmed(StepFailed):
    """The call returned but the observed state does not show the change."""


@dataclass(slots=True)
class StageHandle:
    seq: int
    name: str
    detail: dict[str, Any] = field(default_factory=dict)


class StageRecorder:
    def __init__(
        self,
        ledger: Ledger,
        run_id: UUID,
        clock_ns: ClockNs = time.monotonic_ns,
        wall: WallClock = _utcnow,
    ) -> None:
        self._ledger = ledger
        self._run_id = run_id
        self._clock_ns = clock_ns
        self._wall = wall
        self._seq = 0
        self.timings_ms: dict[str, int] = {}

    @property
    def last_seq(self) -> int:
        return self._seq

    @asynccontextmanager
    async def stage(self, name: str) -> AsyncIterator[StageHandle]:
        self._seq += 1
        handle = StageHandle(seq=self._seq, name=name)
        started_at = self._wall()
        start = self._clock_ns()
        tracer = trace.get_tracer("bravo_executor")
        with tracer.start_as_current_span(f"cutover.stage.{name}") as span:
            span.set_attribute("bravo.stage.seq", handle.seq)
            try:
                yield handle
            except BaseException as exc:
                ms = (self._clock_ns() - start) // 1_000_000
                if isinstance(exc, StepFailed):
                    handle.detail.setdefault("reason", exc.code)
                    if exc.detail:
                        handle.detail.setdefault("reason_detail", exc.detail)
                else:
                    handle.detail.setdefault("reason", "EXCEPTION")
                    handle.detail.setdefault("error", error_evidence(exc))
                self.timings_ms[name] = ms
                span.set_attribute("bravo.stage.ms", ms)
                span.set_attribute("bravo.stage.result", "FAILED")
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(handle.detail.get("reason"))))
                await self._ledger.record_stage(
                    self._run_id, handle.seq, name, started_at, ms, "FAILED", handle.detail
                )
                raise
            ms = (self._clock_ns() - start) // 1_000_000
            self.timings_ms[name] = ms
            span.set_attribute("bravo.stage.ms", ms)
            span.set_attribute("bravo.stage.result", "OK")
            await self._ledger.record_stage(self._run_id, handle.seq, name, started_at, ms, "OK", handle.detail)


Observation = tuple[bool, dict[str, Any]]


class EffectRunner:
    """``crash_after`` ("resource:action") is a fault-injection point for contract tests E1/E6:
    the process exits right after that external call returns, before the result is recorded,
    which is exactly "the response was lost". It is None in every normal run."""

    def __init__(self, ledger: Ledger, run_id: UUID, epoch: int, crash_after: str | None = None) -> None:
        self._ledger = ledger
        self._run_id = run_id
        self._epoch = epoch
        self._crash_after = crash_after

    async def apply(
        self,
        resource: str,
        action: str,
        call: Callable[[], Awaitable[dict[str, Any] | None]],
        observe: Callable[[], Awaitable[Observation]],
    ) -> dict[str, Any]:
        span = trace.get_current_span()
        effect_id = await self._ledger.effect_prepare(self._run_id, resource, action, self._epoch)
        await self._ledger.effect_transition(effect_id, EffectState.PREPARED, EffectState.IN_FLIGHT)
        span.add_event("effect.in_flight", {"bravo.resource": resource, "bravo.action": action})
        try:
            call_evidence = await call() or {}
        except FenceRejected as exc:
            evidence = {"reason": "FENCE_REJECTED", "side": exc.side, "epoch": exc.epoch}
            await self._ledger.effect_transition(
                effect_id, EffectState.IN_FLIGHT, EffectState.CONFIRMED_NOT_APPLIED, evidence
            )
            span.add_event("effect.not_applied", {"bravo.resource": resource, "bravo.action": action})
            raise StepFailed("FENCE_REJECTED", {"resource": resource, "action": action}) from exc
        except BaseException as exc:
            evidence = {"reason": "CALL_FAILED", **error_evidence(exc)}
            await self._ledger.effect_transition(effect_id, EffectState.IN_FLIGHT, EffectState.UNKNOWN, evidence)
            span.add_event("effect.unknown", {"bravo.resource": resource, "bravo.action": action})
            raise
        if self._crash_after is not None and self._crash_after == f"{resource}:{action}":
            log.critical("fault injection: exiting after %s:%s (effect %s left IN_FLIGHT)", resource, action, effect_id)
            os._exit(FAULT_EXIT_CODE)
        try:
            applied, observed = await observe()
        except BaseException as exc:
            evidence = {"reason": "OBSERVE_FAILED", "call": call_evidence, **error_evidence(exc)}
            await self._ledger.effect_transition(effect_id, EffectState.IN_FLIGHT, EffectState.UNKNOWN, evidence)
            raise
        evidence = {"call": call_evidence, "observed": observed}
        if not applied:
            await self._ledger.effect_transition(effect_id, EffectState.IN_FLIGHT, EffectState.UNKNOWN, evidence)
            span.add_event("effect.unknown", {"bravo.resource": resource, "bravo.action": action})
            raise EffectUnconfirmed("EFFECT_NOT_OBSERVED", {"resource": resource, "action": action})
        await self._ledger.effect_transition(
            effect_id, EffectState.IN_FLIGHT, EffectState.CONFIRMED_APPLIED, evidence
        )
        span.add_event("effect.applied", {"bravo.resource": resource, "bravo.action": action})
        return evidence
