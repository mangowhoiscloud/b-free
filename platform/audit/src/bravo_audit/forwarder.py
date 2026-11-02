"""Follow the platform API event stream of each plan and store every event in the Object Lock sink.

Source: ``GET /v1/plans/{id}/events`` (SSE, CONTRACT.md 7). Each SSE ``data`` is the event JSON
``{id, plan_id, state, kind, at, mode, run_id, actor, data}``. All kinds are stored, so state transitions
(``state.changed``), approvals (``approval.granted``), gate results (``gate.verdict``), runs, receipts and
blocked agent attempts (``tool.denied``) are covered. Resume point: the highest id already in the bucket
(``Last-Event-ID``), so a restart neither skips nor duplicates.
Auth: an Access JWT for the console AUD, read from the environment variable named by ``token_env``
(on EKS: from OpenBao through ESO, never a file in the image). The token is sent only as a header.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable
from typing import Any

import httpx

from .sink import ObjectLockSink, PutResult
from .sse import parse

log = logging.getLogger("bravo_audit")


class Forwarder:
    def __init__(self, api_base: str, sink: ObjectLockSink, *, token_env: str, verify_tls: bool | str = True,
                 timeout_s: float = 30.0) -> None:
        self.api = api_base.rstrip("/")
        self.sink = sink
        self.token_env = token_env
        self.verify = verify_tls
        self.timeout_s = timeout_s

    def _headers(self, last: int | None) -> dict[str, str]:
        token = os.environ.get(self.token_env, "").strip()
        if not token:
            raise RuntimeError(f"{self.token_env} is not set (Access JWT for the console AUD)")
        h = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        if last is not None:
            h["Last-Event-ID"] = str(last)
        return h

    def store(self, events: Iterable[dict[str, Any]]) -> list[PutResult]:
        out = []
        for ev in events:
            res = self.sink.put_event(ev)
            log.info("audit %s %s id=%s kind=%s", res.outcome, res.key, ev.get("id"), ev.get("kind"))
            if res.outcome == "conflict":
                raise RuntimeError(f"stored record {res.key} differs from the API event (tamper or API change)")
            out.append(res)
        return out

    def follow(self, plan_id: str, *, max_events: int | None = None, idle_exit_s: float | None = None) -> int:
        """Stream one plan until ``max_events`` or ``idle_exit_s`` without an event (None: forever)."""
        stored = 0
        last = self.sink.last_event_id(plan_id)
        while True:
            try:
                with httpx.Client(verify=self.verify, timeout=httpx.Timeout(self.timeout_s, read=None)) as c, \
                        c.stream("GET", f"{self.api}/v1/plans/{plan_id}/events",
                                 headers=self._headers(last)) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"events HTTP {resp.status_code}: {resp.read()[:200]!r}")
                    started = time.monotonic()
                    for ev in parse(resp.iter_lines()):
                        event = json.loads(ev.data)
                        self.store([event])
                        last, stored = int(event["id"]), stored + 1
                        started = time.monotonic()
                        if max_events is not None and stored >= max_events:
                            return stored
                        if idle_exit_s is not None and time.monotonic() - started > idle_exit_s:
                            return stored
            except (httpx.TransportError, httpx.RemoteProtocolError) as exc:
                log.warning("stream for plan %s dropped (%s); resuming after id %s", plan_id, type(exc).__name__,
                            last)
                if idle_exit_s is not None:
                    return stored
                time.sleep(2)
