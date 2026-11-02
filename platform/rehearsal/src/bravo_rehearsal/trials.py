"""Trial records held by the rehearsal service (07 3).

Authoritative per-Trial state: phase, frozen artifact and digest, verdict (set once), and the
server-side tool-call record (allow/deny, rule). The platform DB owns the attempt counter;
this store owns only the running Trial. It is in memory: a restart loses running Trials,
and the API then sees ``trial_lost`` (INFRA_ERROR) and starts a new attempt. Orphaned
namespaces carry the ``bravo.dev/trial-id`` label for cleanup (07 3 "재기동").
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .allowlist import Allowlist
from .cluster import Names, Sandbox

PHASES = ("provisioning", "agent_running", "proposed", "verifying", "done")
MAX_EVENTS = 500
# Who made a tool-layer call. ``agent``: the agent worker through /mcp. ``probe``: an operator demo probe sent with the
# Trial's own token or through the service (B10/P13: a planted attempt is never shown as the agent's behaviour).
ORIGINS = ("agent", "probe")
CALL_ORIGIN: contextvars.ContextVar[str] = contextvars.ContextVar("bravo_call_origin", default="agent")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Trial:
    trial_id: str
    tenant_id: str
    plan_id: str
    rehearsal_id: str
    attempt: int
    max_attempts: int
    task: str
    role: str
    idem_key: str
    request_digest: str
    deadline: float
    allowlist: Allowlist
    envelope: dict[str, Any]
    phase: str = "provisioning"
    verdict: str | None = None
    reason: str | None = None
    artifact: dict[str, Any] | None = None
    artifact_digest: str | None = None
    verifier: dict[str, Any] | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    hook_events: list[dict[str, Any]] = field(default_factory=list)  # worker-reported, not authoritative
    isolation: dict[str, Any] = field(default_factory=dict)  # per namespace: T3 checks as they ran
    leftovers: list[str] = field(default_factory=list)
    names: Names | None = None
    box: Sandbox | None = None
    locked: bool = False
    harbor_trial_dir: str | None = None
    repo_snapshot: str | None = None  # uploaded customer repo snapshot (snapshot.SnapshotStore), if any
    verifier_observed: dict[str, Any] | None = None  # verifier output before acceptance (reward, Pod record)
    worker_started: bool = False  # the agent worker (and so the model) was started for this Trial
    verifier_kind: str | None = None  # what the verifier did (harbor_task.STATIC_RULES today)
    proposed: asyncio.Event = field(default_factory=asyncio.Event)
    created_at: str = field(default_factory=now_iso)
    # Every phase change with its wall-clock time (progress for the console, duration per phase; B04/P2).
    phase_history: list[dict[str, Any]] = field(default_factory=list)
    network_drops: dict[str, Any] | None = None  # Hubble drop counters of the Trial namespaces (Prometheus)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "phase":
            hist = self.__dict__.get("phase_history")
            if hist is None:  # first assignment in __init__, before phase_history exists
                object.__setattr__(self, "_first_phase", value)
            elif not hist or hist[-1]["phase"] != value:
                hist.append({"phase": value, "at": now_iso(), "t": time.time()})
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        first = self.__dict__.pop("_first_phase", self.phase)
        self.phase_history.append({"phase": first, "at": self.created_at, "t": time.time()})

    def timings(self) -> dict[str, Any]:
        """Seconds per phase and in total, from the recorded phase changes (wall clock of this service)."""
        hist = self.phase_history
        per: dict[str, float] = {}
        for cur, nxt in zip(hist, hist[1:], strict=False):
            per[cur["phase"]] = round(per.get(cur["phase"], 0.0) + nxt["t"] - cur["t"], 1)
        end = hist[-1]["t"] if self.verdict is not None else time.time()
        return {"started_at": hist[0]["at"] if hist else self.created_at,
                "finished_at": hist[-1]["at"] if self.verdict is not None and hist else None,
                "elapsed_s": round(end - hist[0]["t"], 1) if hist else None, "phase_s": per}

    def running(self) -> bool:
        return self.phase == "agent_running" and not self.locked and time.time() < self.deadline

    def record(self, tool: str, args: Any, allowed: bool, rule: str, result: str | None = None) -> dict[str, Any]:
        raw = json.dumps(args, sort_keys=True, default=str).encode()
        ev = {
            "seq": len(self.tool_events) + 1,
            "tool": tool,
            "args_sha256": hashlib.sha256(raw).hexdigest(),
            "allowed": allowed,
            "rule": rule,
            "result": result,
            "origin": CALL_ORIGIN.get(),
            "at": now_iso(),
        }
        if len(self.tool_events) < MAX_EVENTS:
            self.tool_events.append(ev)
        return ev

    def mark_last(self, tool: str, args: Any, origin: str) -> int | None:
        """Label the latest agent-labelled event for (tool, args) with another origin (the CLI runner's probes go
        through /mcp with the Trial token, so the service first records them as ``agent``): the record says who
        called."""
        sha = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
        for ev in reversed(self.tool_events):
            if ev["tool"] == tool and ev["args_sha256"] == sha and ev.get("origin", "agent") == "agent":
                ev["origin"] = origin
                return int(ev["seq"])
        return None

    def add_hook_event(self, event: dict[str, Any]) -> bool:
        """Store one worker hook event once per ``seq`` (bounded). Hook input is hashed, not stored."""
        seq = event.get("seq")
        if not isinstance(seq, int) or any(e["seq"] == seq for e in self.hook_events):
            return False
        if len(self.hook_events) < MAX_EVENTS:
            raw = json.dumps(event.get("tool_input"), sort_keys=True, default=str).encode()
            self.hook_events.append({"seq": seq, "kind": str(event.get("kind", ""))[:32],
                                     "tool_name": str(event.get("tool_name", ""))[:128],
                                     "tool_input_sha256": hashlib.sha256(raw).hexdigest(),
                                     "at": str(event.get("at", ""))[:64], "authoritative": False})
        return True

    def finish(self, verdict: str, reason: str | None) -> bool:
        """Set the terminal verdict once (CAS). Later results are refused."""
        if self.verdict is not None:
            return False
        self.verdict, self.reason, self.phase, self.locked = verdict, reason, "done", True
        self.proposed.set()
        return True

    def view(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "plan_id": self.plan_id,
            "rehearsal_id": self.rehearsal_id,
            "attempt": self.attempt,
            "task": self.task,
            "role": self.role,
            "phase": self.phase,
            "verdict": self.verdict,
            "reason": self.reason,
            "artifact": self.artifact,
            "artifact_digest": self.artifact_digest,
            "verifier": self.verifier,
            "tool_events": self.tool_events,
            "hook_events": self.hook_events,
            "isolation": self.isolation,
            "namespaces": [self.names.rehearsal_ns, self.names.verify_ns] if self.names else [],
            "leftovers": self.leftovers,
            "harbor_trial_dir": self.harbor_trial_dir,
            "verifier_kind": self.verifier_kind,
            "worker_started": self.worker_started,
            "verifier_observed": self.verifier_observed,
            "created_at": self.created_at,
            "phase_history": [{"phase": h["phase"], "at": h["at"]} for h in self.phase_history],
            "timings": self.timings(),
            "tool_summary": self.tool_summary(),
            "network_drops": self.network_drops,
        }

    def tool_summary(self) -> dict[str, Any]:
        """Allowed and denied calls by origin, so a probe's denial is never counted as the agent's (B10)."""
        out: dict[str, dict[str, int]] = {}
        for ev in self.tool_events:
            o = out.setdefault(ev.get("origin", "agent"), {"allowed": 0, "denied": 0})
            o["allowed" if ev["allowed"] else "denied"] += 1
        return out


class TrialStore:
    def __init__(self) -> None:
        self._by_id: dict[str, Trial] = {}
        self._by_key: dict[str, str] = {}

    def get(self, trial_id: str) -> Trial | None:
        return self._by_id.get(trial_id)

    def by_key(self, key: str) -> Trial | None:
        tid = self._by_key.get(key)
        return self._by_id.get(tid) if tid else None

    def add(self, trial: Trial) -> None:
        self._by_id[trial.trial_id] = trial
        self._by_key[trial.idem_key] = trial.trial_id
