"""Approval check before any run (spec §8.9, r2 "승인 2종", audit A2).

The Executor does not trust the ``approval_id`` it is given. It reads the approval the platform
API recorded (table ``approvals`` in the platform DB, owner platform/api migrations) and refuses
the run unless:

* the approval exists and belongs to the same ``plan_id``;
* its ``stage`` equals the run kind (``cutover`` or ``failback``);
* the approver is not the requester (separation of duties, D-18);
* it has not expired (``expires_at > now()``);
* the platform API consumed it when an operator started the run in the console (``consumed_at`` set):
  this is the evidence of the console start (contract 15 §10.1); the starter is not the approver;
* no other run of the same kind already used it (one approval: one shift run and one cutover run,
  or one failback run);
* a cutover only: the shift run (10/50/100) with the same approval ended DONE (SHIFT before CUTOVER).

Reading needs ``SELECT`` on ``approvals`` for the Executor's DB role (platform/api grants it).
When the table is missing or unreadable the run is refused with APPROVAL_SOURCE_UNAVAILABLE:
there is no fallback that accepts an unverified ID.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class ApprovalRejected(Exception):
    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


@dataclass(frozen=True, slots=True)
class ApprovalCheck:
    approval_id: str
    plan_id: UUID
    kind: str                 # approval stage: cutover | failback
    idem_key: str
    run_kind: str = ""        # runs.kind being admitted (shift | cutover | failback); empty = kind

    @property
    def admitted_kind(self) -> str:
        return self.run_kind or self.kind


class PlatformDbApprovals:
    def __init__(self, pool: AsyncConnectionPool[AsyncConnection]) -> None:
        self._pool = pool

    async def verify(self, check: ApprovalCheck) -> dict[str, Any]:
        try:
            approval_id = UUID(check.approval_id)
        except ValueError as exc:
            raise ApprovalRejected("APPROVAL_ID_INVALID") from exc
        try:
            async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT approval_id, plan_id, stage, approver_id, requester_id, expires_at, consumed_at,
                           consumed_by, expires_at > now() AS live
                    FROM approvals WHERE approval_id = %s
                    """,
                    (approval_id,),
                )
                row = await cur.fetchone()
                await cur.execute(
                    """
                    SELECT r.run_id FROM run_stages s JOIN runs r USING (run_id)
                    WHERE s.seq = 1 AND s.detail->>'approval_id' = %s AND r.idem_key <> %s AND r.kind = %s
                    LIMIT 1
                    """,
                    (str(approval_id), check.idem_key, check.admitted_kind),
                )
                reused = await cur.fetchone()
                shift = None
                if check.admitted_kind == "cutover":
                    await cur.execute(
                        """
                        SELECT r.run_id, r.status FROM run_stages s JOIN runs r USING (run_id)
                        WHERE s.seq = 1 AND s.detail->>'approval_id' = %s AND r.kind = 'shift'
                        ORDER BY r.started_at DESC LIMIT 1
                        """,
                        (str(approval_id),),
                    )
                    shift = await cur.fetchone()
        except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege) as exc:
            raise ApprovalRejected("APPROVAL_SOURCE_UNAVAILABLE", {"error": type(exc).__name__}) from exc
        if row is None:
            raise ApprovalRejected("APPROVAL_NOT_FOUND")
        evidence = {
            "approval_id": str(row["approval_id"]),
            "stage": row["stage"],
            "approver_id": str(row["approver_id"]),
            "requester_id": str(row["requester_id"]),
            "expires_at": row["expires_at"].isoformat(),
            "consumed_at": None if row["consumed_at"] is None else row["consumed_at"].isoformat(),
            "started_by": None if row["consumed_by"] is None else str(row["consumed_by"]),
            "source": "platform-db approvals",
        }
        if row["plan_id"] != check.plan_id:
            raise ApprovalRejected("APPROVAL_PLAN_MISMATCH", evidence)
        if row["stage"] != check.kind:
            raise ApprovalRejected("APPROVAL_STAGE_MISMATCH", evidence)
        if row["approver_id"] == row["requester_id"]:
            raise ApprovalRejected("APPROVER_IS_REQUESTER", evidence)
        if not row["live"]:
            raise ApprovalRejected("APPROVAL_EXPIRED", evidence)
        if row["consumed_at"] is None:
            raise ApprovalRejected("APPROVAL_NOT_STARTED", evidence)  # no operator started it in the console
        if row["consumed_by"] == row["approver_id"]:
            raise ApprovalRejected("STARTER_IS_APPROVER", evidence)
        if reused is not None:
            raise ApprovalRejected("APPROVAL_ALREADY_USED", {**evidence, "run_id": str(reused["run_id"])})
        if check.admitted_kind == "cutover":
            if shift is None or shift["status"] != "DONE":
                raise ApprovalRejected("SHIFT_NOT_DONE", {
                    **evidence, "shift_run_id": None if shift is None else str(shift["run_id"]),
                    "shift_status": None if shift is None else shift["status"]})
            evidence["shift_run_id"] = str(shift["run_id"])
        return evidence
