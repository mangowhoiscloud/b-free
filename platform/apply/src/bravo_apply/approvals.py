"""The approval the apply phase runs under, read from the platform DB and checked here (fail closed).

Nothing outside the platform DB can grant an apply: not a binding file, not a CLI flag, not the change row's
own digests. The only source is the ``approvals`` row platform/api recorded through the console (stage
``change``, bound by the ``bravo_approvals_bind`` trigger), read with this worker's role ``bravo_apply``.

Refusals (``Stop("refused", reason)``):
  approval_source_unavailable  no platform DB configured, or the table cannot be read
  approval_missing             the change names no approval (e.g. ``approval None``)
  approval_not_found           no such row
  approval_stage_mismatch      the row is not a ``change`` approval
  approval_change_mismatch     the row is for another change, or the change does not point back at it
  approval_not_granted         the change is not in ``approved`` / ``applying`` (the API has not granted it)
  approver_is_requester        separation of duties (approver = requester or proposer)
  approval_expired             ``expires_at`` has passed
The returned dict carries the approved digests; the pipeline compares them with what it prepared.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from .runtime import Result, Stop

GRANTED = ("approved", "applying")


class ApprovalSource(Protocol):
    def verify(self, change_id: str, approval_id: str | None) -> dict[str, Any]: ...


def _refuse(reason: str, **detail: Any) -> Stop:
    return Stop("refused", reason, Result.REFUSED, detail)


class DbApprovals:
    """Reads ``approvals`` joined to ``changes`` as role ``bravo_apply`` (migration 0004 grants and RLS)."""

    def __init__(self, dsn: str | None) -> None:
        self._dsn = dsn

    def verify(self, change_id: str, approval_id: str | None) -> dict[str, Any]:
        if not self._dsn:
            raise _refuse("approval_source_unavailable", why="BRAVO_APPLY_DATABASE_URL not set")
        if not approval_id or str(approval_id).lower() == "none":
            raise _refuse("approval_missing", change_id=change_id)
        try:
            aid, cid = uuid.UUID(str(approval_id)), uuid.UUID(str(change_id))
        except ValueError as exc:
            raise _refuse("approval_not_found", approval_id=str(approval_id)) from exc
        try:
            with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5) as conn:
                row = conn.execute(
                    """SELECT a.approval_id, a.plan_id, a.stage, a.change_id, a.approver_id, a.requester_id,
                              a.proposer_id, a.expires_at, a.expires_at > now() AS live, a.artifact_digest,
                              a.diff_digest, a.image_digest, a.policy_diff_digest,
                              c.change_id AS c_change_id, c.plan_id AS c_plan_id, c.status AS c_status,
                              c.approval_id AS c_approval_id
                         FROM approvals a LEFT JOIN changes c ON c.change_id = %s
                        WHERE a.approval_id = %s""",
                    (cid, aid),
                ).fetchone()
        except psycopg.Error as exc:
            raise _refuse("approval_source_unavailable", error=type(exc).__name__) from exc
        if row is None:
            raise _refuse("approval_not_found", approval_id=str(aid))
        evidence = {"approval_id": str(aid), "stage": row["stage"], "expires_at": row["expires_at"].isoformat()}
        if row["stage"] != "change":
            raise _refuse("approval_stage_mismatch", **evidence)
        if row["change_id"] != cid or row["c_change_id"] is None or row["c_plan_id"] != row["plan_id"]:
            raise _refuse("approval_change_mismatch", **evidence, change_id=str(cid))
        if row["c_status"] not in GRANTED:
            raise _refuse("approval_not_granted", **evidence, change_status=row["c_status"])
        if row["c_approval_id"] != aid:
            raise _refuse("approval_change_mismatch", **evidence, change_id=str(cid))
        if row["approver_id"] in (row["requester_id"], row["proposer_id"]):
            raise _refuse("approver_is_requester", **evidence)
        if not row["live"]:
            raise _refuse("approval_expired", **evidence)
        return {
            "approval_id": str(aid),
            "plan_id": str(row["plan_id"]),
            "change_id": str(cid),
            "artifact_digest": row["artifact_digest"],
            "diff_digest": row["diff_digest"],
            "image_digest": row["image_digest"],
            "policy_diff_digest": row["policy_diff_digest"],
            "expires_at": row["expires_at"].isoformat(),
            "source": "platform-db approvals (role bravo_apply)",
        }
