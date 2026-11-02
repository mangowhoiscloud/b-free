"""Rehearsal start and result acceptance (FR-4, FR-5, S2, §8.3-8.4, 05 3, 07 3).

The API never runs an agent and never judges a proposal. It asks platform/rehearsal for one
Trial per attempt and accepts the Trial's verdict only when it is bound to this plan, this
attempt, this Trial and the artifact digest (05 3 "결과 수용"). Terminal status is set once
(CAS on ``rehearsals.status``). The attempt counter lives in the platform DB: three FAIL
verdicts -> NEEDS_HUMAN (FR-5); infrastructure errors have their own cap (07 3).
Preconditions that stopped a Trial before anything ran (``llm_unconfigured`` and friends)
are recorded as INFRA_ERROR with that reason and are not counted. No LLM here (NFR-5).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from . import idempotency
from .auth import Action, Channel, Principal, authorize
from .changes import open_change
from .config import Settings
from .db import Conn, Database
from .errors import BusinessError
from .events import SYSTEM, Actor, EventKind, EventMode, emit
from .models import RehearsalStart
from .statemachine import PRE_CUTOVER, STAGE_ROLES, State, Trigger, lock_plan, transition
from .upstreams import SNAPSHOT_REFUSALS, CollectorClient, RehearsalClient, UpstreamError

LOG = logging.getLogger("bravo_api.rehearsal")

# Reasons that mean "nothing was run": not an attempt, not an infrastructure failure.
PRECONDITIONS = frozenset(
    {
        # platform/rehearsal POST /v1/trials preconditions (Orchestrator.preconditions, llm_credential)
        "llm_unconfigured",
        "cluster_unconfigured",
        "rehearsal_unreachable",
        "task_unknown",
        "verifier_unconfigured",
        "tool_layer_unconfigured",
        "role_task_mismatch",
        "role_unknown",
        "role_allowlist_missing",
        "role_allowlist_invalid",
        "unauthorized",
        "envelope_invalid",
        "state_not_allowed_for_role",
        "agent_runtime_unavailable",
        "openbao_unconfigured",
        "openbao_unreachable",
        "openbao_denied",
        "llm_token_missing",
        "bad_request",
        "idempotency_key_reuse",
        # API side, before the Trial request: the collector inventory and the repo snapshot bound to it
        "inventory_unavailable",
        "inventory_root_empty",  # collector 503: nothing mounted to scan (not "no dependencies")
        "collector_unreachable",
        "collector_response_lost",
        "collector_bad_response",
        "snapshot_unavailable",
        "snapshot_inventory_mismatch",
        "snapshot_upload_failed",
        # platform/rehearsal PUT /v1/snapshots refusals (SnapshotRejected)
        *SNAPSHOT_REFUSALS,
    }
)
EVIDENCE_MAX = 48  # StateEnvelope allows 64; the envelope must stay under 32 KiB with the task instruction
SUMMARY_MAX = 160
TIER_ORDER = {"code_change": 0, "unclassified": 1, "adapter": 2, "config_swap": 3, "no_change": 4}
_REASON_RE = re.compile(r"[^a-z0-9_]+")


def _reason_code(reason: str | None) -> str:
    """``PriorFailure.reason`` pattern ``^[a-z0-9_]{1,64}$``: the recorded reason with other characters folded."""
    code = _REASON_RE.sub("_", (reason or "").lower()).strip("_")[:64]
    return code or "no_reason_recorded"


def _failed_checks(verifier: Any) -> str:
    """The verifier's failed checks with what was observed (e.g. ``gate_OBJ-06: UNKNOWN plain_http_inconclusive error
    400 InvalidRequest``): what the next attempt has to fix (spec S2 4: analyse the cause and retry). Names and observed
    answers only; the private tests themselves are never sent."""
    checks = verifier.get("checks") if isinstance(verifier, dict) else None
    failed = [c for c in checks or [] if isinstance(c, dict) and not c.get("ok")]
    return "; ".join(f"{str(c.get('name'))[:60]}: {str(c.get('detail') or '')[:120]}" for c in failed[:4])


def prior_failures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FAIL verdicts of the current attempt budget, oldest first, as ``bravo_agents.PriorFailure``."""
    out = []
    for r in rows:
        detail = _failed_checks(r.get("verifier"))
        summary = f"attempt {r['attempt']}: verifier verdict FAIL ({r['reason'] or 'no reason recorded'})"
        out.append({"attempt": r["attempt"], "verdict": "FAIL", "reason": _reason_code(r["reason"]),
                    "summary": (summary + (f"; failed checks: {detail}" if detail else ""))[:500]})
    return out


def evidence_from(inventory: dict[str, Any], gate: dict[str, Any] | None) -> list[dict[str, Any]]:
    """``bravo_agents.Evidence`` items from what the platform actually holds: the collector inventory the
    snapshot is bound to (finding ids are the collector's own) and the last gate verdict's blocked rows.
    Metadata only: the finding's masked source line is not copied (it stays in the collector's output)."""
    items: list[dict[str, Any]] = []
    if gate is not None:
        for c in gate["controls"] or []:
            # PASS rows and approved exceptions (S1 5b, accepted as they are) are not work for the agent.
            if not isinstance(c, dict) or c.get("verdict") in ("PASS", "EXCEPTION") or not isinstance(
                c.get("control_id"), str
            ):
                continue
            items.append(
                {
                    "id": f"gate:{gate['verdict_id']}:{c['control_id'].lower()}",
                    "kind": "gate_control",
                    "summary": (
                        f"{c['control_id']} src={c.get('src')} dst={c.get('dst')} verdict={c.get('verdict')} "
                        f"reason={c.get('reason')}"
                    )[:SUMMARY_MAX],
                }
            )
    findings = [*inventory.get("findings", []), *inventory.get("unclassified", [])]
    findings.sort(key=lambda f: (TIER_ORDER.get(f["tier_code"], 9), f["file"], f["line"], f["id"]))
    seen: set[str] = set()
    for f in findings:
        eid = f"inv:{f['id']}"
        if eid in seen:
            continue
        seen.add(eid)
        items.append(
            {
                "id": eid,
                "kind": "inventory_finding",
                "summary": f"{f['tier_code']} {f['service']} {f['kind']} {f['file']}:{f['line']} ({f['rule_id']})"[
                    :SUMMARY_MAX
                ],
            }
        )
    return items[:EVIDENCE_MAX]


def trial_request(
    *,
    plan_id: UUID,
    tenant_id: str,
    rehearsal_id: UUID,
    attempt: int,
    max_attempts: int,
    task: str,
    role: str,
    state: str,
    prior: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    source_env: str,
    target_env: str,
    analysis: dict[str, Any] | None,
    inventory_digest: str,
) -> dict[str, Any]:
    """Body of platform/rehearsal ``POST /v1/trials`` (``TrialRequest``, extra fields forbidden)."""
    return {
        "plan_id": str(plan_id),
        "tenant_id": tenant_id,
        "rehearsal_id": str(rehearsal_id),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "task": task,
        "role": role,
        "state": state,
        "prior_failures": prior,
        "evidence": evidence,
        "source_env": source_env,
        "target_env": target_env,
        "analysis": analysis,
        "inventory_digest": inventory_digest,
    }
OPEN = ("requested", "running", "proposed")


def artifact_digest(artifact: Any) -> str:
    """sha256 over canonical JSON. Must match platform/rehearsal ``artifact.digest``."""
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def bound_problem(body: dict[str, Any], row: dict[str, Any]) -> str | None:
    """Why a terminal Trial result cannot be accepted for this rehearsal row, or None."""
    if body.get("trial_id") != row["trial_id"] or body.get("plan_id") != str(row["plan_id"]):
        return "verdict_unbound:trial"
    if body.get("attempt") != row["attempt"]:
        return "verdict_unbound:attempt"
    verdict = body.get("verdict")
    if verdict == "INFRA_ERROR":
        return None
    artifact, digest = body.get("artifact"), body.get("artifact_digest")
    if artifact is None or not isinstance(digest, str) or artifact_digest(artifact) != digest:
        return "verdict_unbound:artifact"
    v = body.get("verifier")
    if not isinstance(v, dict) or v.get("artifact_digest") != digest:
        return "verdict_unbound:verifier"
    for k in ("run_id", "pod_uid", "image_digest", "task_version", "collected_at"):
        if not isinstance(v.get(k), str) or not v[k]:
            return f"verdict_unbound:verifier_{k}"
    reward = v.get("reward")
    if not isinstance(reward, (int, float)) or isinstance(reward, bool):
        return "verdict_unbound:reward"
    if (verdict == "PASS") != (reward >= 1):
        return "verdict_unbound:reward_mismatch"
    return None


class RehearsalService:
    def __init__(
        self,
        db: Database,
        client: RehearsalClient | None,
        settings: Settings,
        collector: CollectorClient | None = None,
    ) -> None:
        self._db = db
        self._client = client
        self._settings = settings
        self._collector = collector

    async def start(
        self, p: Principal, channel: Channel, plan_id: UUID, req: RehearsalStart, key: str | None
    ) -> dict[str, Any]:
        authorize(p, Action.REHEARSAL_START, channel)
        key = idempotency.validate_key(key)
        digest = idempotency.request_digest(
            {"action": "rehearsal_start", "plan_id": str(plan_id), "task": req.task, "role": req.role}
        )
        if req.role not in STAGE_ROLES[State.PROPOSE.value]:
            raise BusinessError("role_not_allowed_in_stage", status=422)
        actor = Actor.of(p)
        async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
            replay = await idempotency.claim(
                conn, tenant_id=p.tenant_id, action="rehearsal_start", key=key, digest=digest, principal_id=p.id
            )
            if replay is not None:
                return replay
            plan = await lock_plan(conn, plan_id)
            state = State(plan["state"])
            if state.value not in PRE_CUTOVER:
                raise BusinessError("rehearsal_not_allowed", detail={"state": state.value})  # S2-AC5
            if plan["active_rehearsal_id"] is not None:
                raise BusinessError("rehearsal_active")
            if self._client is None:  # checked after the state rules so S2-AC5 is reported as such
                raise BusinessError("rehearsal_unconfigured", status=503)
            if state is State.NEEDS_HUMAN:
                if channel is not Channel.CONSOLE:
                    raise BusinessError("needs_human_console_only", status=403)
                plan = await transition(
                    conn,
                    plan_id,
                    Trigger.REPROPOSAL_REQUESTED,
                    actor=actor,
                    reason="human_reset",
                    updates={"rehearsal_failures": 0, "rehearsal_infra_errors": 0},
                )
            elif state is State.GATE:
                if plan["last_gate_verdict_id"] is None:
                    raise BusinessError("gate_not_run")
                plan = await transition(conn, plan_id, Trigger.REPROPOSAL_REQUESTED, actor=actor, reason="gate_block")
            elif state is not State.PROPOSE:
                raise BusinessError("state_mismatch", detail={"state": state.value})
            # The attempt within the FR-5 budget (1..max): FAIL verdicts so far + 1. Precondition and infra
            # retries reuse the number, so the envelope's attempt <= max_attempts holds (bravo_agents).
            failures = int(plan["rehearsal_failures"])
            attempt = failures + 1
            cur = await conn.execute(
                "SELECT attempt, reason, verifier FROM rehearsals WHERE plan_id = %s AND status = 'FAIL' "
                "ORDER BY created_at DESC LIMIT %s",
                (plan_id, failures),
            )
            prior = prior_failures(list(reversed(await cur.fetchall())))
            analysis = plan["analysis"]
            cur = await conn.execute(
                "SELECT environment_id, name FROM environments WHERE environment_id = ANY(%s)",
                ([plan["source_environment_id"], plan["target_environment_id"]],),
            )
            env_names = {r["environment_id"]: r["name"] for r in await cur.fetchall()}
            gate = None
            if plan["last_gate_verdict_id"] is not None:
                cur = await conn.execute(
                    "SELECT verdict_id, controls FROM gate_verdicts WHERE verdict_id = %s", (plan["last_gate_verdict_id"],)
                )
                gate = await cur.fetchone()
            rehearsal_id = uuid.uuid4()
            idem = f"rehearsal-{rehearsal_id}"
            await conn.execute(
                """INSERT INTO rehearsals (rehearsal_id, tenant_id, plan_id, attempt, task, role, idem_key, status,
                                           requested_by, channel)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'requested', %s, %s)""",
                (rehearsal_id, p.tenant_id, plan_id, attempt, req.task, req.role, idem, p.id, channel.value),
            )
            await conn.execute(
                "UPDATE plans SET active_rehearsal_id = %s, rehearsal = 'running', version = version + 1, "
                "updated_at = now() WHERE plan_id = %s",
                (rehearsal_id, plan_id),
            )
            await emit(
                conn,
                plan_id=plan_id,
                kind=EventKind.TRIAL_STARTED,
                mode=EventMode.LIVE,
                actor=actor,
                data={
                    "rehearsal_id": str(rehearsal_id),
                    "attempt": attempt,
                    "task": req.task,
                    "role": req.role,
                    "status": "requested",
                },
            )
            response: dict[str, Any] = {
                "plan_id": str(plan_id),
                "rehearsal_id": str(rehearsal_id),
                "attempt": attempt,
                "task": req.task,
                "role": req.role,
                "trial_id": None,
                "status": "requested",
                "state": State.PROPOSE.value,
                "reason": None,
            }
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="rehearsal_start", key=key, response=response, status_code=202
            )

        assert self._client is not None
        try:
            # 1. The collector inventory the Trial is bound to. 2. The repo snapshot matching its input_digest,
            # uploaded before the Trial (platform/rehearsal repo_read and /src read only that). 3. The Trial.
            inventory = await self._inventory()
            digest_hex = inventory["input_digest"]
            snapshot = await self._snapshot(digest_hex)
            await self._client.put_snapshot(p.tenant_id, str(plan_id), snapshot, digest_hex)
            payload = trial_request(
                plan_id=plan_id,
                tenant_id=p.tenant_id,
                rehearsal_id=rehearsal_id,
                attempt=attempt,
                max_attempts=self._settings.rehearsal_max_attempts,
                task=req.task,
                role=req.role,
                state=State.PROPOSE.value,
                prior=prior,
                evidence=evidence_from(inventory, gate),
                source_env=env_names[plan["source_environment_id"]],
                target_env=env_names[plan["target_environment_id"]],
                analysis=analysis,
                inventory_digest=digest_hex,
            )
            started = await self._client.start(payload, idem)
        except UpstreamError as exc:
            reason = exc.reason
            missing = exc.body.get("missing") if isinstance(exc.body, dict) else None
            if missing:
                LOG.info("rehearsal refused before running rehearsal_id=%s missing=%s", rehearsal_id, missing)
            async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
                row = await _row(conn, rehearsal_id)
                plan = await settle(conn, self._settings, row, {"verdict": "INFRA_ERROR", "reason": reason})
                response = {**response, "status": "INFRA_ERROR", "reason": reason, "state": plan["state"]}
                await idempotency.complete(
                    conn, tenant_id=p.tenant_id, action="rehearsal_start", key=key, response=response, status_code=202
                )
            return response
        async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
            await conn.execute(
                "UPDATE rehearsals SET trial_id = %s, status = 'running' WHERE rehearsal_id = %s AND status = 'requested'",
                (started["trial_id"], rehearsal_id),
            )
            response = {**response, "trial_id": started["trial_id"], "status": "running"}
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="rehearsal_start", key=key, response=response, status_code=202
            )
        return response


    async def _inventory(self) -> dict[str, Any]:
        """The collector inventory, or ``UpstreamError`` with a precondition reason (nothing is invented)."""
        if self._collector is None:
            raise UpstreamError("inventory_unavailable")
        try:
            return await self._collector.inventory()
        except UpstreamError as exc:
            if exc.reason in PRECONDITIONS:
                raise
            LOG.warning("collector inventory unavailable: %s", exc.reason)
            raise UpstreamError("inventory_unavailable", exc.status) from exc

    async def _snapshot(self, input_digest: str) -> bytes:
        assert self._collector is not None  # _inventory() ran first
        return await self._collector.snapshot(input_digest)


async def _row(conn: Conn, rehearsal_id: UUID) -> dict[str, Any]:
    cur = await conn.execute("SELECT * FROM rehearsals WHERE rehearsal_id = %s", (rehearsal_id,))
    row = await cur.fetchone()
    if row is None:
        raise BusinessError("rehearsal_not_found", status=404)
    return row


async def settle(conn: Conn, settings: Settings, row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Record a terminal Trial result once and move the plan. Returns the plan row."""
    plan = await lock_plan(conn, row["plan_id"])
    verdict = body.get("verdict")
    reason = body.get("reason") if isinstance(body.get("reason"), str) else None
    if verdict not in ("PASS", "FAIL", "INFRA_ERROR"):
        verdict, reason = "INFRA_ERROR", "verdict_missing"
    cur = await conn.execute(
        """UPDATE rehearsals SET status = %s, reason = %s, artifact_digest = COALESCE(%s, artifact_digest),
                  artifact = COALESCE(%s, artifact), verifier = %s, ended_at = now()
            WHERE rehearsal_id = %s AND status = ANY(%s) RETURNING *""",
        (
            verdict,
            reason,
            body.get("artifact_digest") if verdict != "INFRA_ERROR" else None,
            Jsonb(body["artifact"]) if verdict != "INFRA_ERROR" and body.get("artifact") is not None else None,
            Jsonb(body["verifier"]) if isinstance(body.get("verifier"), dict) else None,
            row["rehearsal_id"],
            list(OPEN),
        ),
    )
    if await cur.fetchone() is None or plan["active_rehearsal_id"] != row["rehearsal_id"]:
        LOG.warning("late or duplicate trial result ignored rehearsal_id=%s", row["rehearsal_id"])
        return plan  # terminal status already set: late results are recorded in logs only
    await emit(
        conn,
        plan_id=row["plan_id"],
        kind=EventKind.TRIAL_VERDICT,
        mode=EventMode.LIVE,
        actor=SYSTEM,
        data={
            "rehearsal_id": str(row["rehearsal_id"]),
            "trial_id": row["trial_id"],
            "attempt": row["attempt"],
            "verdict": verdict,
            "reason": reason,
            "artifact_digest": body.get("artifact_digest") if verdict != "INFRA_ERROR" else None,
            "verifier": body.get("verifier") if isinstance(body.get("verifier"), dict) else None,
        },
    )
    state = State(plan["state"])
    common: dict[str, Any] = {"actor": SYSTEM, "data": {"rehearsal_id": str(row["rehearsal_id"])}}
    base = {"active_rehearsal_id": None, "last_rehearsal_id": row["rehearsal_id"]}
    if state not in (State.PROPOSE, State.REHEARSE):  # pragma: no cover - guarded by active_rehearsal_id
        return plan
    if verdict == "PASS":
        if state is State.PROPOSE:
            plan = await transition(conn, plan["plan_id"], Trigger.PROPOSAL_ACCEPTED, **common)
        # Spec r2: stay in REHEARSE and open a change (T31). The change approval binds the diff, the image
        # digest the trusted pipeline builds from it and the policy diff; only the applied change leaves for GATE.
        plan = await transition(
            conn,
            plan["plan_id"],
            Trigger.REHEARSAL_PASSED,
            reason="change_requested",
            updates={
                **base,
                "rehearsal": "passed",
                "approved_artifact_digest": body["artifact_digest"],
                "revision": plan["revision"] + 1,
            },  # new content -> new revision (approvals bind it)
            **common,
        )
        await open_change(conn, plan, row, body["artifact"], body["artifact_digest"], actor=SYSTEM)
        return await lock_plan(conn, plan["plan_id"])
    if verdict == "FAIL":
        failures = plan["rehearsal_failures"] + 1
        exhausted = failures >= settings.rehearsal_max_attempts
        return await transition(
            conn,
            plan["plan_id"],
            Trigger.ATTEMPTS_EXHAUSTED if exhausted else Trigger.REHEARSAL_FAILED,
            reason="rehearsal_attempts_exhausted" if exhausted else f"rehearsal_failed:{reason or 'verifier'}",
            updates={**base, "rehearsal": "needs_human" if exhausted else "failed", "rehearsal_failures": failures},
            **common,
        )
    infra = plan["rehearsal_infra_errors"] + (0 if reason in PRECONDITIONS else 1)
    exhausted = infra > settings.rehearsal_max_infra_errors
    return await transition(
        conn,
        plan["plan_id"],
        Trigger.ATTEMPTS_EXHAUSTED if exhausted else Trigger.REHEARSAL_INFRA_ERROR,
        reason="rehearsal_infra_errors_exhausted" if exhausted else reason or "infra_error",
        updates={**base, "rehearsal": "needs_human" if exhausted else "infra_error", "rehearsal_infra_errors": infra},
        **common,
    )


PHASES = ("provisioning", "agent_running", "proposed", "verifying", "done")


class RehearsalWatcher:
    """Polls in-flight Trials (any channel started them) and applies their progress."""

    async def _progress(self, conn: Conn, row: dict[str, Any], body: dict[str, Any]) -> None:
        """One ``trial.progress`` per Trial phase, under the plan lock; the events table itself is the dedupe record
        (two API replicas poll the same Trial)."""
        phase = body.get("phase")
        if phase not in PHASES:
            return
        cur = await conn.execute(
            "SELECT 1 FROM events WHERE plan_id = %s AND kind = %s AND data->>'rehearsal_id' = %s "
            "AND data->>'phase' = %s LIMIT 1",
            (row["plan_id"], EventKind.TRIAL_PROGRESS.value, str(row["rehearsal_id"]), phase),
        )
        if await cur.fetchone() is not None:
            return
        timings = body.get("timings") if isinstance(body.get("timings"), dict) else {}
        await emit(
            conn,
            plan_id=row["plan_id"],
            kind=EventKind.TRIAL_PROGRESS,
            mode=EventMode.LIVE,
            actor=SYSTEM,
            data={
                "rehearsal_id": str(row["rehearsal_id"]),
                "trial_id": row["trial_id"],
                "role": row["role"],
                "phase": phase,
                "elapsed_s": timings.get("elapsed_s"),
                "phase_s": timings.get("phase_s"),
                "tool_summary": body.get("tool_summary") if isinstance(body.get("tool_summary"), dict) else None,
            },
        )

    def __init__(self, db: Database, client: RehearsalClient | None, settings: Settings) -> None:
        self._db = db
        self._client = client
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._client is not None and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="rehearsal-watch")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep watching; the Trial record is the source of truth
                LOG.exception("rehearsal watcher poll failed")
            await asyncio.sleep(self._settings.rehearsal_poll_interval_s)

    async def poll_once(self) -> int:
        assert self._client is not None
        async with self._db.tenant_tx("") as conn:
            cur = await conn.execute("SELECT tenant_id, plan_id, rehearsal_id FROM bravo_inflight_rehearsals()")
            inflight = await cur.fetchall()
        for item in inflight:
            async with self._db.tenant_tx(item["tenant_id"]) as conn:
                row = await _row(conn, item["rehearsal_id"])
            if not row["trial_id"]:
                continue  # the start call settles it
            try:
                body = await self._client.get(row["trial_id"])
            except UpstreamError as exc:
                if exc.reason != "trial_lost":
                    continue  # transient: read again next poll, never invent a result
                body = {"verdict": "INFRA_ERROR", "reason": "trial_lost"}
            async with self._db.tenant_tx(item["tenant_id"]) as conn:
                await self._apply(conn, row, body)
        return len(inflight)

    async def _apply(self, conn: Conn, row: dict[str, Any], body: dict[str, Any]) -> None:
        plan = await lock_plan(conn, row["plan_id"])
        raw_events = body.get("tool_events")
        events: list[dict[str, Any]] = [
            e
            for e in (raw_events if isinstance(raw_events, list) else [])
            if isinstance(e, dict) and isinstance(e.get("seq"), int)
        ]
        last = row["tool_seq"]
        for ev in sorted(events, key=lambda e: e["seq"]):
            if ev["seq"] <= last:
                continue
            last = ev["seq"]
            if not ev.get("allowed"):
                await emit(
                    conn,
                    plan_id=row["plan_id"],
                    kind=EventKind.TOOL_DENIED,
                    mode=EventMode.LIVE,
                    actor=SYSTEM,
                    data={
                        "rehearsal_id": str(row["rehearsal_id"]),
                        "trial_id": row["trial_id"],
                        "seq": ev["seq"],
                        "tool": ev.get("tool"),
                        "rule": ev.get("rule"),
                        # Who called (B10/P13): "agent" (the worker through /mcp) or "probe" (an operator demo
                        # probe through the rehearsal service). A probe's denial is never the agent's behaviour.
                        "origin": ev.get("origin") if ev.get("origin") in ("agent", "probe") else "agent",
                        "at": ev.get("at"),
                    },
                )
        if last != row["tool_seq"]:
            await conn.execute("UPDATE rehearsals SET tool_seq = %s WHERE rehearsal_id = %s", (last, row["rehearsal_id"]))
        await self._progress(conn, row, body)
        verdict = body.get("verdict")
        if verdict in ("PASS", "FAIL", "INFRA_ERROR"):
            problem = None if verdict == "INFRA_ERROR" and "trial_id" not in body else bound_problem(body, row)
            if problem:
                body = {"verdict": "INFRA_ERROR", "reason": problem}
            await settle(conn, self._settings, row, body)
            return
        if body.get("phase") in ("proposed", "verifying") and row["status"] == "running":
            digest = body.get("artifact_digest")
            if body.get("artifact") is None or not isinstance(digest, str) or artifact_digest(body["artifact"]) != digest:
                return  # not a frozen artifact yet; wait for the terminal result
            await conn.execute(
                "UPDATE rehearsals SET status = 'proposed', artifact_digest = %s, artifact = %s "
                "WHERE rehearsal_id = %s AND status = 'running'",
                (digest, Jsonb(body["artifact"]), row["rehearsal_id"]),
            )
            await emit(
                conn,
                plan_id=row["plan_id"],
                kind=EventKind.TRIAL_PROPOSED,
                mode=EventMode.LIVE,
                actor=SYSTEM,
                data={"rehearsal_id": str(row["rehearsal_id"]), "trial_id": row["trial_id"], "artifact_digest": digest},
            )
            if plan["state"] == State.PROPOSE.value and plan["active_rehearsal_id"] == row["rehearsal_id"]:
                await transition(
                    conn,
                    row["plan_id"],
                    Trigger.PROPOSAL_ACCEPTED,
                    actor=SYSTEM,
                    data={"rehearsal_id": str(row["rehearsal_id"]), "artifact_digest": digest},
                )
