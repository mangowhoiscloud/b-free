"""Spec r2 change path (§8.2 REHEARSE row, T10, T31): change row, change approval request, apply watch.

A passing rehearsal opens a change: the bound values (artifact, diff and policy-diff digests, policy targets)
are computed here from the frozen artifact. The T31 pipeline (platform/apply, role ``bravo_apply``) prepares a
candidate (image digest, SBOM, scan) and, after the console ``change`` approval, applies it to the destination.
It writes only ``changes`` / ``change_stages`` (migration 0004). This watcher turns those rows into plan events
and owns every plan transition: prepared -> change approval request; applied with the approved digest ->
REHEARSE -> GATE and a system gate re-check; refused / failed -> stays in REHEARSE with the reason.
No LLM (NFR-5).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.types.json import Jsonb

from . import idempotency
from .auth import Action, Channel, Principal, authorize
from .config import Settings
from .db import Conn, Database
from .errors import BusinessError, NotFoundError
from .events import SYSTEM, Actor, EventKind, EventMode, emit
from .statemachine import State, Trigger, lock_plan, request_change_approval, transition

if TYPE_CHECKING:
    from .service import Services

LOG = logging.getLogger("bravo_api.changes")
CHANGE_ROLES = frozenset({"governance", "app_migration", "ci"})
OPEN = ("requested", "preparing", "prepared", "approved", "applying")
TERMINAL_BAD = ("refused", "failed", "prepare_failed")


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def bound_values(artifact: dict[str, Any] | None) -> dict[str, Any]:
    """Same formulas as platform/apply ``digests`` (the worker recomputes and refuses on mismatch)."""
    art = artifact if isinstance(artifact, dict) else {}
    diff = art.get("diff")
    diff = diff if isinstance(diff, str) and diff else None
    items = art.get("translations")
    items = items if isinstance(items, list) and items else None
    targets = []
    for t in items or []:
        target = t.get("target") if isinstance(t, dict) else None
        if isinstance(target, dict):
            targets.append({"type": str(target.get("type")), "name": str(target.get("name"))})
    return {
        "diff_digest": _sha(diff.encode("utf-8")) if diff is not None else None,
        "policy_diff_digest": _sha(_canonical(items)) if items is not None else None,
        "policy_targets": sorted(targets, key=lambda x: (x["type"], x["name"])),
    }


def change_view(c: dict[str, Any], stages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    def iso(v: Any) -> str | None:
        return v.isoformat() if v else None

    scan = c.get("scan") or None
    out = {
        "change_id": str(c["change_id"]),
        "status": c["status"],
        "reason": c["reason"],
        "role": c["role"],
        "rehearsal_id": str(c["rehearsal_id"]),
        "plan_revision": c["plan_revision"],
        "artifact_digest": c["artifact_digest"],
        "diff_digest": c["diff_digest"],
        "policy_diff_digest": c["policy_diff_digest"],
        "policy_targets": c["policy_targets"],
        "image_repository": c["image_repository"],
        "image_digest": c["image_digest"],
        "sbom_digest": c["sbom_digest"],
        "scan": {k: scan.get(k) for k in ("scanner", "version", "db", "counts", "policy") if k in scan} if scan else None,
        "build": c["build"],
        "signature": c["signature"],
        "deployed_digest": c["deployed_digest"],
        "policies_applied": c["policies_applied"],
        "gate_recheck": c["gate_recheck"],
        "approval_request_id": str(c["approval_request_id"]) if c["approval_request_id"] else None,
        "approval_id": str(c["approval_id"]) if c["approval_id"] else None,
        "created_at": iso(c["created_at"]),
        "prepared_at": iso(c["prepared_at"]),
        "approved_at": iso(c["approved_at"]),
        "ended_at": iso(c["ended_at"]),
    }
    if stages is not None:
        out["stages"] = [stage_view(s) for s in stages]
    return out


def stage_view(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": s["seq"],
        "phase": s["phase"],
        "name": s["name"],
        "started_at": s["started_at"].isoformat(),
        "ms": s["ms"],
        "result": s["result"],
        "detail": s["detail"],
    }


async def open_change(conn: Conn, plan: dict[str, Any], rehearsal: dict[str, Any], artifact: Any, artifact_digest: str,
                      *, actor: Actor) -> dict[str, Any]:
    """Create the change for a passing rehearsal (status ``requested``) and point the plan at it."""
    if rehearsal["role"] not in CHANGE_ROLES:
        raise BusinessError("change_role_not_allowed", detail={"role": rehearsal["role"]})
    bv = bound_values(artifact if isinstance(artifact, dict) else None)
    if bv["diff_digest"] is None and bv["policy_diff_digest"] is None:
        # Nothing to apply: the proposal carries neither a diff nor translations. Recorded, not invented.
        bv_reason = "change_without_diff_or_policy"
    else:
        bv_reason = None
    change_id = uuid.uuid4()
    await conn.execute(
        """INSERT INTO changes (change_id, tenant_id, plan_id, rehearsal_id, plan_revision, target_environment_id, role,
                                artifact_digest, diff_digest, policy_diff_digest, policy_targets, status, reason,
                                requested_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'requested', %s, %s)""",
        (change_id, plan["tenant_id"], plan["plan_id"], rehearsal["rehearsal_id"], plan["revision"],
         plan["target_environment_id"], rehearsal["role"], artifact_digest, bv["diff_digest"],
         bv["policy_diff_digest"], Jsonb(bv["policy_targets"]), bv_reason, rehearsal["requested_by"]),
    )
    await conn.execute(
        "UPDATE plans SET active_change_id = %s, version = version + 1, updated_at = now() WHERE plan_id = %s",
        (change_id, plan["plan_id"]),
    )
    await emit(
        conn,
        plan_id=plan["plan_id"],
        kind=EventKind.CHANGE_REQUESTED,
        mode=EventMode.LIVE,
        actor=actor,
        data={
            "change_id": str(change_id),
            "rehearsal_id": str(rehearsal["rehearsal_id"]),
            "role": rehearsal["role"],
            "plan_revision": plan["revision"],
            "artifact_digest": artifact_digest,
            **bv,
        },
    )
    return {"change_id": change_id, **bv}


class ChangeService:
    """Console reads and the operator's re-request. Approval itself is ``Services.approve`` (stage change)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def list(self, p: Principal, channel: Channel, plan_id: UUID) -> dict[str, Any]:
        authorize(p, Action.PLAN_READ, channel)
        async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
            cur = await conn.execute("SELECT 1 FROM plans WHERE plan_id = %s", (plan_id,))
            if await cur.fetchone() is None:
                raise NotFoundError("plan")
            cur = await conn.execute("SELECT * FROM changes WHERE plan_id = %s ORDER BY created_at DESC", (plan_id,))
            rows = await cur.fetchall()
        return {"plan_id": str(plan_id), "changes": [change_view(r) for r in rows]}

    async def get(self, p: Principal, channel: Channel, plan_id: UUID, change_id: UUID) -> dict[str, Any]:
        authorize(p, Action.PLAN_READ, channel)
        async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
            cur = await conn.execute("SELECT * FROM changes WHERE plan_id = %s AND change_id = %s", (plan_id, change_id))
            row = await cur.fetchone()
            if row is None:
                raise NotFoundError("change")
            cur = await conn.execute("SELECT * FROM change_stages WHERE change_id = %s ORDER BY seq", (change_id,))
            stages = await cur.fetchall()
        return {"plan_id": str(plan_id), **change_view(row, stages)}

    async def request(self, p: Principal, channel: Channel, plan_id: UUID, key: str | None) -> dict[str, Any]:
        """Open a new change for the last passing rehearsal after the previous one ended without applying."""
        authorize(p, Action.CHANGE_REQUEST, channel)
        key = idempotency.validate_key(key)
        digest = idempotency.request_digest({"action": "change_request", "plan_id": str(plan_id)})
        async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
            replay = await idempotency.claim(
                conn, tenant_id=p.tenant_id, action="change_request", key=key, digest=digest, principal_id=p.id
            )
            if replay is not None:
                return replay
            plan = await lock_plan(conn, plan_id)
            if plan["state"] != State.REHEARSE.value:
                raise BusinessError("state_mismatch", detail={"state": plan["state"]})
            if plan["active_change_id"] is not None:
                raise BusinessError("change_active", detail={"change_id": str(plan["active_change_id"])})
            # The newest PASS whose last change ended without applying (refused, failed, prepare_failed). With roles
            # side by side this is not necessarily plan.last_rehearsal_id.
            cur = await conn.execute(
                """SELECT r.* FROM rehearsals r
                    WHERE r.plan_id = %s AND r.status = 'PASS'
                      AND EXISTS (SELECT 1 FROM changes c WHERE c.rehearsal_id = r.rehearsal_id)
                      AND NOT EXISTS (SELECT 1 FROM changes c WHERE c.rehearsal_id = r.rehearsal_id
                                        AND c.status IN ('requested', 'preparing', 'prepared', 'approved', 'applying',
                                                         'applied'))
                    ORDER BY r.ended_at DESC NULLS LAST LIMIT 1""",
                (plan_id,),
            )
            reh = await cur.fetchone()
            if reh is None or reh["artifact"] is None or _sha(_canonical(reh["artifact"])) != reh["artifact_digest"]:
                raise BusinessError("rehearsal_artifact_missing")
            await conn.execute(
                "UPDATE plans SET revision = revision + 1, approved_artifact_digest = %s, version = version + 1, "
                "updated_at = now() WHERE plan_id = %s",
                (reh["artifact_digest"], plan_id),
            )
            plan = await lock_plan(conn, plan_id)
            # The person asking now is the requester of this change (separation of duties applies to them).
            opened = await open_change(conn, plan, {**reh, "requested_by": p.id}, reh["artifact"],
                                       reh["artifact_digest"], actor=Actor.of(p))
            result = {"plan_id": str(plan_id), "change_id": str(opened["change_id"]), "status": "requested",
                      "diff_digest": opened["diff_digest"], "policy_diff_digest": opened["policy_diff_digest"]}
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="change_request", key=key, response=result, status_code=201
            )
            return result


class ChangeWatcher:
    """Polls changes whose plan still points at them and settles their progress (any worker wrote it)."""

    def __init__(self, db: Database, services: Services, settings: Settings) -> None:
        self._db = db
        self._services = services
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="change-watch")

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
            except Exception:  # keep watching; the change rows are the source of truth
                LOG.exception("change watcher poll failed")
            await asyncio.sleep(self._settings.change_poll_interval_s)

    async def poll_once(self) -> int:
        async with self._db.tenant_tx("") as conn:
            cur = await conn.execute("SELECT tenant_id, plan_id, change_id FROM bravo_inflight_changes()")
            items = await cur.fetchall()
        for it in items:
            recheck = None
            async with self._db.tenant_tx(it["tenant_id"]) as conn:
                recheck = await self._apply(conn, it["plan_id"], it["change_id"])
            if recheck is not None:
                await self._services.change_recheck(it["tenant_id"], it["plan_id"], recheck)
        return len(items)

    async def _apply(self, conn: Conn, plan_id: UUID, change_id: UUID) -> UUID | None:
        """One settle step. Returns the requester to run the system gate re-check for, else None."""
        plan = await lock_plan(conn, plan_id)
        cur = await conn.execute("SELECT * FROM changes WHERE change_id = %s FOR UPDATE", (change_id,))
        c = await cur.fetchone()
        if c is None or plan["active_change_id"] != change_id:
            return None
        cur = await conn.execute(
            "SELECT * FROM change_stages WHERE change_id = %s AND seq > %s ORDER BY seq", (change_id, c["stage_seq"])
        )
        stages = await cur.fetchall()
        for s in stages:
            await emit(conn, plan_id=plan_id, kind=EventKind.CHANGE_STAGE, mode=EventMode.LIVE, actor=SYSTEM,
                       data={"change_id": str(change_id), **stage_view(s)})
        if stages:
            await conn.execute("UPDATE changes SET stage_seq = %s WHERE change_id = %s", (stages[-1]["seq"], change_id))
        status = c["status"]
        if plan["state"] != State.REHEARSE.value:
            return None
        if status == "prepared" and c["approval_request_id"] is None:
            await emit(conn, plan_id=plan_id, kind=EventKind.CHANGE_PREPARED, mode=EventMode.LIVE, actor=SYSTEM,
                       data={k: v for k, v in change_view(c).items()
                             if k in ("change_id", "image_repository", "image_digest", "sbom_digest", "scan", "build",
                                      "diff_digest", "policy_diff_digest", "policy_targets", "artifact_digest")})
            await request_change_approval(conn, plan_id, c, ttl_s=self._settings.approval_ttl_s, actor=SYSTEM)
            return None
        if status == "applied":
            cur = await conn.execute(
                "SELECT image_digest, policy_diff_digest FROM approvals WHERE approval_id = %s AND stage = 'change' "
                "AND change_id = %s",
                (c["approval_id"], change_id),
            )
            a = await cur.fetchone()
            # Defence in depth: the DB constraint already ties deployed to the change's digest; check the approval too.
            if a is None or (a["image_digest"] is not None and c["deployed_digest"] != a["image_digest"]):
                await self._end(conn, plan, c, "change_refused", "deployed_digest_mismatch", EventKind.CHANGE_REFUSED)
                return None
            await emit(conn, plan_id=plan_id, kind=EventKind.CHANGE_APPLIED, mode=EventMode.LIVE, actor=SYSTEM,
                       data={"change_id": str(change_id), "approval_id": str(c["approval_id"]),
                             "approved_image_digest": a["image_digest"], "deployed_digest": c["deployed_digest"],
                             "policies_applied": c["policies_applied"], "gate_recheck": c["gate_recheck"],
                             "signature": c["signature"]})
            # A policy-only change deploys no image: keep the image an earlier change of this plan deployed.
            applied_image = c["deployed_digest"] or plan["applied_image_digest"]
            from .rehearsal import open_queued_change, open_rehearsals  # rehearsal imports this module

            others = await open_rehearsals(conn, plan_id)
            cur = await conn.execute(
                "UPDATE plans SET active_change_id = NULL, last_change_id = %s, applied_image_digest = %s, "
                "version = version + 1, updated_at = now() WHERE plan_id = %s RETURNING *",
                (change_id, applied_image, plan_id),
            )
            plan = await cur.fetchone()
            opened = await open_queued_change(conn, plan)
            if opened is not None or others:
                # Another role's change is next, or another role still rehearses: the gate re-check waits for it.
                await conn.execute(
                    "UPDATE plans SET state_reason = %s, version = version + 1, updated_at = now() WHERE plan_id = %s",
                    ("change_applied:next_change" if opened is not None else "change_applied:rehearsal_running",
                     plan_id),
                )
                return None
            await transition(
                conn,
                plan_id,
                Trigger.CHANGE_APPLIED,
                actor=SYSTEM,
                reason="change_applied",
                updates={"active_change_id": None, "last_change_id": change_id, "applied_image_digest": applied_image},
                data={"change_id": str(change_id), "deployed_digest": c["deployed_digest"]},
            )
            return c["requested_by"]
        if status == "prepare_failed":
            await self._end(conn, plan, c, "change_prepare_failed", c["reason"], EventKind.CHANGE_PREPARE_FAILED)
        elif status in ("refused", "failed"):
            kind = EventKind.CHANGE_REFUSED if status == "refused" else EventKind.CHANGE_FAILED
            await self._end(conn, plan, c, f"change_{status}", c["reason"], kind)
        return None

    async def _end(self, conn: Conn, plan: dict[str, Any], c: dict[str, Any], prefix: str, reason: str | None,
                   kind: EventKind) -> None:
        await emit(conn, plan_id=plan["plan_id"], kind=kind, mode=EventMode.LIVE, actor=SYSTEM,
                   data={"change_id": str(c["change_id"]), "status": c["status"], "reason": reason})
        await conn.execute(
            """UPDATE plans SET active_change_id = NULL, last_change_id = %s, state_reason = %s,
                      approval_request_id = CASE WHEN approval_stage = 'change' THEN NULL ELSE approval_request_id END,
                      approval_request_expires_at = CASE WHEN approval_stage = 'change' THEN NULL
                                                         ELSE approval_request_expires_at END,
                      approval_stage = CASE WHEN approval_stage = 'change' THEN NULL ELSE approval_stage END,
                      version = version + 1, updated_at = now()
                WHERE plan_id = %s""",
            (c["change_id"], f"{prefix}:{reason or 'unknown'}", plan["plan_id"]),
        )
        # A queued PASS of another role does not wait behind a change that ended without applying.
        from .rehearsal import open_queued_change

        cur = await conn.execute("SELECT * FROM plans WHERE plan_id = %s", (plan["plan_id"],))
        fresh = await cur.fetchone()
        if fresh is not None and fresh["state"] == State.REHEARSE.value:
            await open_queued_change(conn, fresh)
