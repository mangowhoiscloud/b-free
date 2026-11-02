"""Executor service: run admission, idempotency, restart reconciliation, readiness, controls.

Restart rule (D-19, P0 has no automatic takeover): when the platform DB holds unresolved
effects (PREPARED, IN_FLIGHT, UNKNOWN) or RUNNING runs, the Executor starts in observe-only
mode. It then
  * marks every IN_FLIGHT effect UNKNOWN with what it can observe read-only (never re-sends),
  * moves RUNNING runs to SAFE_HOLD with a ``safe_hold_on_restart`` stage,
  * reads src/dst with ``executor_observe`` and PgBouncer with ``pgb_stats`` only,
  * refuses new runs (503) until a person resolves the effects (runbook safe-hold.md).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .approvals import ApprovalCheck, PlatformDbApprovals
from .attachment_copy import AttachmentCopier, ObjectStore, copier_from_env
from .chatdb import ChatDb, connect
from .config import Settings
from .controls import ANON_READ, AUTH, UNKNOWN, ControlResult, evaluate_anon_read, evaluate_auth
from .cutover import CUTOVER, CutoverRequest, CutoverRunner
from .ledger import EffectState, PostgresLedger, RunRequest, RunStatus

log = logging.getLogger(__name__)


class ObserveOnly(RuntimeError):
    """Unresolved effects exist. The Executor refuses new runs until a person resolves them."""


class RunInProgress(RuntimeError):
    def __init__(self, run_id: UUID) -> None:
        super().__init__(f"run {run_id} is in progress")
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class CutoverInput:
    plan_id: UUID
    epoch: int
    approval_id: str
    write_pause_budget_ms: int
    kind: str = CUTOVER


class ExecutorService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool: AsyncConnectionPool[AsyncConnection] = AsyncConnectionPool(
            settings.platform_dsn, connection_class=AsyncConnection, min_size=1, max_size=5, open=False
        )
        self.ledger = PostgresLedger(self.pool)
        self.runner = CutoverRunner(settings, self.ledger, self._copier)
        self.approvals = PlatformDbApprovals(self.pool)
        self.observe_only = False
        self.observe_reason: dict[str, Any] = {}
        self._admit = asyncio.Lock()
        self._tasks: dict[UUID, asyncio.Task[Any]] = {}

    def _copier(self, from_side: str, to_side: str) -> AttachmentCopier | None:
        s = self.settings
        if s.object_stores:
            stores = dict(s.object_stores)
            if from_side not in stores or to_side not in stores:
                return None
            return AttachmentCopier(self.pool, ObjectStore(stores[from_side]), ObjectStore(stores[to_side]))
        return copier_from_env(self.pool, from_side, to_side, {"src": s.src_store_id, "dst": s.dst_store_id})

    # ---- lifecycle -----------------------------------------------------------------------

    async def start(self) -> None:
        if self.settings.fault_crash_after:
            log.warning("FAULT INJECTION ENABLED: exit after %s (contract tests only)", self.settings.fault_crash_after)
        await self.pool.open(wait=True, timeout=30)
        orphaned = await self.ledger.running_run_ids()
        unresolved = await self.ledger.unresolved_effects()
        if not orphaned and not unresolved:
            return
        self.observe_only = True
        pgb_observation = await self._observe_pgbouncer()
        marked = 0
        for eff in unresolved:
            if eff["state"] != EffectState.IN_FLIGHT:
                continue
            evidence: dict[str, Any] = {
                "reason": "EXECUTOR_RESTARTED_IN_FLIGHT",
                "rule": "response lost: not re-sent, not assumed applied or not applied",
                "observed_at_restart": datetime.now(UTC).isoformat(),
            }
            if str(eff["resource"]).startswith("pgbouncer/"):
                evidence["pgbouncer"] = pgb_observation
            await self.ledger.effect_transition(eff["effect_id"], EffectState.IN_FLIGHT, EffectState.UNKNOWN, evidence)
            marked += 1
        for run_id in orphaned:
            seq = await self.ledger.next_stage_seq(run_id)
            await self.ledger.record_stage(
                run_id, seq, "safe_hold_on_restart", datetime.now(UTC), 0, "OK",
                {"reason": "EXECUTOR_RESTARTED", "auto_resume": False},
            )
        await self.ledger.hold_orphaned_runs(orphaned)
        self.observe_reason = {
            "orphaned_runs": [str(r) for r in orphaned],
            "unresolved_effects": len(unresolved),
            "in_flight_marked_unknown": marked,
            "pgbouncer": pgb_observation,
        }
        log.warning("observe-only mode: %s", self.observe_reason)

    async def _observe_pgbouncer(self) -> dict[str, Any]:
        """Read-only PgBouncer view per pool (stats user): pool state and admin sessions still
        connected (E6). Never uses the admin credentials."""
        out: dict[str, Any] = {}
        for name, dsn in self.settings.pool_stats_dsns:
            db_name = next((p.db_name for p in self.settings.pools if p.name == name), self.settings.chat_db)
            try:
                conn = await AsyncConnection.connect(dsn, autocommit=True, connect_timeout=3)
            except Exception as exc:  # noqa: BLE001
                out[name] = {"reachable": False, "error": type(exc).__name__}
                continue
            try:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(b"SHOW DATABASES")
                    pool = next((r for r in await cur.fetchall() if r.get("name") == db_name), None)
                    await cur.execute(b"SHOW CLIENTS")
                    admin_clients = [
                        {"addr": r.get("addr"), "port": r.get("port"), "connect_time": str(r.get("connect_time"))}
                        for r in await cur.fetchall()
                        if r.get("user") == "pgb_admin"
                    ]
            finally:
                await conn.close()
            out[name] = {
                "reachable": True,
                "pool_host": None if pool is None else pool.get("host"),
                "pool_paused": None if pool is None else pool.get("paused"),
                "admin_clients": admin_clients,
            }
        # Compose pilot has one pool named "0": keep the flat shape the contract test reads.
        return out["0"] if list(out) == ["0"] else out

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.pool.close()

    # ---- admission -----------------------------------------------------------------------

    async def submit(self, body: CutoverInput, idem_key: str) -> tuple[UUID, bool]:
        """Same key -> same run_id (no second execution). Different input -> IdempotencyConflict."""
        req = RunRequest(plan_id=body.plan_id, epoch=body.epoch, idem_key=idem_key, kind=body.kind)
        async with self._admit:
            existing = await self.ledger.find_run_by_key(idem_key)
            if existing is not None:
                return await self.ledger.create_or_get_run(req)
            if self.observe_only:
                raise ObserveOnly(str(self.observe_reason))
            running = await self.ledger.running_run_ids()
            if running:
                raise RunInProgress(running[0])
            # A2: verify the approval against the platform's record before a run row exists.
            approval = await self.approvals.verify(
                ApprovalCheck(approval_id=body.approval_id, plan_id=body.plan_id, kind=body.kind, idem_key=idem_key)
            )
            run_id, created = await self.ledger.create_or_get_run(req)
            if created:
                creq = CutoverRequest(
                    run_id=run_id,
                    plan_id=body.plan_id,
                    epoch=body.epoch,
                    approval_id=body.approval_id,
                    write_pause_budget_ms=body.write_pause_budget_ms,
                    kind=body.kind,
                    approval=approval,
                )
                self._tasks[run_id] = asyncio.create_task(self._execute(creq), name=f"{body.kind}-{run_id}")
            return run_id, created

    async def _execute(self, req: CutoverRequest) -> None:
        try:
            outcome = await self.runner.run(req)
            log.info("%s run %s: %s write_pause_ms=%s", req.kind, req.run_id, outcome.status, outcome.write_pause_ms)
            if outcome.status == RunStatus.SAFE_HOLD:
                self.observe_only = True
                self.observe_reason = {"safe_hold_run": str(req.run_id), "reason": outcome.reason}
        except Exception:
            # The runner records ABORT/SAFE_HOLD itself; reaching here means the ledger is unreachable.
            log.exception("run %s: ledger failure, entering observe-only mode", req.run_id)
            self.observe_only = True
            self.observe_reason = {"ledger_failure_run": str(req.run_id)}
        finally:
            self._tasks.pop(req.run_id, None)

    # ---- reads ---------------------------------------------------------------------------

    def _read_dsn(self, side: str) -> str:
        s = self.settings
        if self.observe_only:
            return s.src_observe_dsn if side == "src" else s.dst_observe_dsn
        return s.src_dsn if side == "src" else s.dst_dsn

    async def _open(self, side: str) -> ChatDb | None:
        try:
            return ChatDb(await connect(self._read_dsn(side), timeout_s=3), side)
        except Exception as exc:  # noqa: BLE001 - an unreachable side is reported as null/UNKNOWN
            log.warning("%s unreachable: %s", side, type(exc).__name__)
            return None

    async def readiness(self) -> dict[str, Any]:
        """Replication readiness of the direction that is currently active.

        forward: dst subscription bravo_fwd_sub enabled -> lag of slot bravo_fwd_sub on src
        reverse: src subscription bravo_rev_sub enabled -> lag of slot bravo_rev_sub on dst
        none:    neither enabled (after failback, or before setup) -> lag_bytes null
        """
        s = self.settings
        src = await self._open("src")
        dst = await self._open("dst")
        out: dict[str, Any] = {
            "lag_bytes": None,
            "sub_state": "unknown",
            "direction": "unknown",
            "schema_hash": {"src": None, "dst": None},
            "seq": {"src": None, "dst": None},
            "rows": {"src": None, "dst": None},
        }
        try:
            if src is not None and dst is not None:
                fwd = await dst.subscription(s.fwd_sub)
                rev = await src.subscription(s.rev_sub)
                if fwd.exists and fwd.enabled:
                    out["direction"], sub, lag_db, slot = "forward", fwd, src, s.fwd_slot
                elif rev.exists and rev.enabled:
                    out["direction"], sub, lag_db, slot = "reverse", rev, dst, s.rev_sub
                else:
                    out["direction"], sub, lag_db, slot = "none", None, None, None
                if sub is None or lag_db is None or slot is None:
                    out["sub_state"] = "none"
                else:
                    out["sub_state"] = "streaming" if sub.streaming else "enabled_not_running"
                    out["lag_bytes"] = await lag_db.slot_lag_bytes(slot)
            for side, db in (("src", src), ("dst", dst)):
                if db is None:
                    continue
                out["schema_hash"][side] = await db.schema_hash()
                out["seq"][side] = await db.sequence_last("messages_id_seq")
                out["rows"][side] = await db.row_count("messages")
        finally:
            for db in (src, dst):
                if db is not None:
                    await db.close()
        return out

    async def controls(self) -> list[dict[str, str]]:
        s = self.settings
        anon: dict[str, str] = {}
        auth: dict[str, str] = {}
        for side in ("src", "dst"):
            db = await self._open(side)
            if db is None:
                anon[side] = UNKNOWN
                auth[side] = UNKNOWN
                continue
            try:
                try:
                    anon[side] = evaluate_anon_read(await db.public_can_select("messages"))
                except Exception:  # noqa: BLE001
                    anon[side] = UNKNOWN
                try:
                    settings = await db.settings(("password_encryption",))
                    verdict, _evidence = evaluate_auth(
                        password_encryption=settings.get("password_encryption"),
                        role_has_password=await db.role_has_password(s.app_role),
                        hba_rules=await db.hba_rules(),
                        app_role=s.app_role,
                        database=s.chat_db,
                    )
                    auth[side] = verdict
                except Exception:  # noqa: BLE001
                    auth[side] = UNKNOWN
            finally:
                await db.close()
        return [
            ControlResult(ANON_READ[0], ANON_READ[1], anon["src"], anon["dst"]).as_dict(),
            ControlResult(AUTH[0], AUTH[1], auth["src"], auth["dst"]).as_dict(),
        ]
