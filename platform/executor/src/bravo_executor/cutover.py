"""Cutover (§8.6, 03-migration.md 4.2) and failback (FR-11, 03-migration.md 4.3).

Both directions share one write-pause protocol. ``origin`` is the side that takes writes
when the run starts, ``target`` the side that takes them when it ends.

Cutover  (origin=src, target=dst)                Failback (origin=dst, target=src)
  1  lag, epoch fence on both sides                1  reverse lag, epoch fence, approval recorded
  2-3 PgBouncer PAUSE (timer starts)               2  PgBouncer PAUSE (timer starts)
  4  src writer: NOLOGIN, REVOKE INSERT,           2  dst writer: NOLOGIN, REVOKE INSERT,
     terminate sessions, prove login rejected         terminate sessions, prove login rejected
  5  barrier on src, arrival on dst, compare,      3-4 barrier on dst, arrival on src, compare,
     raise dst sequences, receipt (before writes)     raise src sequences, receipt
  6a forward apply off, reverse subscription on    6  reverse subscription off (apply stopped)
  6b commit point; drop forward slot (src)         6  commit point; drop reverse slot (dst)
  7  dst writer GRANT + LOGIN, pool -> dst,        6  src writer GRANT + LOGIN, pool -> src,
     RELOAD, RESUME                                   RELOAD, RESUME
  8  business write through PgBouncer (timer stops on its ACK; it must be served by the target)

Failure before the commit point -> ABORT_PRE_WRITE: undo only what this run changed, in
reverse order, each undo recorded as its own effect. Failure after it -> SAFE_HOLD; nothing
is resumed automatically.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
from opentelemetry import trace
from psycopg import AsyncConnection

from .attachment_copy import AttachmentCopier
from .chatdb import DIGEST_TABLES, SEQUENCES, ChatDb, connect, wait_until
from .config import Resources, Settings
from .fence import read_fence
from .hashing import HASH_RULE, TableDigest
from .ledger import Ledger, ResourceBusy, RunStatus
from .pgbouncer import PgBouncerAdmin, read_database_target, rewrite_database_target, write_ini_atomically
from .stages import EffectRunner, Observation, StageRecorder, StepFailed, error_evidence

CUTOVER = "cutover"
FAILBACK = "failback"


@dataclass(frozen=True, slots=True)
class CutoverRequest:
    run_id: UUID
    plan_id: UUID
    epoch: int
    approval_id: str
    write_pause_budget_ms: int
    kind: str = CUTOVER


@dataclass(slots=True)
class Applied:
    """External changes this run issued (confirmed or not). Drives ABORT_PRE_WRITE restoration."""

    paused: bool = False
    writer_login_closed: str | None = None
    writer_insert_revoked: str | None = None
    subs_disabled: list[tuple[str, str]] = field(default_factory=list)
    subs_enabled: list[tuple[str, str]] = field(default_factory=list)
    attachments_prior: dict[str, dict[str, Any]] = field(default_factory=dict)  # side -> state before freeze


@dataclass(slots=True)
class RunOutcome:
    status: str
    write_pause_ms: int | None
    stages_ms: dict[str, int] = field(default_factory=dict)
    reason: str | None = None


def _target_host(target: str) -> str | None:
    for part in target.split():
        if part.startswith("host="):
            return part.split("=", 1)[1]
    return None


def instance_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Sides:
    """Open control sessions for one run."""

    def __init__(self, src: ChatDb, dst: ChatDb, admin: PgBouncerAdmin) -> None:
        self.db = {"src": src, "dst": dst}
        self.admin = admin

    async def close(self) -> None:
        for closer in (*self.db.values(), self.admin):
            try:
                await closer.close()
            except Exception:  # noqa: BLE001, S110 - best-effort close of a control session
                pass


CopierFactory = Callable[[str, str], "AttachmentCopier | None"]


class CutoverRunner:
    def __init__(self, settings: Settings, ledger: Ledger, copier_factory: CopierFactory | None = None) -> None:
        self._s = settings
        self._ledger = ledger
        self._copier_factory = copier_factory
        self._clock_ns = time.monotonic_ns
        self._owner = instance_owner()

    async def run(self, req: CutoverRequest) -> RunOutcome:
        tracer = trace.get_tracer("bravo_executor")
        with tracer.start_as_current_span(f"{req.kind}.run") as span:
            span.set_attribute("bravo.run_id", str(req.run_id))
            span.set_attribute("bravo.plan_id", str(req.plan_id))
            span.set_attribute("bravo.epoch", req.epoch)
            span.set_attribute("bravo.direction", req.kind)
            outcome = await self._run(req)
            span.set_attribute("bravo.status", outcome.status)
            if outcome.write_pause_ms is not None:
                span.set_attribute("bravo.write_pause_ms", outcome.write_pause_ms)
            await self._ledger.finish_run(req.run_id, outcome.status, outcome.write_pause_ms)
            await self._ledger.release_locks_if_resolved(req.run_id)
            return outcome

    # ---------------------------------------------------------------------------------------

    async def _run(self, req: CutoverRequest) -> RunOutcome:
        s = self._s
        cutover = req.kind == CUTOVER
        origin, target = ("src", "dst") if cutover else ("dst", "src")
        prefix = "" if cutover else f"{FAILBACK}."
        rec = StageRecorder(self._ledger, req.run_id)
        fx = EffectRunner(self._ledger, req.run_id, req.epoch, crash_after=s.fault_crash_after)
        applied = Applied()
        committed = False
        pause_start_ns: int | None = None
        sides: Sides | None = None

        def name(cutover_name: str, failback_name: str) -> str:
            return cutover_name if cutover else prefix + failback_name

        try:
            async with rec.stage(name("1_preflight", "1_preflight")) as st:
                st.detail.update({"approval_id": req.approval_id, "write_pause_budget_ms": req.write_pause_budget_ms,
                                  "direction": req.kind})
                try:
                    await self._ledger.reserve_locks(
                        req.run_id, Resources.lock_set(s), self._owner, req.epoch, s.lock_lease_s
                    )
                except ResourceBusy as exc:
                    raise StepFailed("RESOURCE_BUSY", {"held": exc.held}) from exc
                st.detail["locks"] = len(Resources.lock_set(s))
                sides = Sides(
                    ChatDb(await connect(s.src_dsn), "src"),
                    ChatDb(await connect(s.dst_dsn), "dst"),
                    await PgBouncerAdmin.connect(s.pgb_admin_dsn),
                )
                await self._preflight(req, st.detail, sides, origin, target)
                for side in ("src", "dst"):
                    await self._touch_fence(fx, sides.db[side], req.epoch)

            async with rec.stage(name("1b_freeze_attachments", "1b_freeze_attachments")) as st:
                freeze_ns = await self._freeze_attachments(req, fx, sides, origin, target, applied, st.detail)
                # Final delta copy under freeze, before the write stop (does not extend write_pause_ms).
                st.detail["objects"] = await self._copy_objects(sides.db[origin], origin, target)

            async with rec.stage(name("2_3_pause", "2_pause")) as st:
                long_tx = await sides.db[origin].long_transactions(s.app_role, s.long_tx_max_s)
                st.detail["long_transactions"] = long_tx
                if long_tx:
                    raise StepFailed("LONG_TRANSACTION", {"count": long_tx})
                pause_start_ns = self._clock_ns()
                applied.paused = True
                await self._pause(fx, sides.admin)

            async with rec.stage(name("4_fence_src", "2_fence_dst")) as st:
                await self._close_writer(req, fx, sides.db[origin], applied, st.detail)

            async with rec.stage(name("5_barrier_compare", "3_4_barrier_compare")) as st:
                receiving_side, receiving_sub = (("dst", s.fwd_sub) if cutover else ("src", s.rev_sub))
                receipt = await self._barrier_and_compare(
                    req, fx, sides.db[origin], sides.db[target], receiving_side, receiving_sub, rec,
                    pause_start_ns, st.detail,
                )
                st.detail["verdict"] = receipt["verdict"]
                st.detail["receipt_id"] = receipt["receipt_id"]
                if receipt["verdict"] != "MATCH":
                    raise StepFailed("RECEIPT_MISMATCH", {"receipt_id": receipt["receipt_id"]})
                elapsed_ms = (self._clock_ns() - pause_start_ns) // 1_000_000
                if elapsed_ms > req.write_pause_budget_ms:
                    raise StepFailed(
                        "WRITE_PAUSE_BUDGET_EXCEEDED",
                        {"elapsed_ms": elapsed_ms, "budget_ms": req.write_pause_budget_ms},
                    )

            if cutover:
                async with rec.stage("6a_switch_replication") as st:
                    await self._set_sub(fx, sides.db["dst"], s.fwd_sub, False, req.epoch, applied)
                    await self._set_sub(fx, sides.db["src"], s.rev_sub, True, req.epoch, applied)
                    st.detail["forward"] = (await sides.db["dst"].subscription(s.fwd_sub)).as_dict()
                    st.detail["reverse"] = (await sides.db["src"].subscription(s.rev_sub)).as_dict()
            else:
                async with rec.stage("failback.6_stop_reverse") as st:
                    await self._set_sub(fx, sides.db["src"], s.rev_sub, False, req.epoch, applied)
                    st.detail["reverse"] = (await sides.db["src"].subscription(s.rev_sub)).as_dict()
            committed = True

            async with rec.stage(name("6b_commit_point", "6_commit_point")) as st:
                await self._commit_point(req, fx, rec, sides, cutover, st.detail)

            async with rec.stage(name("7_reconnect", "6_reconnect")) as st:
                await self._open_writer(req, fx, sides.db[target])
                await self._retarget_pool(fx, sides.admin, target, st.detail)
                applied.paused = False

            async with rec.stage(name("8_confirm_write", "7_confirm_write")) as st:
                ack_ns = await self._confirm_write(req, fx, sides.db[target], target, st.detail)
                write_pause_ms = (ack_ns - pause_start_ns) // 1_000_000
                st.detail["write_pause_ms"] = write_pause_ms
                await self._switch_attachments(req, fx, sides.db[target], st.detail)
                st.detail["attachment_freeze_ms"] = (self._clock_ns() - freeze_ns) // 1_000_000

            return RunOutcome(RunStatus.DONE, write_pause_ms, dict(rec.timings_ms))

        except BaseException as exc:  # noqa: BLE001 - every failure must end in ABORT_PRE_WRITE or SAFE_HOLD
            reason = exc.code if isinstance(exc, StepFailed) else type(exc).__name__
            if isinstance(exc, asyncio.CancelledError):
                reason = "CANCELLED"
            if committed:
                async with rec.stage(prefix + "safe_hold") as st:
                    st.detail.update({"reason": reason, "auto_resume": False})
                return RunOutcome(RunStatus.SAFE_HOLD, None, dict(rec.timings_ms), reason)
            try:
                async with rec.stage(prefix + "abort_restore") as st:
                    st.detail["reason"] = reason
                    st.detail["restored"] = await self._restore(req, fx, applied, origin, target)
                    if pause_start_ns is not None:
                        st.detail["write_pause_ms_until_resume"] = (self._clock_ns() - pause_start_ns) // 1_000_000
            except BaseException:  # noqa: BLE001
                return RunOutcome(RunStatus.SAFE_HOLD, None, dict(rec.timings_ms), f"{reason}+RESTORE_FAILED")
            return RunOutcome(RunStatus.ABORT_PRE_WRITE, None, dict(rec.timings_ms), reason)
        finally:
            if sides is not None:
                await sides.close()

    # ---- step helpers ----------------------------------------------------------------------

    async def _preflight(
        self, req: CutoverRequest, detail: dict[str, Any], sides: Sides, origin: str, target: str
    ) -> None:
        s = self._s
        cutover = req.kind == CUTOVER
        src, dst = sides.db["src"], sides.db["dst"]
        if cutover:
            lag = await src.slot_lag_bytes(s.fwd_slot)
            streaming = await dst.subscription(s.fwd_sub)
            standby = await src.subscription(s.rev_sub)
        else:
            lag = await dst.slot_lag_bytes(s.rev_sub)
            streaming = await src.subscription(s.rev_sub)
            standby = await dst.subscription(s.fwd_sub)
        writer_origin = await sides.db[origin].writer_state(s.app_role)
        writer_target = await sides.db[target].writer_state(s.app_role)
        pool = await sides.admin.database(s.pgb_db_name)
        ini_target = read_database_target(s.pgb_ini_path.read_text(encoding="utf-8"), s.pgb_db_name)
        att_origin = await sides.db[origin].attachment_store()
        att_target = await sides.db[target].attachment_store()
        detail["attachment_store"] = {origin: att_origin, target: att_target}
        detail.update(
            {
                "lag_bytes": lag,
                "lag_max_bytes": s.lag_max_bytes,
                "streaming_subscription": streaming.as_dict(),
                "standby_subscription": standby.as_dict(),
                f"{origin}_writer": writer_origin,
                f"{target}_writer": writer_target,
                "pool_host": None if pool is None else pool.get("host"),
                "pool_paused": None if pool is None else pool.get("paused"),
                "ini_target_is_origin": ini_target == s.pgb_target(origin),
            }
        )
        checks = {
            "LAG_UNKNOWN": lag is None,
            "LAG_ABOVE_LIMIT": lag is not None and lag > s.lag_max_bytes,
            "REPLICATION_NOT_STREAMING": not streaming.streaming,
            # cutover: reverse must exist and be off; failback: forward must be off.
            "STANDBY_SUBSCRIPTION_NOT_READY": (not standby.exists or standby.enabled) if cutover else standby.enabled,
            "ORIGIN_WRITER_NOT_OPEN": writer_origin.get("login") is not True or writer_origin.get("insert") is not True,
            "TARGET_WRITER_NOT_CLOSED": writer_target.get("login") is not False,
            "POOL_NOT_ON_ORIGIN": pool is None or pool.get("host") != _target_host(s.pgb_target(origin)),
            "POOL_ALREADY_PAUSED": pool is not None and int(pool.get("paused", 0)) == 1,
            "INI_NOT_ON_ORIGIN": ini_target != s.pgb_target(origin),
            # D-04: attachments are written on the origin's active store and not frozen yet.
            "ORIGIN_ATTACHMENT_STORE_NOT_ACTIVE": att_origin is None or att_origin["frozen"]
            or att_origin["active_store"] != s.store_id(origin),
            "TARGET_ATTACHMENT_STORE_MISSING": att_target is None,
        }
        failed = [code for code, bad in checks.items() if bad]
        if failed:
            raise StepFailed("PREFLIGHT_FAILED", {"failed": failed})

    async def _touch_fence(self, fx: EffectRunner, db: ChatDb, epoch: int) -> None:
        async def call() -> dict[str, Any]:
            await db.touch_fence(epoch)
            return {"epoch": epoch}

        async def observe() -> Observation:
            value = await read_fence(db.conn)
            return value == epoch, {"fence_epoch": value}

        await fx.apply(Resources.fence(db.side), "FENCE_EPOCH", call, observe)

    async def _freeze_attachments(
        self, req: CutoverRequest, fx: EffectRunner, sides: Sides, origin: str, target: str, applied: Applied,
        detail: dict[str, Any],
    ) -> int:
        """D-04: before the write stop, freeze attachment writes on the origin (and on the target if
        it is not frozen yet) so the referenced object set is final for the copy and the receipt."""
        s = self._s
        frozen_at_ns = self._clock_ns()
        for side in (origin, target):
            db = sides.db[side]
            prior = await db.attachment_store()
            if prior is None:
                raise StepFailed("ATTACHMENT_STORE_MISSING", {"side": side})
            if prior["frozen"]:
                detail[side] = {"already_frozen": True, **prior}
                continue
            applied.attachments_prior[side] = prior

            async def call(db: ChatDb = db) -> dict[str, Any]:
                await db.freeze_attachments(req.epoch, s.freeze_lock_timeout_ms)
                return {}

            async def observe(db: ChatDb = db) -> Observation:
                now = await db.attachment_store()
                return now is not None and now["frozen"] and now["freeze_watermark"] is not None, {"store": now}

            evidence = await fx.apply(Resources.attachment_store(side), "FREEZE", call, observe)
            detail[side] = evidence["observed"]["store"]
            if side == origin:
                frozen_at_ns = self._clock_ns()
        return frozen_at_ns

    async def _switch_attachments(
        self, req: CutoverRequest, fx: EffectRunner, db: ChatDb, detail: dict[str, Any]
    ) -> None:
        """D-04 step 8: the target's store becomes active and attachments unfreeze, in one transaction."""
        store = self._s.store_id(db.side)

        async def call() -> dict[str, Any]:
            await db.switch_attachment_store(req.epoch, store)
            return {"active_store": store}

        async def observe() -> Observation:
            now = await db.attachment_store()
            ok = now is not None and not now["frozen"] and now["active_store"] == store
            return ok, {"store": now}

        evidence = await fx.apply(Resources.attachment_store(db.side), "SWITCH_AND_UNFREEZE", call, observe)
        detail["attachment_store"] = evidence["observed"]["store"]

    async def _pause(self, fx: EffectRunner, admin: PgBouncerAdmin) -> None:
        s = self._s

        async def call() -> dict[str, Any]:
            await asyncio.wait_for(admin.command("PAUSE", s.pgb_db_name), s.pause_timeout_s)
            return {}

        async def observe() -> Observation:
            row = await admin.database(s.pgb_db_name)
            paused = row is not None and int(row.get("paused", 0)) == 1
            return paused, {"paused": None if row is None else row.get("paused")}

        await fx.apply(Resources.PGBOUNCER, "PAUSE", call, observe)

    async def _close_writer(
        self, req: CutoverRequest, fx: EffectRunner, db: ChatDb, applied: Applied, detail: dict[str, Any]
    ) -> None:
        """03 4.2 step 4 / 4.3 step 2: NOLOGIN, REVOKE INSERT, end sessions, prove rejection."""
        s, side = self._s, db.side
        applied.writer_login_closed = side

        async def nologin() -> dict[str, Any]:
            await db.set_role_login(req.epoch, s.app_role, login=False)
            return {}

        async def obs_nologin() -> Observation:
            st = await db.writer_state(s.app_role)
            return st["login"] is False, st

        await fx.apply(Resources.app_role(side), "NOLOGIN", nologin, obs_nologin)

        applied.writer_insert_revoked = side

        async def revoke() -> dict[str, Any]:
            await db.set_insert_privilege(req.epoch, s.app_role, granted=False)
            return {}

        async def obs_revoke() -> Observation:
            st = await db.writer_state(s.app_role)
            return st["insert"] is False, st

        await fx.apply(Resources.app_grant(side), "REVOKE_INSERT", revoke, obs_revoke)

        async def terminate() -> dict[str, Any]:
            return {"terminated": await db.terminate_role_sessions(req.epoch, s.app_role)}

        async def obs_sessions() -> Observation:
            async def none_left() -> bool:
                return await db.role_session_count(s.app_role) == 0

            ok, waited = await wait_until(none_left, 3.0)
            return ok, {"sessions_left": 0 if ok else await db.role_session_count(s.app_role),
                        "waited_ms": round(waited)}

        await fx.apply(Resources.app_sessions(side), "TERMINATE", terminate, obs_sessions)
        detail["login_rejected"] = await self._prove_login_rejected(side)

    async def _prove_login_rejected(self, side: str) -> dict[str, Any]:
        try:
            conn = await AsyncConnection.connect(self._s.app_dsn(side), connect_timeout=3)
        except psycopg.OperationalError as exc:
            if "not permitted to log in" in str(exc):
                return {"rejected": True, "evidence": "role is not permitted to log in"}
            raise StepFailed("FENCE_CHECK_INCONCLUSIVE", {"error": error_evidence(exc)}) from exc
        await conn.close()
        raise StepFailed("FENCE_NOT_EFFECTIVE", {"rejected": False, "side": side})

    async def _barrier_and_compare(
        self,
        req: CutoverRequest,
        fx: EffectRunner,
        origin: ChatDb,
        target: ChatDb,
        receiving_side: str,
        receiving_sub: str,
        rec: StageRecorder,
        pause_start_ns: int,
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        s = self._s
        barrier: dict[str, Any] = {}

        async def insert_barrier() -> dict[str, Any]:
            barrier_id, at = await origin.insert_barrier(req.epoch, str(req.plan_id))
            barrier.update({"id": barrier_id, "committed_at": at, "source_lsn": await origin.current_wal_lsn()})
            return dict(barrier)

        async def obs_barrier() -> Observation:
            async def arrived() -> bool:
                return await target.barrier_present(int(barrier["id"]))

            ok, waited = await wait_until(arrived, s.barrier_timeout_s, 0.01)
            barrier["arrival_ms"] = round(waited)
            return ok, {f"arrived_on_{target.side}": ok, "waited_ms": round(waited)}

        try:
            await fx.apply(Resources.barrier(origin.side), "INSERT_BARRIER", insert_barrier, obs_barrier)
        except StepFailed as exc:
            if exc.code == "EFFECT_NOT_OBSERVED":
                raise StepFailed("BARRIER_NOT_ARRIVED", {"timeout_s": s.barrier_timeout_s}) from exc
            raise
        receiving = await target.subscription(receiving_sub)
        detail["barrier"] = dict(barrier)

        async def digest_all(db: ChatDb) -> list[TableDigest]:
            # One connection runs one statement at a time: tables in sequence, sides in parallel.
            return [await db.table_digest(t) for t in DIGEST_TABLES]

        origin_d, target_d = await asyncio.gather(digest_all(origin), digest_all(target))
        tables: list[dict[str, Any]] = []
        tables_match = True
        for od, td in zip(origin_d, target_d, strict=True):
            match = od.rows == td.rows and od.pk_hash == td.pk_hash and od.content_hash == td.content_hash
            tables_match = tables_match and match
            tables.append(
                {
                    "name": od.name,
                    "rows": {origin.side: od.rows, target.side: td.rows},
                    "pk_hash": {origin.side: od.pk_hash, target.side: td.pk_hash},
                    "content_hash": {origin.side: od.content_hash, target.side: td.content_hash},
                    "match": match,
                }
            )

        sequences: list[dict[str, Any]] = []
        seq_match = True
        for seq in SEQUENCES:
            origin_last = await origin.sequence_last(seq)
            target_before = await target.sequence_last(seq)
            if tables_match and origin_last > target_before:

                async def raise_seq(seq: str = seq, value: int = origin_last) -> dict[str, Any]:
                    await target.raise_sequence(req.epoch, seq, value)
                    return {"requested": value}

                async def obs_raise(seq: str = seq, value: int = origin_last) -> Observation:
                    now = await target.sequence_last(seq)
                    return now >= value, {f"{target.side}_last": now}

                await fx.apply(Resources.sequence(target.side, seq), "RAISE_SEQUENCE", raise_seq, obs_raise)
            target_set = await target.sequence_last(seq)
            ok = target_set >= origin_last
            seq_match = seq_match and ok
            sequences.append(
                {"name": seq, "origin_last": origin_last, "target_before": target_before, "target_set": target_set,
                 "match": ok}
            )

        objects = await self._copy_objects(origin, origin.side, target.side)
        objects_ok = objects["verdict"] in ("MATCH", "NOT_CHECKED")
        verdict = "MATCH" if tables_match and seq_match and objects_ok else "MISMATCH"
        body: dict[str, Any] = {
            "receipt_id": str(uuid.uuid4()),
            "run_id": str(req.run_id),
            "plan_id": str(req.plan_id),
            "epoch": req.epoch,
            "direction": req.kind,
            "source": s.cluster_name(origin.side),
            "target": s.cluster_name(target.side),
            "barrier": {"id": barrier.get("id"), "committed_at": barrier.get("committed_at"),
                        "arrival_ms": barrier.get("arrival_ms")},
            "watermark": {"source_lsn": barrier.get("source_lsn"), "received_lsn": receiving.latest_end_lsn,
                          "subscription": f"{receiving_side}/{receiving_sub}"},
            "hash_rule": HASH_RULE,
            "tables": tables,
            "sequences": sequences,
            "objects": objects,
            "compared_under": (
                f"writes paused (PgBouncer PAUSE, {origin.side} app_writer NOLOGIN + INSERT revoked, sessions ended)"
            ),
            "write_pause_ms_at_verdict": (self._clock_ns() - pause_start_ns) // 1_000_000,
            "stages_ms_at_verdict": dict(rec.timings_ms),
            "verdict": verdict,
            "created_at": datetime.now(UTC).isoformat(),
        }
        # Saved before any write is opened on the target (S3-AC2).
        await self._ledger.save_receipt(req.run_id, body, verdict)
        return body

    async def _copy_objects(self, origin: ChatDb, from_side: str, to_side: str) -> dict[str, Any]:
        """D-04 objects check: referenced keys of the origin store must be VERIFIED on the target store.

        NOT_CHECKED (honestly reported, does not fail the receipt) when no object stores are
        configured (BRAVO_ATT_SRC_S3_BUCKET / BRAVO_ATT_DST_S3_BUCKET)."""
        copier = self._copier_factory(from_side, to_side) if self._copier_factory else None
        if copier is None:
            cur = await origin.conn.execute(
                "SELECT count(DISTINCT object_key) FROM attachments WHERE store_id = %s", (self._s.store_id(from_side),)
            )
            row = await cur.fetchone()
            return {"verdict": "NOT_CHECKED", "reason": "OBJECT_STORES_NOT_CONFIGURED",
                    "referenced": int(row[0]) if row else 0}
        report = await copier.run(origin.conn)
        return {"verdict": "MATCH" if report.complete else "MISMATCH", **report.as_dict()}

    async def _set_sub(
        self, fx: EffectRunner, db: ChatDb, sub: str, enabled: bool, epoch: int, applied: Applied | None
    ) -> None:
        s = self._s
        if applied is not None:
            (applied.subs_enabled if enabled else applied.subs_disabled).append((db.side, sub))

        async def call() -> dict[str, Any]:
            await db.set_subscription_enabled(epoch, sub, enabled=enabled)
            return {}

        async def observe() -> Observation:
            async def reached() -> bool:
                st = await db.subscription(sub)
                return st.streaming if enabled else (not st.enabled and st.worker_pid is None)

            ok, waited = await wait_until(reached, s.subscription_timeout_s, 0.02)
            return ok, {"subscription": (await db.subscription(sub)).as_dict(), "waited_ms": round(waited)}

        await fx.apply(Resources.subscription(db.side, sub), "ENABLE" if enabled else "DISABLE", call, observe)

    async def _commit_point(
        self, req: CutoverRequest, fx: EffectRunner, rec: StageRecorder, sides: Sides, cutover: bool,
        detail: dict[str, Any],
    ) -> None:
        """Record the point of no return, then drop the replication slot that is no longer used:
        cutover -> forward slot on src (03 4.2 6b); failback -> reverse slot on dst (03 4.3 step 6)."""
        s = self._s

        async def commit() -> dict[str, Any]:
            return {"commit_point": req.kind, "after_stage_seq": rec.last_seq}

        async def obs_commit() -> Observation:
            return True, {"recorded_in": "platform-db"}

        await fx.apply(Resources.PLATFORM_RUN, "COMMIT_POINT", commit, obs_commit)
        detail["irreversible"] = True
        sub_side, sub, slot_side, slot = (
            ("dst", s.fwd_sub, "src", s.fwd_slot) if cutover else ("src", s.rev_sub, "dst", s.rev_sub)
        )
        sub_db, slot_db = sides.db[sub_side], sides.db[slot_side]

        async def detach() -> dict[str, Any]:
            await sub_db.detach_subscription_slot(req.epoch, sub)
            return {}

        async def obs_detach() -> Observation:
            slot_name = await sub_db.subscription_slot_name(sub)
            return slot_name is None, {"subslotname": slot_name}

        await fx.apply(Resources.subscription(sub_side, sub), "DETACH_SLOT", detach, obs_detach)

        async def drop() -> dict[str, Any]:
            async def inactive() -> bool:
                st = await slot_db.slot(slot)
                return st is None or not st["active"]

            ok, waited = await wait_until(inactive, s.subscription_timeout_s, 0.02)
            if not ok:
                raise StepFailed("SLOT_STILL_ACTIVE", {"slot": slot})
            await slot_db.drop_slot(req.epoch, slot)
            return {"waited_inactive_ms": round(waited)}

        async def obs_drop() -> Observation:
            return await slot_db.slot(slot) is None, {"slot_exists": await slot_db.slot(slot) is not None}

        await fx.apply(Resources.slot(slot_side, slot), "DROP_SLOT", drop, obs_drop)
        detail["dropped_slot"] = f"{slot_side}/{slot}"

    async def _open_writer(self, req: CutoverRequest, fx: EffectRunner, db: ChatDb) -> None:
        s, side = self._s, db.side

        async def grant() -> dict[str, Any]:
            await db.set_insert_privilege(req.epoch, s.app_role, granted=True)
            return {}

        async def obs_grant() -> Observation:
            st = await db.writer_state(s.app_role)
            return st["insert"] is True, st

        await fx.apply(Resources.app_grant(side), "GRANT_INSERT", grant, obs_grant)

        async def login() -> dict[str, Any]:
            await db.set_role_login(req.epoch, s.app_role, login=True)
            return {}

        async def obs_login() -> Observation:
            st = await db.writer_state(s.app_role)
            return st["login"] is True, st

        await fx.apply(Resources.app_role(side), "LOGIN", login, obs_login)

    async def _retarget_pool(self, fx: EffectRunner, admin: PgBouncerAdmin, side: str, detail: dict[str, Any]) -> None:
        s = self._s
        new_target = s.pgb_target(side)
        new_host = _target_host(new_target)

        async def rewrite() -> dict[str, Any]:
            text = s.pgb_ini_path.read_text(encoding="utf-8")
            before = read_database_target(text, s.pgb_db_name)
            write_ini_atomically(s.pgb_ini_path, rewrite_database_target(text, s.pgb_db_name, new_target))
            return {"from": before, "to": new_target}

        async def obs_rewrite() -> Observation:
            now = read_database_target(s.pgb_ini_path.read_text(encoding="utf-8"), s.pgb_db_name)
            return now == new_target, {"target": now}

        await fx.apply(Resources.PGBOUNCER_CONFIG, "REWRITE_TARGET", rewrite, obs_rewrite)

        async def reload() -> dict[str, Any]:
            await admin.command("RELOAD")
            return {}

        async def obs_reload() -> Observation:
            row = await admin.database(s.pgb_db_name)
            host = None if row is None else row.get("host")
            return host == new_host, {"host": host}

        await fx.apply(Resources.PGBOUNCER, "RELOAD", reload, obs_reload)

        async def resume() -> dict[str, Any]:
            await admin.command("RESUME", s.pgb_db_name)
            return {}

        async def obs_resume() -> Observation:
            row = await admin.database(s.pgb_db_name)
            paused = None if row is None else int(row.get("paused", 0))
            return paused == 0, {"paused": paused, "host": None if row is None else row.get("host")}

        await fx.apply(Resources.PGBOUNCER, "RESUME", resume, obs_resume)
        detail["pool_host"] = new_host

    async def _confirm_write(
        self, req: CutoverRequest, fx: EffectRunner, target: ChatDb, side: str, detail: dict[str, Any]
    ) -> int:
        s = self._s
        expected = s.cluster_name(side)
        probe_key = uuid.uuid5(uuid.NAMESPACE_URL, f"bravo:{req.kind}-probe:{req.run_id}")
        result: dict[str, Any] = {}

        async def probe() -> dict[str, Any]:
            conn = await AsyncConnection.connect(
                s.pgb_app_dsn, autocommit=True, prepare_threshold=None, connect_timeout=int(s.probe_timeout_s)
            )
            try:
                cur = await asyncio.wait_for(
                    conn.execute(
                        """
                        INSERT INTO messages (room, author, body, idem_key)
                        VALUES (%s, 'bravo-executor', %s, %s)
                        ON CONFLICT (idem_key) DO NOTHING
                        RETURNING id, current_setting('cluster_name')
                        """,
                        (s.probe_room, f"{req.kind} probe run={req.run_id}", probe_key),
                    ),
                    s.probe_timeout_s,
                )
                row = await cur.fetchone()
                result["ack_ns"] = self._clock_ns()
                if row is None:
                    cur = await conn.execute(
                        "SELECT id, current_setting('cluster_name') FROM messages WHERE idem_key = %s", (probe_key,)
                    )
                    row = await cur.fetchone()
            finally:
                await conn.close()
            if row is None:
                raise StepFailed("PROBE_NOT_ACKED")
            result.update({"id": int(row[0]), "cluster": str(row[1])})
            return {"id": result["id"], "cluster": result["cluster"], "idem_key": str(probe_key)}

        async def obs_probe() -> Observation:
            cur = await target.conn.execute("SELECT id FROM messages WHERE idem_key = %s", (probe_key,))
            row = await cur.fetchone()
            on_target = row is not None and int(row[0]) == result.get("id")
            return on_target and result.get("cluster") == expected, {
                f"row_on_{side}": on_target,
                "served_by": result.get("cluster"),
            }

        await fx.apply(Resources.PGBOUNCER_POOL_PATH, "PROBE_WRITE", probe, obs_probe)
        detail["probe"] = {"id": result.get("id"), "served_by": result.get("cluster")}
        return int(result["ack_ns"])

    async def _restore(
        self, req: CutoverRequest, fx: EffectRunner, applied: Applied, origin: str, target: str
    ) -> list[str]:
        """ABORT_PRE_WRITE (03 4.2): fresh sessions; undo only what this run changed, newest first."""
        s = self._s
        restored: list[str] = []
        dbs = {"src": ChatDb(await connect(s.src_dsn), "src"), "dst": ChatDb(await connect(s.dst_dsn), "dst")}
        try:
            for side, sub in reversed(applied.subs_enabled):
                await self._set_sub(fx, dbs[side], sub, False, req.epoch, None)
                restored.append(f"{side}/{sub} disabled")
            for side, sub in reversed(applied.subs_disabled):
                await self._set_sub(fx, dbs[side], sub, True, req.epoch, None)
                restored.append(f"{side}/{sub} enabled")
            target_writer = await dbs[target].writer_state(s.app_role)
            if target_writer.get("login") is not False:
                raise StepFailed("TARGET_WRITER_OPEN_DURING_ABORT", {"side": target})
            restored.append(f"{target} writer confirmed closed")
            if applied.writer_insert_revoked:
                await self._undo(
                    fx, Resources.app_grant(origin), "GRANT_INSERT",
                    lambda: dbs[origin].set_insert_privilege(req.epoch, s.app_role, granted=True),
                    lambda: self._writer_is(dbs[origin], "insert", True),
                )
                restored.append(f"{origin} INSERT granted")
            if applied.writer_login_closed:
                await self._undo(
                    fx, Resources.app_role(origin), "LOGIN",
                    lambda: dbs[origin].set_role_login(req.epoch, s.app_role, login=True),
                    lambda: self._writer_is(dbs[origin], "login", True),
                )
                restored.append(f"{origin} LOGIN restored")
        finally:
            for db in dbs.values():
                await db.close()
        if applied.paused:
            admin = await PgBouncerAdmin.connect(s.pgb_admin_dsn)
            try:
                async def resume() -> dict[str, Any]:
                    row = await admin.database(s.pgb_db_name)
                    if row is not None and int(row.get("paused", 0)) == 1:
                        await admin.command("RESUME", s.pgb_db_name)
                        return {"compensates": "PAUSE", "sent": True}
                    return {"compensates": "PAUSE", "sent": False, "reason": "pool was not paused"}

                async def obs_resume() -> Observation:
                    row = await admin.database(s.pgb_db_name)
                    paused = None if row is None else int(row.get("paused", 0))
                    return paused == 0, {"paused": paused, "host": None if row is None else row.get("host")}

                await fx.apply(Resources.PGBOUNCER, "RESUME", resume, obs_resume)
                restored.append("pool resumed")
            finally:
                await admin.close()
        for side, prior in applied.attachments_prior.items():
            db = ChatDb(await connect(s.dst_dsn if side == "dst" else s.src_dsn), side)
            try:
                async def restore(db: ChatDb = db, prior: dict[str, Any] = prior) -> dict[str, Any]:
                    await db.restore_attachment_store(req.epoch, prior)
                    return {"compensates": "FREEZE", "restored_to": prior}

                async def obs_restore(db: ChatDb = db, prior: dict[str, Any] = prior) -> Observation:
                    now = await db.attachment_store()
                    ok = (
                        now is not None
                        and now["frozen"] == prior["frozen"]
                        and now["active_store"] == prior["active_store"]
                    )
                    return ok, {"store": now}

                await fx.apply(Resources.attachment_store(side), "RESTORE", restore, obs_restore)
                restored.append(
                    f"{side} attachments restored (frozen={prior['frozen']}, store={prior['active_store']})"
                )
            finally:
                await db.close()
        return restored

    async def _writer_is(self, db: ChatDb, key: str, value: bool) -> Observation:
        st = await db.writer_state(self._s.app_role)
        return st[key] is value, st

    async def _undo(
        self,
        fx: EffectRunner,
        resource: str,
        action: str,
        do: Callable[[], Awaitable[None]],
        check: Callable[[], Awaitable[Observation]],
    ) -> None:
        async def call() -> dict[str, Any]:
            await do()
            return {"compensation": True}

        await fx.apply(resource, action, call, check)
