"""Handlers shared by REST and MCP (08 4: one handler, two channels).

Every handler calls ``authorize`` first, opens one tenant transaction for its writes and
returns only server-generated values: states, IDs, reason codes, counts, console links.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg import errors as pgerrors
from psycopg.types.json import Jsonb

from . import idempotency
from .auth import Action, Channel, Principal, authorize
from .changes import change_view
from .config import Settings
from .db import Conn, Database
from .errors import BusinessError, NotFoundError
from .events import SYSTEM, Actor, EventKind, EventMode, emit, list_events, receipt_body
from .executor_client import ExecutorClient, ExecutorError
from .gate import GatePolicy, Verdict, evaluate
from .models import ApprovalDecision, EnvRegister, PlanCreate
from .secretrefs import BaoSettings, SecretRefResolver
from .statemachine import State, Trigger, lock_plan, transition
from .upstreams import GAP_GRADES, ROOT_EMPTY, CollectorClient, GateControlsClient, UpstreamError


@dataclass(frozen=True, slots=True)
class Snapshot:
    controls: list[dict[str, Any]] | None
    readiness: dict[str, Any] | None
    errors: tuple[str, ...]
    collected_at: datetime

    def as_json(self) -> dict[str, Any]:
        controls = [
            {k: c.get(k) for k in ("control_id", "src", "dst")} for c in (self.controls or []) if isinstance(c, dict)
        ]
        r = self.readiness or {}
        readiness = {k: r.get(k) for k in ("lag_bytes", "sub_state", "schema_hash", "seq", "rows")}
        return {"controls": controls, "readiness": readiness, "collected_at": self.collected_at.isoformat()}

    def summary(self) -> dict[str, Any]:
        return {
            "status": "unavailable" if self.errors else "ok",
            "reason": self.errors[0] if self.errors else None,
            "controls": len(self.controls or []),
            "collected_at": self.collected_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Inventory:
    """Collector inventory (``inventory/v1``) as read, or why it is missing.

    ``status``: ok | unconfigured | unavailable (collector answered ``inventory_root_empty``) | error.

    Gaps are findings whose ``tier_code`` needs a proposal (``code_change``, ``unclassified``; FR-2, §8.1), from
    ``findings[]`` and ``unclassified[]``. Test-code findings (``test_findings``) are not migration scope.
    """

    status: str
    body: dict[str, Any] | None
    reason: str | None

    def _items(self) -> list[dict[str, Any]]:
        b = self.body or {}
        return [d for key in ("findings", "unclassified") for d in (b.get(key) or []) if isinstance(d, dict)]

    def gaps(self) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out = []
        for d in self._items():
            if d.get("tier_code") in GAP_GRADES and d["id"] not in seen:
                seen.add(d["id"])
                out.append({"id": d["id"], "kind": d.get("kind"), "grade": d["tier_code"], "service": d.get("service"),
                            "file": d.get("file"), "line": d.get("line"), "rule_id": d.get("rule_id")})
        return out

    def as_json(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        ids: set[str] = set()
        for d in self._items():
            if d["id"] in ids:
                continue
            ids.add(d["id"])
            counts[d["tier_code"]] = counts.get(d["tier_code"], 0) + 1
        b = self.body or {}
        return {
            "status": self.status,
            "reason": self.reason,
            "schema_version": b.get("schema_version"),
            "rules_version": b.get("rules_version"),
            "input_digest": b.get("input_digest"),
            "files_scanned": b.get("files_scanned"),
            "dependencies": len(ids),
            "by_grade": counts,
        }


def _schema_match(readiness: dict[str, Any]) -> bool:
    hashes = readiness.get("schema_hash")
    return isinstance(hashes, dict) and bool(hashes.get("src")) and hashes.get("src") == hashes.get("dst")


# States in which the gate (or, in WATCH, the control re-test) may run.
GATE_STATES = frozenset({State.COLLECT, State.ANALYZE, State.GATE, State.AWAIT_APPROVAL, State.NEEDS_HUMAN, State.WATCH})


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _uuid(value: Any) -> str | None:
    return str(value) if value else None


class Services:
    def __init__(
        self,
        db: Database,
        executor: ExecutorClient,
        settings: Settings,
        gate_controls: GateControlsClient | None = None,
        collector: CollectorClient | None = None,
    ) -> None:
        self.db = db
        self.executor = executor
        self.settings = settings
        self.gate_controls = gate_controls
        self.collector = collector
        self.gate_policy = GatePolicy(
            required_controls=settings.gate_required_controls, max_lag_bytes=settings.gate_max_lag_bytes
        )
        self.secret_refs = SecretRefResolver(
            BaoSettings(settings.bao_addr, settings.bao_cacert, settings.bao_kv_mount, settings.bao_role_id_file,
                        settings.bao_secret_id_file, settings.bao_token_file)
        )

    def console_url(self, plan_id: UUID) -> str:
        return f"{self.settings.console_base_url}/plans/{plan_id}"

    # Environments ---------------------------------------------------------------------

    async def env_register(self, p: Principal, channel: Channel, req: EnvRegister, key: str | None) -> dict[str, Any]:
        authorize(p, Action.ENV_REGISTER, channel)
        key = idempotency.validate_key(key)
        # tenants/<tenant>/... or the platform OpenBao layout envs/<this environment's name>/... (D-26, 0006).
        prefixes = (f"tenants/{p.tenant_id}/", f"envs/{req.name}/")
        if not req.secret_ref.startswith(prefixes) or ".." in req.secret_ref:
            raise BusinessError("secret_ref_out_of_scope", status=422)
        digest = idempotency.request_digest({"action": "env_register", **req.model_dump(mode="json")})
        resolution = await self.secret_refs.resolve(req.secret_ref)  # metadata only, outside any transaction
        if resolution.status == "not_found":
            raise BusinessError("secret_ref_not_found", status=422, detail={"reason": resolution.reason})
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            replay = await idempotency.claim(
                conn, tenant_id=p.tenant_id, action="env_register", key=key, digest=digest, principal_id=p.id
            )
            if replay is not None:
                return replay
            env_id = uuid.uuid4()
            try:
                async with conn.transaction():
                    await conn.execute(
                        """INSERT INTO environments (environment_id, tenant_id, name, role, engine, secret_ref, created_by,
                                                    secret_status, secret_reason, secret_checked_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())""",
                        (env_id, p.tenant_id, req.name, req.role, req.engine, req.secret_ref, p.id, resolution.status,
                         resolution.reason),
                    )
            except pgerrors.UniqueViolation as exc:
                raise BusinessError("environment_name_taken") from exc
            result = {"environment_id": str(env_id), "name": req.name, "role": req.role, "engine": req.engine,
                      "secret_status": resolution.status, "secret_reason": resolution.reason}
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="env_register", key=key, response=result, status_code=201
            )
            return result

    async def env_list(self, p: Principal, channel: Channel) -> dict[str, Any]:
        """The tenant's environments with their real status: secret reference (OpenBao) and live cluster view
        (platform/collector ``/v1/clusters``, matched by environment name). Unreachable sources say so."""
        authorize(p, Action.ENV_READ, channel)
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            cur = await conn.execute(
                "SELECT environment_id, name, role, engine, secret_ref, secret_status, secret_reason, secret_checked_at, "
                "created_at FROM environments ORDER BY role DESC, name"
            )
            envs = await cur.fetchall()
        clusters: dict[str, dict[str, Any]] = {}
        not_configured: dict[str, str] = {}
        cluster_source = {"status": "ok", "reason": None, "observed_at": None}
        if self.collector is None:
            cluster_source = {"status": "unconfigured", "reason": "collector_unconfigured", "observed_at": None}
        else:
            try:
                view = await self.collector.clusters()
                clusters = {str(c.get("cluster_id")): c for c in view["clusters"] if isinstance(c, dict)}
                not_configured = {str(n.get("cluster_id")): str(n.get("reason")) for n in view.get("not_configured") or []
                                  if isinstance(n, dict)}
                cluster_source["observed_at"] = view.get("observed_at")
            except UpstreamError as exc:
                cluster_source = {"status": "error", "reason": exc.reason, "observed_at": None}
        out = []
        for e in envs:
            c = clusters.get(e["name"])
            if c is not None:
                cluster = {"status": c.get("status"), "provider": c.get("provider"), "context": c.get("context"),
                           "server_version": c.get("server_version"), "nodes": len(c.get("nodes") or []),
                           "error": c.get("error")}
            elif e["name"] in not_configured:
                cluster = {"status": "not_configured", "reason": not_configured[e["name"]]}
            else:
                cluster = {"status": "unknown", "reason": cluster_source["reason"] or "not_in_collector_registry"}
            out.append({
                "environment_id": str(e["environment_id"]),
                "name": e["name"],
                "role": e["role"],
                "engine": e["engine"],
                "secret_ref": e["secret_ref"],
                "secret_status": e["secret_status"],
                "secret_reason": e["secret_reason"],
                "secret_checked_at": _iso(e["secret_checked_at"]),
                "cluster": cluster,
                "created_at": _iso(e["created_at"]),
            })
        return {"environments": out, "cluster_source": cluster_source}

    async def plan_list(self, p: Principal, channel: Channel, limit: int = 100) -> dict[str, Any]:
        """The tenant's plans, newest first, with state, environments and the pending approval (if any)."""
        authorize(p, Action.PLAN_READ, channel)
        limit = max(1, min(limit, 200))
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            cur = await conn.execute(
                """SELECT p.plan_id, p.workload, p.state, p.state_reason, p.rehearsal, p.revision, p.created_at,
                          p.updated_at, p.approval_stage, p.approval_request_id, p.approval_request_expires_at,
                          p.source_environment_id, s.name AS source_name, p.target_environment_id,
                          t.name AS target_name, c.status AS change_status, c.change_id,
                          EXISTS (SELECT 1 FROM approvals a WHERE a.approval_request_id = p.approval_request_id)
                            AS decided
                     FROM plans p
                     JOIN environments s ON s.environment_id = p.source_environment_id
                     JOIN environments t ON t.environment_id = p.target_environment_id
                     LEFT JOIN changes c ON c.change_id = COALESCE(p.active_change_id, p.last_change_id)
                    ORDER BY p.created_at DESC LIMIT %s""",
                (limit,),
            )
            rows = await cur.fetchall()
        plans = []
        for r in rows:
            pending = None
            if r["approval_request_id"] and not r["decided"]:
                pending = {"stage": r["approval_stage"], "approval_request_id": str(r["approval_request_id"]),
                           "expires_at": _iso(r["approval_request_expires_at"]),
                           "approve_url": self.console_url(r["plan_id"]) + "#approve"}
            plans.append({
                "plan_id": str(r["plan_id"]),
                "workload": r["workload"],
                "state": r["state"],
                "state_reason": r["state_reason"],
                "rehearsal": r["rehearsal"],
                "revision": r["revision"],
                "source": {"environment_id": str(r["source_environment_id"]), "name": r["source_name"]},
                "target": {"environment_id": str(r["target_environment_id"]), "name": r["target_name"]},
                "pending_approval": pending,
                "change": {"change_id": str(r["change_id"]), "status": r["change_status"]} if r["change_id"] else None,
                "created_at": _iso(r["created_at"]),
                "updated_at": _iso(r["updated_at"]),
                "console_url": self.console_url(r["plan_id"]),
            })
        return {"plans": plans}

    async def env_controls(self, p: Principal, channel: Channel, environment_id: UUID) -> dict[str, Any]:
        authorize(p, Action.ENV_READ, channel)
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            cur = await conn.execute("SELECT role FROM environments WHERE environment_id = %s", (environment_id,))
            env = await cur.fetchone()
        if env is None:
            raise NotFoundError("environment")
        side = "src" if env["role"] == "source" else "dst"
        collected_at = datetime.now(UTC).isoformat()
        try:
            raw = await self.executor.controls()
            status, reason = "ok", None
        except ExecutorError as exc:
            raw, status, reason = [], "unavailable", exc.reason
        by_id = {c.get("control_id"): c for c in raw if isinstance(c, dict)}
        ids = sorted({*self.gate_policy.required_controls, *(k for k in by_id if isinstance(k, str))})
        controls = []
        for cid in ids:
            value = by_id.get(cid, {}).get(side)
            controls.append({"control_id": cid, "result": value if value in ("PASS", "FAIL") else "UNKNOWN"})
        return {
            "environment_id": str(environment_id),
            "role": env["role"],
            "status": status,
            "reason": reason,
            "collected_at": collected_at,
            "controls": controls,
        }

    # Plans ----------------------------------------------------------------------------

    async def plan_create(self, p: Principal, channel: Channel, req: PlanCreate, key: str | None) -> dict[str, Any]:
        authorize(p, Action.PLAN_CREATE, channel)
        key = idempotency.validate_key(key)
        if req.source_environment_id == req.target_environment_id:
            raise BusinessError("source_equals_target", status=422)
        digest = idempotency.request_digest({"action": "plan_create", **req.model_dump(mode="json")})
        actor = Actor.of(p)
        await self.refresh_secret_refs(p.tenant_id, p.id, [req.source_environment_id, req.target_environment_id])
        snapshot = await self.collect_snapshot()  # network I/O outside any transaction
        inventory = await self.read_inventory()
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            replay = await idempotency.claim(
                conn, tenant_id=p.tenant_id, action="plan_create", key=key, digest=digest, principal_id=p.id
            )
            if replay is not None:
                return replay
            cur = await conn.execute(
                "SELECT environment_id, role FROM environments WHERE environment_id = ANY(%s)",
                ([req.source_environment_id, req.target_environment_id],),
            )
            roles = {r["environment_id"]: r["role"] for r in await cur.fetchall()}
            if req.source_environment_id not in roles or req.target_environment_id not in roles:
                raise NotFoundError("environment")
            if roles[req.source_environment_id] != "source" or roles[req.target_environment_id] != "target":
                raise BusinessError("environment_role_mismatch", status=422)
            plan_id = uuid.uuid4()
            await conn.execute(
                """INSERT INTO plans (plan_id, tenant_id, workload, source_environment_id, target_environment_id,
                                      state, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    plan_id,
                    p.tenant_id,
                    req.workload,
                    req.source_environment_id,
                    req.target_environment_id,
                    State.COLLECT.value,
                    p.id,
                ),
            )
            await emit(
                conn,
                plan_id=plan_id,
                kind=EventKind.STATE_CHANGED,
                mode=EventMode.LIVE,
                actor=actor,
                data={
                    "from": None,
                    "to": State.COLLECT.value,
                    "trigger": "plan_created",
                    "reason": None,
                },
            )
            plan = await self._apply_snapshot(conn, plan_id, snapshot, actor)
            plan = await self._apply_analysis(conn, plan_id, snapshot, inventory, actor)
            result = {
                "plan_id": str(plan_id),
                "state": plan["state"],
                "state_reason": plan["state_reason"],
                "revision": plan["revision"],
                "rehearsal": plan["rehearsal"],
                "collect": snapshot.summary(),
                "analysis": plan["analysis"],
                "console_url": self.console_url(plan_id),
            }
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="plan_create", key=key, response=result, status_code=201
            )
            return result

    async def collect_snapshot(self) -> Snapshot:
        """Read-only snapshot of Executor controls and readiness (pilot COLLECT)."""
        errors: list[str] = []
        controls: list[dict[str, Any]] | None = None
        readiness: dict[str, Any] | None = None
        try:
            controls = await self.executor.controls()
        except ExecutorError as exc:
            errors.append(exc.reason)
        try:
            readiness = await self.executor.readiness()
        except ExecutorError as exc:
            errors.append(exc.reason)
        if self.gate_controls is not None:
            # Controls from platform/gate are appended as read. A control id reported by both
            # sources is contradictory evidence and the gate treats it as UNKNOWN (fail closed).
            try:
                extra = await self.gate_controls.controls()
                controls = [*(controls or []), *extra] if controls is not None else None
            except UpstreamError as exc:
                errors.append(exc.reason)
        return Snapshot(
            controls=controls, readiness=readiness, errors=tuple(sorted(set(errors))), collected_at=datetime.now(UTC)
        )

    async def refresh_secret_refs(self, tenant_id: str, principal_id: UUID | None, env_ids: list[UUID]) -> None:
        """Re-resolve environments whose secret_ref is not resolved yet (network I/O outside any transaction)."""
        async with self.db.tenant_tx(tenant_id, principal_id) as conn:
            cur = await conn.execute(
                "SELECT environment_id, secret_ref FROM environments WHERE environment_id = ANY(%s) "
                "AND secret_status <> 'resolved'",
                (env_ids,),
            )
            pending = await cur.fetchall()
        for env in pending:
            res = await self.secret_refs.resolve(env["secret_ref"])
            status = "resolved" if res.status == "resolved" else "unverified"  # a vanished ref stays blocked
            async with self.db.tenant_tx(tenant_id, principal_id) as conn:
                await conn.execute(
                    "UPDATE environments SET secret_status = %s, secret_reason = %s, secret_checked_at = now() "
                    "WHERE environment_id = %s",
                    (status, res.reason, env["environment_id"]),
                )

    async def _apply_snapshot(self, conn: Conn, plan_id: UUID, snap: Snapshot, actor: Actor) -> dict[str, Any]:
        """In COLLECT: store a complete snapshot and move to ANALYZE, or stay with the reason.

        Both environments' secret references must resolve in the platform OpenBao first (audit B11): an
        unverified reference keeps the plan in COLLECT (``environment_secret_unverified:<reason>``).
        """
        plan = await lock_plan(conn, plan_id)
        if plan["state"] != State.COLLECT.value:
            return plan
        cur = await conn.execute(
            "SELECT name, secret_status, secret_reason FROM environments WHERE environment_id = ANY(%s) "
            "ORDER BY role DESC",
            ([plan["source_environment_id"], plan["target_environment_id"]],),
        )
        blocked = [e for e in await cur.fetchall() if e["secret_status"] != "resolved"]
        if blocked:
            reason = f"environment_secret_unverified:{blocked[0]['secret_reason'] or 'unknown'}"
            cur = await conn.execute(
                "UPDATE plans SET state_reason = %s, version = version + 1, updated_at = now() "
                "WHERE plan_id = %s RETURNING *",
                (reason, plan_id),
            )
            return await cur.fetchone() or plan
        if snap.errors:
            cur = await conn.execute(
                "UPDATE plans SET state_reason = %s, version = version + 1, updated_at = now() "
                "WHERE plan_id = %s RETURNING *",
                (snap.errors[0], plan_id),
            )
            return await cur.fetchone() or plan
        return await transition(
            conn,
            plan_id,
            Trigger.COLLECTED,
            actor=actor,
            updates={"collect_snapshot": snap.as_json(), "collected_at": snap.collected_at},
            data={"collect": snap.summary()},
        )

    async def read_inventory(self) -> Inventory:
        """Collector inventory (FR-1, FR-2). Unconfigured is reported as such, never filled in."""
        if self.collector is None:
            return Inventory("unconfigured", None, "collector_unconfigured")
        try:
            return Inventory("ok", await self.collector.inventory(), None)
        except UpstreamError as exc:
            # inventory_root_empty: the collector answered, but nothing was mounted to scan. Unavailable, not an
            # outage and never "no gaps": ANALYZE holds with that reason (plan.analysis.inventory.reason).
            return Inventory("unavailable" if exc.reason == ROOT_EMPTY else "error", None, exc.reason)

    async def _apply_analysis(
        self, conn: Conn, plan_id: UUID, snap: Snapshot, inv: Inventory, actor: Actor
    ) -> dict[str, Any]:
        """In ANALYZE: dependency gaps (code_change, unclassified) -> PROPOSE, none -> GATE.

        No inventory (collector not configured, or configured but unreadable) keeps the plan in ANALYZE with
        the reason (``collector_unconfigured``, ``collector_*``): "no gaps" is never assumed (audit A4).
        Control differences are not gaps here: the gate's BLOCK reasons are (S1 -> S2).
        """
        plan = await lock_plan(conn, plan_id)
        if plan["state"] != State.ANALYZE.value:
            return plan
        controls = snap.as_json()["controls"]
        differing = sorted(
            c["control_id"] for c in controls if isinstance(c.get("control_id"), str) and c.get("src") != c.get("dst")
        )
        analysis = {
            "inventory": inv.as_json(),
            "gaps": inv.gaps(),
            "control_differences": differing,
            "analyzed_at": datetime.now(UTC).isoformat(),
        }
        await emit(conn, plan_id=plan_id, kind=EventKind.ANALYSIS_DONE, mode=EventMode.LIVE, actor=actor, data=analysis)
        if inv.status != "ok":
            cur = await conn.execute(
                "UPDATE plans SET state_reason = %s, analysis = %s, analyzed_at = now(), version = version + 1, "
                "updated_at = now() WHERE plan_id = %s RETURNING *",
                (inv.reason, Jsonb(analysis), plan_id),
            )
            return await cur.fetchone() or plan
        trigger = Trigger.ANALYZED_GAPS if analysis["gaps"] else Trigger.ANALYZED_NO_GAPS
        return await transition(
            conn,
            plan_id,
            trigger,
            actor=actor,
            updates={"analysis": analysis, "analyzed_at": datetime.now(UTC)},
            data={"gaps": len(analysis["gaps"]), "inventory": inv.status},
        )

    async def plan_get(self, p: Principal, channel: Channel, plan_id: UUID) -> dict[str, Any]:
        authorize(p, Action.PLAN_READ, channel)
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            return await self._plan_view(conn, plan_id)

    async def _plan_view(self, conn: Conn, plan_id: UUID) -> dict[str, Any]:
        cur = await conn.execute("SELECT * FROM plans WHERE plan_id = %s", (plan_id,))
        plan = await cur.fetchone()
        if plan is None:
            raise NotFoundError("plan")
        cur = await conn.execute("SELECT email FROM principals WHERE id = %s", (plan["created_by"],))
        creator_row = await cur.fetchone()
        creator = creator_row["email"] if creator_row else None
        last_gate = None
        if plan["last_gate_verdict_id"]:
            cur = await conn.execute(
                "SELECT verdict_id, verdict, reasons, controls, readiness, plan_revision, created_at FROM gate_verdicts "
                "WHERE verdict_id = %s",
                (plan["last_gate_verdict_id"],),
            )
            g = await cur.fetchone()
            if g:
                last_gate = {
                    "verdict_id": str(g["verdict_id"]),
                    "verdict": g["verdict"],
                    "reasons": list(g["reasons"]),
                    "controls": g["controls"],
                    "readiness": g["readiness"],
                    "plan_revision": g["plan_revision"],
                    "at": _iso(g["created_at"]),
                }
        awaiting = None
        approval = None
        if plan["approval_request_id"]:
            cur = await conn.execute(
                "SELECT approval_id, expires_at, consumed_at FROM approvals WHERE approval_request_id = %s",
                (plan["approval_request_id"],),
            )
            a = await cur.fetchone()
            if a:
                approval = {
                    "approval_id": str(a["approval_id"]),
                    "expires_at": _iso(a["expires_at"]),
                    "consumed": a["consumed_at"] is not None,
                }
            elif plan["state"] in (State.AWAIT_APPROVAL.value, State.WATCH.value, State.REHEARSE.value):
                awaiting = {
                    "stage": plan["approval_stage"],
                    "approval_request_id": str(plan["approval_request_id"]),
                    "revision": plan["revision"],
                    "gate_verdict_id": _uuid(plan["last_gate_verdict_id"]) if plan["approval_stage"] == "cutover" else None,
                    "artifact_digest": plan["approved_artifact_digest"],
                    "applied_image_digest": plan["applied_image_digest"] if plan["approval_stage"] == "cutover" else None,
                    "bound_run_id": _uuid(plan["last_run_id"]) if plan["approval_stage"] == "failback" else None,
                    "change": None,
                    "expires_at": _iso(plan["approval_request_expires_at"]),
                    "approve_url": self.console_url(plan_id) + "#approve",
                }
        change = None
        change_id = plan["active_change_id"] or plan["last_change_id"]
        if change_id:
            cur = await conn.execute("SELECT * FROM changes WHERE change_id = %s", (change_id,))
            c = await cur.fetchone()
            if c:
                change = {**change_view(c), "active": plan["active_change_id"] is not None}
                if awaiting is not None and plan["approval_stage"] == "change":
                    awaiting["change"] = {k: change[k] for k in (
                        "change_id", "role", "artifact_digest", "diff_digest", "image_repository", "image_digest",
                        "policy_diff_digest", "policy_targets", "sbom_digest", "scan")}
                    awaiting["artifact_digest"] = c["artifact_digest"]
        run = None
        run_id = plan["active_run_id"] or plan["last_run_id"]
        if run_id:
            cur = await conn.execute(
                "SELECT run_id, kind, status, write_pause_ms, epoch, started_at, ended_at FROM runs WHERE run_id = %s",
                (run_id,),
            )
            r = await cur.fetchone()
            cur = await conn.execute(
                "SELECT seq, name, started_at, ms, result FROM run_stages WHERE run_id = %s ORDER BY seq", (run_id,)
            )
            stages = [
                {
                    "seq": s["seq"],
                    "name": s["name"],
                    "started_at": _iso(s["started_at"]),
                    "ms": s["ms"],
                    "result": s["result"],
                }
                for s in await cur.fetchall()
            ]
            run = {
                "run_id": str(run_id),
                "active": plan["active_run_id"] is not None,
                "kind": r["kind"] if r else None,
                # No runs row yet: the Executor accepted the request but has not recorded it.
                "status": r["status"] if r else "UNKNOWN",
                "epoch": r["epoch"] if r else plan["epoch"],
                "write_pause_ms": r["write_pause_ms"] if r else None,
                "write_pause_budget_ms": None,
                "started_at": _iso(r["started_at"]) if r else None,
                "ended_at": _iso(r["ended_at"]) if r else None,
                "stages": stages,
                "receipt": None,
            }
        receipt = None
        if run_id:
            cur = await conn.execute(
                "SELECT receipt_id, run_id, verdict, body, created_at FROM receipts WHERE run_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            )
            rc = await cur.fetchone()
            if rc:
                receipt = {
                    "receipt_id": str(rc["receipt_id"]),
                    "run_id": str(rc["run_id"]),
                    "verdict": rc["verdict"],
                    "at": _iso(rc["created_at"]),
                    "created_at": _iso(rc["created_at"]),
                    "body": receipt_body(rc["body"]),
                }
                if run is not None:
                    run["receipt"] = receipt
        rehearsal = None
        rid = plan["active_rehearsal_id"] or plan["last_rehearsal_id"]
        if rid:
            cur = await conn.execute(
                "SELECT rehearsal_id, attempt, task, trial_id, status, reason, artifact_digest, verifier, created_at, "
                "ended_at FROM rehearsals WHERE rehearsal_id = %s",
                (rid,),
            )
            rh = await cur.fetchone()
            if rh:
                rehearsal = {
                    "rehearsal_id": str(rh["rehearsal_id"]),
                    "active": plan["active_rehearsal_id"] is not None,
                    "attempt": rh["attempt"],
                    "task": rh["task"],
                    "trial_id": rh["trial_id"],
                    "status": rh["status"],
                    "reason": rh["reason"],
                    "artifact_digest": rh["artifact_digest"],
                    "verifier": rh["verifier"],
                    "started_at": _iso(rh["created_at"]),
                    "ended_at": _iso(rh["ended_at"]),
                }
        return {
            "plan_id": str(plan["plan_id"]),
            "workload": plan["workload"],
            "created_by": {"principal": creator},
            "created_at": _iso(plan["created_at"]),
            "state": plan["state"],
            "state_reason": plan["state_reason"],
            "rehearsal": plan["rehearsal"],
            "rehearsal_attempts": {
                "failed": plan["rehearsal_failures"],
                "infra_errors": plan["rehearsal_infra_errors"],
                "max": self.settings.rehearsal_max_attempts,
            },
            "last_rehearsal": rehearsal,
            "analysis": plan["analysis"],
            "collected_at": _iso(plan["collected_at"]),
            "revision": plan["revision"],
            "version": plan["version"],
            "epoch": plan["epoch"],
            "source_environment_id": str(plan["source_environment_id"]),
            "target_environment_id": str(plan["target_environment_id"]),
            "last_gate": last_gate,
            "awaiting_approval": awaiting,
            "approval": approval,
            # `last_run` and `run` are the same object: the active run, else the most recent one.
            "last_run": run,
            "run": run,
            "last_receipt": receipt,
            "change": change,
            "applied_image_digest": plan["applied_image_digest"],
            # D-20 holds are not implemented: say so instead of an empty list that reads as "no holds" (audit 26).
            "security_holds": None,
            "security_holds_reason": "not_implemented:D-20",
            "last_event_id": str(plan["event_seq"]),
            "console_url": self.console_url(plan_id),
            "updated_at": _iso(plan["updated_at"]),
        }

    async def plan_events(
        self, p: Principal, channel: Channel, plan_id: UUID, after: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        authorize(p, Action.PLAN_READ, channel)
        limit = max(1, min(limit, 200))
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            await self.require_plan(conn, plan_id)
            events = await list_events(conn, plan_id, after, limit)
        return {"plan_id": str(plan_id), "events": events, "next_after": events[-1]["id"] if events else str(after)}

    async def require_plan(self, conn: Conn, plan_id: UUID) -> None:
        cur = await conn.execute("SELECT 1 FROM plans WHERE plan_id = %s", (plan_id,))
        if await cur.fetchone() is None:
            raise NotFoundError("plan")

    # Gate -------------------------------------------------------------------------------

    async def gate_run(self, p: Principal, channel: Channel, plan_id: UUID, key: str | None) -> dict[str, Any]:
        authorize(p, Action.GATE_RUN, channel)
        key = idempotency.validate_key(key)
        digest = idempotency.request_digest({"action": "gate_run", "plan_id": str(plan_id)})
        # Cheap pre-check so an obviously wrong state does not call the Executor.
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            cur = await conn.execute(
                "SELECT state, source_environment_id, target_environment_id FROM plans WHERE plan_id = %s", (plan_id,)
            )
            row = await cur.fetchone()
            if row is None:
                raise NotFoundError("plan")
        if row["state"] == State.COLLECT.value:
            await self.refresh_secret_refs(
                p.tenant_id, p.id, [row["source_environment_id"], row["target_environment_id"]]
            )
        snap = await self.collect_snapshot()  # network I/O outside any transaction
        inventory = await self.read_inventory() if row["state"] in (State.COLLECT.value, State.ANALYZE.value) else None
        result = evaluate(snap.controls, snap.readiness, self.gate_policy, snap.errors)
        actor = Actor.of(p)
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            replay = await idempotency.claim(
                conn, tenant_id=p.tenant_id, action="gate_run", key=key, digest=digest, principal_id=p.id
            )
            if replay is not None:
                return replay
            plan = await lock_plan(conn, plan_id)
            state = State(plan["state"])
            if state not in GATE_STATES:
                raise BusinessError("state_mismatch", detail={"state": state.value})
            if state is State.COLLECT:
                plan = await self._apply_snapshot(conn, plan_id, snap, actor)
                state = State(plan["state"])
            if state is State.ANALYZE:
                plan = await self._apply_analysis(
                    conn, plan_id, snap, inventory or Inventory("error", None, "inventory_not_read"), actor
                )
                state = State(plan["state"])
            verdict_id = uuid.uuid4()
            controls = [c.as_dict() for c in result.controls]
            verdict, reasons = result.verdict, list(result.reasons)
            if state is State.WATCH:
                # Control re-test after cutover (FR-12, §8.2 WATCH): the same per-control rules;
                # replication readiness belongs to the Executor's failback, not to this verdict.
                reasons = sorted(r for r in result.reasons if not r.startswith("readiness:"))
                verdict = Verdict.PASS if controls and not reasons else Verdict.BLOCK
            # Display values as read (seq/rows are not rule inputs, so not in input_digest).
            readiness_view = {**snap.as_json()["readiness"], "max_lag_bytes": self.gate_policy.max_lag_bytes}
            await conn.execute(
                """INSERT INTO gate_verdicts (verdict_id, tenant_id, plan_id, plan_revision, verdict, reasons,
                                              controls, readiness, input_digest, rule_version, requested_by, channel,
                                              purpose)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    verdict_id,
                    p.tenant_id,
                    plan_id,
                    plan["revision"],
                    verdict.value,
                    reasons,
                    Jsonb(controls),
                    Jsonb(readiness_view),
                    result.input_digest,
                    result.rule_version,
                    p.id,
                    channel.value,
                    "control_retest" if state is State.WATCH else "gate",
                ),
            )
            await emit(
                conn,
                plan_id=plan_id,
                kind=EventKind.GATE_VERDICT,
                mode=EventMode.LIVE,
                actor=actor,
                data={
                    "verdict_id": str(verdict_id),
                    "verdict": verdict.value,
                    "reasons": reasons,
                    "controls": controls,
                    "readiness": readiness_view,
                    "input_digest": result.input_digest,
                    "rule_version": result.rule_version,
                    "purpose": "control_retest" if state is State.WATCH else "gate",
                },
            )
            common = {"actor": actor, "data": {"verdict_id": str(verdict_id)}}
            if verdict is Verdict.PASS and state in (State.GATE, State.AWAIT_APPROVAL, State.NEEDS_HUMAN):
                plan = await transition(
                    conn,
                    plan_id,
                    Trigger.GATE_PASSED,
                    updates={"last_gate_verdict_id": verdict_id, "approval_requested_by": p.id},
                    approval_ttl_s=self.settings.approval_ttl_s,
                    **common,
                )
            elif state is State.AWAIT_APPROVAL:
                plan = await transition(
                    conn,
                    plan_id,
                    Trigger.GATE_BLOCKED,
                    reason="gate_block",
                    updates={"last_gate_verdict_id": verdict_id},
                    **common,
                )
            elif state is State.WATCH:
                trigger = Trigger.CONTROLS_VERIFIED if verdict is Verdict.PASS else Trigger.CONTROLS_REGRESSED
                plan = await transition(
                    conn,
                    plan_id,
                    trigger,
                    reason=None if verdict is Verdict.PASS else "controls_regressed",
                    updates={"last_gate_verdict_id": verdict_id, "approval_requested_by": p.id},
                    approval_ttl_s=self.settings.approval_ttl_s,
                    **common,
                )
            else:  # BLOCK in GATE / NEEDS_HUMAN, or a plan analysis sent to PROPOSE: no transition
                cur = await conn.execute(
                    """UPDATE plans SET last_gate_verdict_id = %s,
                              state_reason = CASE WHEN %s = 'BLOCK' THEN COALESCE(state_reason, 'gate_block')
                                                  ELSE state_reason END,
                              version = version + 1, updated_at = now()
                        WHERE plan_id = %s AND version = %s RETURNING *""",
                    (verdict_id, verdict.value, plan_id, plan["version"]),
                )
                plan = await cur.fetchone() or plan
            response = {
                "plan_id": str(plan_id),
                "verdict_id": str(verdict_id),
                "verdict": verdict.value,
                "reasons": reasons,
                "controls": controls,
                "readiness": {**readiness_view, "schema_match": _schema_match(readiness_view)},
                "input_digest": result.input_digest,
                "rule_version": result.rule_version,
                "state": plan["state"],
                "approval_request_id": _uuid(plan["approval_request_id"]),
                "console_url": self.console_url(plan_id),
            }
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="gate_run", key=key, response=response, status_code=200
            )
            return response

    async def change_recheck(self, tenant_id: str, plan_id: UUID, requested_by: UUID) -> dict[str, Any] | None:
        """System gate re-check after an approved change was applied (spec r2: REHEARSE -> GATE re-check).

        Same rules as ``gate_run``; recorded as channel ``system``, purpose ``change_recheck``. PASS issues the
        cutover approval request, requested on behalf of the change's requester (so they cannot approve it).
        """
        snap = await self.collect_snapshot()  # network I/O outside any transaction
        result = evaluate(snap.controls, snap.readiness, self.gate_policy, snap.errors)
        async with self.db.tenant_tx(tenant_id) as conn:
            plan = await lock_plan(conn, plan_id)
            if plan["state"] != State.GATE.value:
                return None
            verdict_id = uuid.uuid4()
            controls = [c.as_dict() for c in result.controls]
            readiness_view = {**snap.as_json()["readiness"], "max_lag_bytes": self.gate_policy.max_lag_bytes}
            await conn.execute(
                """INSERT INTO gate_verdicts (verdict_id, tenant_id, plan_id, plan_revision, verdict, reasons,
                                              controls, readiness, input_digest, rule_version, requested_by, channel,
                                              purpose)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, 'system', 'change_recheck')""",
                (verdict_id, tenant_id, plan_id, plan["revision"], result.verdict.value, list(result.reasons),
                 Jsonb(controls), Jsonb(readiness_view), result.input_digest, result.rule_version),
            )
            await emit(
                conn,
                plan_id=plan_id,
                kind=EventKind.GATE_VERDICT,
                mode=EventMode.LIVE,
                actor=SYSTEM,
                data={
                    "verdict_id": str(verdict_id),
                    "verdict": result.verdict.value,
                    "reasons": list(result.reasons),
                    "controls": controls,
                    "readiness": readiness_view,
                    "input_digest": result.input_digest,
                    "rule_version": result.rule_version,
                    "purpose": "change_recheck",
                },
            )
            if result.verdict is Verdict.PASS:
                plan = await transition(
                    conn,
                    plan_id,
                    Trigger.GATE_PASSED,
                    actor=SYSTEM,
                    updates={"last_gate_verdict_id": verdict_id, "approval_requested_by": requested_by},
                    approval_ttl_s=self.settings.approval_ttl_s,
                    data={"verdict_id": str(verdict_id), "purpose": "change_recheck"},
                )
            else:
                cur = await conn.execute(
                    "UPDATE plans SET last_gate_verdict_id = %s, state_reason = 'gate_block', version = version + 1, "
                    "updated_at = now() WHERE plan_id = %s RETURNING *",
                    (verdict_id, plan_id),
                )
                plan = await cur.fetchone() or plan
            return {"verdict_id": str(verdict_id), "verdict": result.verdict.value, "state": plan["state"]}

    # Approvals (console only) -----------------------------------------------------------

    async def approve(
        self, p: Principal, channel: Channel, plan_id: UUID, req: ApprovalDecision, key: str | None
    ) -> dict[str, Any]:
        authorize(p, Action.APPROVE, channel)
        key = idempotency.validate_key(key)
        digest = idempotency.request_digest({"action": "approve", "plan_id": str(plan_id), **req.model_dump(mode="json")})
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            replay = await idempotency.claim(
                conn, tenant_id=p.tenant_id, action="approve", key=key, digest=digest, principal_id=p.id
            )
            if replay is not None:
                return replay
            plan = await lock_plan(conn, plan_id)
            expected_state = {"cutover": State.AWAIT_APPROVAL, "failback": State.WATCH, "change": State.REHEARSE}[req.stage]
            if plan["state"] != expected_state.value:
                raise BusinessError("state_mismatch", detail={"state": plan["state"]})
            if (
                plan["approval_stage"] != req.stage
                or plan["approval_request_id"] != req.approval_request_id
                or plan["revision"] != req.revision
                or (req.gate_verdict_id is not None and req.gate_verdict_id != plan["last_gate_verdict_id"])
            ):
                raise BusinessError("approval_stale")
            gate_verdict_id, artifact_digest, bound_run_id = None, None, None
            change: dict[str, Any] | None = None
            if req.stage == "change":
                # Spec r2: bound to the diff, the image digest and the policy diff of the prepared change.
                if req.change_id is None or req.change_id != plan["active_change_id"]:
                    raise BusinessError("approval_stale", detail={"cause": "change_mismatch"})
                cur = await conn.execute("SELECT * FROM changes WHERE change_id = %s FOR UPDATE", (req.change_id,))
                change = await cur.fetchone()
                if (
                    change is None
                    or change["status"] != "prepared"
                    or change["approval_request_id"] != plan["approval_request_id"]
                    or change["plan_revision"] != plan["revision"]
                ):
                    raise BusinessError("approval_stale", detail={"cause": "change_not_prepared"})
                artifact_digest = change["artifact_digest"]
            elif req.stage == "cutover":
                cur = await conn.execute(
                    "SELECT verdict FROM gate_verdicts WHERE verdict_id = %s", (plan["last_gate_verdict_id"],)
                )
                g = await cur.fetchone()
                if g is None or g["verdict"] != Verdict.PASS.value:
                    raise BusinessError("approval_stale")
                gate_verdict_id, artifact_digest = plan["last_gate_verdict_id"], plan["approved_artifact_digest"]
            else:
                bound_run_id = plan["last_run_id"]  # the cutover run this failback reverses (S4)
                if bound_run_id is None:
                    raise BusinessError("approval_stale", detail={"cause": "no_cutover_run"})
            expires_at = plan["approval_request_expires_at"]
            if expires_at is None or expires_at <= datetime.now(UTC):
                raise BusinessError("approval_stale", detail={"cause": "approval_request_expired"})
            approval_id = uuid.uuid4()
            try:
                async with conn.transaction():
                    # Approver is the session principal. Proposer/requester are set by the DB trigger.
                    await conn.execute(
                        """INSERT INTO approvals (approval_id, tenant_id, plan_id, approval_request_id, stage,
                                                  plan_revision, gate_verdict_id, approver_id, proposer_id,
                                                  requester_id, expires_at, artifact_digest, bound_run_id,
                                                  change_id, diff_digest, image_digest, policy_diff_digest)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            approval_id,
                            p.tenant_id,
                            plan_id,
                            req.approval_request_id,
                            req.stage,
                            req.revision,
                            gate_verdict_id,
                            p.id,
                            p.id,
                            p.id,
                            expires_at,
                            artifact_digest,
                            bound_run_id,
                            change["change_id"] if change else None,
                            change["diff_digest"] if change else None,
                            change["image_digest"] if change else None,
                            change["policy_diff_digest"] if change else None,
                        ),
                    )
                    if change is not None:
                        # The approval starts the T31 apply (pipeline claims 'approved'); it binds this change only.
                        await conn.execute(
                            "UPDATE changes SET status = 'approved', approval_id = %s, approved_at = now() "
                            "WHERE change_id = %s",
                            (approval_id, change["change_id"]),
                        )
                        await conn.execute(
                            "UPDATE approvals SET consumed_at = now(), consumed_by = %s, run_idem_key = %s "
                            "WHERE approval_id = %s",
                            (p.id, f"change-{change['change_id']}", approval_id),
                        )
            except pgerrors.UniqueViolation as exc:
                raise BusinessError("approval_request_used") from exc
            except pgerrors.CheckViolation as exc:
                msg = str(exc.diag.message_primary or "")
                if "separation_of_duties" in msg or "approvals_separation_of_duties" in (exc.diag.constraint_name or ""):
                    raise BusinessError("separation_of_duties", status=403) from exc
                if msg in ("approval_stale", "state_mismatch", "change_not_approved", "approval_request_missing"):
                    raise BusinessError(msg) from exc
                raise BusinessError(msg or "approval_rejected") from exc
            binding = {
                "change_id": _uuid(change["change_id"]) if change else None,
                "diff_digest": change["diff_digest"] if change else None,
                "image_digest": change["image_digest"] if change else None,
                "policy_diff_digest": change["policy_diff_digest"] if change else None,
            }
            await emit(
                conn,
                plan_id=plan_id,
                kind=EventKind.APPROVAL_GRANTED,
                mode=EventMode.LIVE,
                actor=Actor.of(p),
                data={
                    "approval_id": str(approval_id),
                    "approval_request_id": str(req.approval_request_id),
                    "stage": req.stage,
                    "revision": req.revision,
                    "gate_verdict_id": _uuid(gate_verdict_id),
                    "artifact_digest": artifact_digest,
                    "bound_run_id": _uuid(bound_run_id),
                    **binding,
                    "expires_at": expires_at.isoformat(),
                },
            )
            result = {
                "approval_id": str(approval_id),
                "plan_id": str(plan_id),
                "stage": req.stage,
                "plan_revision": req.revision,
                "gate_verdict_id": _uuid(gate_verdict_id),
                "artifact_digest": artifact_digest,
                "bound_run_id": _uuid(bound_run_id),
                **binding,
                "approver": p.email,
                "expires_at": expires_at.isoformat(),
            }
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="approve", key=key, response=result, status_code=201
            )
            return result
