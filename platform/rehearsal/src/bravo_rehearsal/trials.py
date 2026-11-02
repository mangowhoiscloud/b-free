"""Trial records held by the rehearsal service (07 3).

Authoritative per-Trial state: phase, frozen artifact and digest, verdict (set once), and the
server-side tool-call record (allow/deny, rule). The platform DB owns the attempt counter;
this store owns only the running Trial. It is in memory: a restart loses running Trials,
and the API then sees ``trial_lost`` (INFRA_ERROR) and starts a new attempt. Orphaned
namespaces carry the ``bravo.dev/trial-id`` label for cleanup (07 3 "재기동").
"""

from __future__ import annotations

import asyncio
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
    leftovers: list[str] = field(default_factory=list)
    names: Names | None = None
    box: Sandbox | None = None
    locked: bool = False
    harbor_trial_dir: str | None = None
    proposed: asyncio.Event = field(default_factory=asyncio.Event)
    created_at: str = field(default_factory=now_iso)

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
            "at": now_iso(),
        }
        if len(self.tool_events) < MAX_EVENTS:
            self.tool_events.append(ev)
        return ev

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
            "namespaces": [self.names.rehearsal_ns, self.names.verify_ns] if self.names else [],
            "leftovers": self.leftovers,
            "harbor_trial_dir": self.harbor_trial_dir,
            "created_at": self.created_at,
        }


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
