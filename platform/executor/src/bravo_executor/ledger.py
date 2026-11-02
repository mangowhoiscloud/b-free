"""Platform DB access: runs, run_stages, effects, receipts (CONTRACT.md 5).

The schema is owned by platform/api/migrations. This module only reads and writes
those four tables, each call in its own committed transaction, so that the ledger
reflects what was decided before the next external call is made.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


class RunStatus:
    RUNNING = "RUNNING"
    DONE = "DONE"
    ABORT_PRE_WRITE = "ABORT_PRE_WRITE"
    SAFE_HOLD = "SAFE_HOLD"


class EffectState:
    PREPARED = "PREPARED"
    IN_FLIGHT = "IN_FLIGHT"
    CONFIRMED_APPLIED = "CONFIRMED_APPLIED"
    CONFIRMED_NOT_APPLIED = "CONFIRMED_NOT_APPLIED"
    UNKNOWN = "UNKNOWN"

    UNRESOLVED = (PREPARED, IN_FLIGHT, UNKNOWN)


class IdempotencyConflict(Exception):
    """The Idempotency-Key was already used with a different request."""

    def __init__(self, run_id: UUID) -> None:
        super().__init__(f"Idempotency-Key already used for run {run_id} with different input")
        self.run_id = run_id


class ResourceBusy(RuntimeError):
    """Another run holds at least one resource of the requested set. Nothing was reserved."""

    def __init__(self, held: dict[str, str]) -> None:
        super().__init__(f"resources held by other runs: {sorted(held)}")
        self.held = held


class LedgerStateError(RuntimeError):
    """A compare-and-set on the ledger matched no row."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    plan_id: UUID
    epoch: int
    idem_key: str
    kind: str = "cutover"


class Ledger(Protocol):
    async def create_or_get_run(self, req: RunRequest) -> tuple[UUID, bool]: ...
    async def finish_run(self, run_id: UUID, status: str, write_pause_ms: int | None) -> None: ...
    async def record_stage(
        self,
        run_id: UUID,
        seq: int,
        name: str,
        started_at: datetime,
        ms: int,
        result: str,
        detail: dict[str, Any],
    ) -> None: ...
    async def effect_prepare(self, run_id: UUID, resource: str, action: str, epoch: int) -> UUID: ...
    async def effect_transition(
        self, effect_id: UUID, from_state: str, to_state: str, evidence: dict[str, Any] | None = None
    ) -> None: ...
    async def save_receipt(self, run_id: UUID, body: dict[str, Any], verdict: str) -> UUID: ...
    async def reserve_locks(
        self, run_id: UUID, resources: list[str], owner: str, epoch: int, lease_s: float
    ) -> None: ...
    async def release_locks_if_resolved(self, run_id: UUID) -> bool: ...
    async def get_run(self, run_id: UUID) -> dict[str, Any] | None: ...


class PostgresLedger:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def create_or_get_run(self, req: RunRequest) -> tuple[UUID, bool]:
        """Insert a RUNNING run for this key, or return the existing run for the same key.

        Same key with a different plan_id, epoch or kind raises IdempotencyConflict.
        """
        new_id = uuid.uuid4()
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """
                INSERT INTO runs (run_id, plan_id, kind, epoch, idem_key, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (idem_key) DO NOTHING
                RETURNING run_id
                """,
                (new_id, req.plan_id, req.kind, req.epoch, req.idem_key, RunStatus.RUNNING),
            )
            if await cur.fetchone() is not None:
                return new_id, True
            cur = await conn.execute(
                "SELECT run_id, plan_id, kind, epoch FROM runs WHERE idem_key = %s", (req.idem_key,)
            )
            row = await cur.fetchone()
        if row is None:  # pragma: no cover - the conflicting row cannot vanish (no deletes)
            raise LedgerStateError("idempotency row disappeared")
        run_id, plan_id, kind, epoch = row
        if plan_id != req.plan_id or kind != req.kind or epoch != req.epoch:
            raise IdempotencyConflict(run_id)
        return run_id, False

    async def finish_run(self, run_id: UUID, status: str, write_pause_ms: int | None) -> None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE runs SET status = %s, write_pause_ms = %s, ended_at = now()
                WHERE run_id = %s AND status = %s
                """,
                (status, write_pause_ms, run_id, RunStatus.RUNNING),
            )
            if cur.rowcount != 1:
                raise LedgerStateError(f"run {run_id} is not RUNNING")

    async def record_stage(
        self,
        run_id: UUID,
        seq: int,
        name: str,
        started_at: datetime,
        ms: int,
        result: str,
        detail: dict[str, Any],
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO run_stages (run_id, seq, name, started_at, ms, result, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (run_id, seq, name, started_at, ms, result, Jsonb(detail)),
            )

    async def effect_prepare(self, run_id: UUID, resource: str, action: str, epoch: int) -> UUID:
        effect_id = uuid.uuid4()
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO effects (effect_id, run_id, resource, action, epoch, state)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (effect_id, run_id, resource, action, epoch, EffectState.PREPARED),
            )
        return effect_id

    async def effect_transition(
        self, effect_id: UUID, from_state: str, to_state: str, evidence: dict[str, Any] | None = None
    ) -> None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE effects
                   SET state = %s, observed_at = now(),
                       evidence = COALESCE(%s, evidence)
                 WHERE effect_id = %s AND state = %s
                """,
                (to_state, None if evidence is None else Jsonb(evidence), effect_id, from_state),
            )
            if cur.rowcount != 1:
                raise LedgerStateError(f"effect {effect_id} is not {from_state}")

    async def save_receipt(self, run_id: UUID, body: dict[str, Any], verdict: str) -> UUID:
        receipt_id = UUID(str(body["receipt_id"]))
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO receipts (receipt_id, run_id, body, verdict) VALUES (%s, %s, %s, %s)",
                (receipt_id, run_id, Jsonb(body), verdict),
            )
        return receipt_id

    # ---- resource locks (D-19) ----------------------------------------------------------

    async def reserve_locks(
        self, run_id: UUID, resources: list[str], owner: str, epoch: int, lease_s: float
    ) -> None:
        """Reserve the whole set in one transaction, or nothing (ResourceBusy)."""
        wanted = sorted(set(resources))
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """
                INSERT INTO resource_locks (resource, run_id, owner, epoch, lease_expires_at)
                SELECT r, %s, %s, %s, now() + make_interval(secs => %s) FROM unnest(%s::text[]) AS r
                ON CONFLICT (resource) DO NOTHING
                RETURNING resource
                """,
                (run_id, owner, epoch, lease_s, wanted),
            )
            got = {r[0] for r in await cur.fetchall()}
            if got != set(wanted):
                cur = await conn.execute(
                    "SELECT resource, run_id FROM resource_locks WHERE resource = ANY(%s) AND run_id <> %s",
                    (wanted, run_id),
                )
                held = {r[0]: str(r[1]) for r in await cur.fetchall()}
                raise ResourceBusy(held)  # rolls back the partial reservation

    async def release_locks_if_resolved(self, run_id: UUID) -> bool:
        """Release only when every effect of the run is CONFIRMED_*. Lease expiry never releases (E3)."""
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT count(*) FROM effects WHERE run_id = %s AND state = ANY(%s)",
                (run_id, list(EffectState.UNRESOLVED)),
            )
            row = await cur.fetchone()
            if row is not None and int(row[0]) > 0:
                return False
            await conn.execute("DELETE FROM resource_locks WHERE run_id = %s", (run_id,))
        return True

    # ---- restart reconciliation ---------------------------------------------------------

    async def unresolved_effects(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT effect_id, run_id, resource, action, epoch, state, evidence
                FROM effects WHERE state = ANY(%s) ORDER BY run_id, observed_at NULLS LAST
                """,
                (list(EffectState.UNRESOLVED),),
            )
            return list(await cur.fetchall())

    async def next_stage_seq(self, run_id: UUID) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT coalesce(max(seq), 0) + 1 FROM run_stages WHERE run_id = %s", (run_id,))
            row = await cur.fetchone()
        return int(row[0]) if row else 1

    # ---- reads -------------------------------------------------------------------------

    async def list_runs(self, kind: str | None, limit: int) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT run_id, plan_id, kind, epoch, status, write_pause_ms, started_at, ended_at
                FROM runs WHERE (%s::text IS NULL OR kind = %s) ORDER BY started_at DESC LIMIT %s
                """,
                (kind, kind, limit),
            )
            return list(await cur.fetchall())

    async def find_run_by_key(self, idem_key: str) -> UUID | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT run_id FROM runs WHERE idem_key = %s", (idem_key,))
            row = await cur.fetchone()
        return None if row is None else row[0]

    async def get_run(self, run_id: UUID) -> dict[str, Any] | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT run_id, plan_id, kind, epoch, status, write_pause_ms, started_at, ended_at
                FROM runs WHERE run_id = %s
                """,
                (run_id,),
            )
            run = await cur.fetchone()
            if run is None:
                return None
            await cur.execute(
                """
                SELECT seq, name, started_at, ms, result, detail
                FROM run_stages WHERE run_id = %s ORDER BY seq
                """,
                (run_id,),
            )
            stages = list(await cur.fetchall())
            await cur.execute(
                """
                SELECT receipt_id, body, verdict, created_at
                FROM receipts WHERE run_id = %s ORDER BY created_at DESC LIMIT 1
                """,
                (run_id,),
            )
            receipt = await cur.fetchone()
            await cur.execute(
                """
                SELECT effect_id, resource, action, epoch, state, observed_at, evidence
                FROM effects WHERE run_id = %s ORDER BY observed_at NULLS LAST, effect_id
                """,
                (run_id,),
            )
            effects = list(await cur.fetchall())
        return {"run": run, "stages": stages, "receipt": receipt, "effects": effects}

    async def count_unresolved_effects(self) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM effects WHERE state = ANY(%s)", (list(EffectState.UNRESOLVED),)
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def running_run_ids(self) -> list[UUID]:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT run_id FROM runs WHERE status = %s", (RunStatus.RUNNING,))
            return [r[0] for r in await cur.fetchall()]

    async def hold_orphaned_runs(self, run_ids: list[UUID]) -> None:
        """Runs left RUNNING by a previous process go to SAFE_HOLD; they are never resumed."""
        for run_id in run_ids:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE runs SET status = %s, ended_at = now() WHERE run_id = %s AND status = %s",
                    (RunStatus.SAFE_HOLD, run_id, RunStatus.RUNNING),
                )
