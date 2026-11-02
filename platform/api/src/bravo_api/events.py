"""Plan events: append under the plan row lock, list after a cursor, stream as SSE.

Event ids are a per-plan sequence assigned while the plan row is locked, so commit order
equals id order and a reconnecting client with ``Last-Event-ID`` never skips an event.
Shape (CONTRACT.md 7): {id, plan_id, state, kind, at, mode, run_id, actor:{kind, principal}, data}.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from .auth import Principal
from .db import Conn
from .errors import NotFoundError


class EventKind(StrEnum):
    STATE_CHANGED = "state.changed"
    GATE_VERDICT = "gate.verdict"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    RUN_STAGE = "run.stage"
    RUN_DONE = "run.done"
    RECEIPT_SAVED = "receipt.saved"
    ANALYSIS_DONE = "analysis.done"
    TRIAL_STARTED = "trial.started"
    TRIAL_PROPOSED = "trial.proposed"
    TOOL_DENIED = "tool.denied"
    TRIAL_VERDICT = "trial.verdict"
    # T31 change path (spec r2): requested -> stage* -> prepared -> approval -> stage* -> applied | refused | failed
    CHANGE_REQUESTED = "change.requested"
    CHANGE_STAGE = "change.stage"
    CHANGE_PREPARED = "change.prepared"
    CHANGE_PREPARE_FAILED = "change.prepare_failed"
    CHANGE_APPLIED = "change.applied"
    CHANGE_REFUSED = "change.refused"
    CHANGE_FAILED = "change.failed"
    # Control exception (spec S1 5b, contract 15 §9)
    EXCEPTION_REQUESTED = "exception.requested"
    EXCEPTION_APPROVED = "exception.approved"
    EXCEPTION_REJECTED = "exception.rejected"
    EXCEPTION_STALE = "exception.stale"
    EXCEPTION_EXPIRED = "exception.expired"


class EventMode(StrEnum):
    LIVE = "live"
    RECORDED = "recorded"


@dataclass(frozen=True, slots=True)
class Actor:
    kind: str  # console | mcp | system
    principal_id: UUID | None = None

    @classmethod
    def of(cls, principal: Principal) -> Actor:
        return cls(kind=principal.channel.value, principal_id=principal.id)


SYSTEM = Actor(kind="system")


async def emit(
    conn: Conn,
    *,
    plan_id: UUID,
    kind: EventKind,
    mode: EventMode,
    actor: Actor,
    data: dict[str, Any] | None = None,
    run_id: UUID | None = None,
) -> int:
    """Append one event. Takes the plan row lock (UPDATE) to assign the next sequence."""
    cur = await conn.execute(
        "UPDATE plans SET event_seq = event_seq + 1 WHERE plan_id = %s RETURNING event_seq, tenant_id, state",
        (plan_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError("plan")
    await conn.execute(
        """INSERT INTO events (plan_id, seq, tenant_id, state, kind, mode, run_id, actor_kind, actor_principal, data)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            plan_id,
            row["event_seq"],
            row["tenant_id"],
            row["state"],
            kind.value,
            mode.value,
            run_id,
            actor.kind,
            actor.principal_id,
            Jsonb(data or {}),
        ),
    )
    return int(row["event_seq"])


PLATFORM_OBJECT_PREFIX = "bravo_"  # barrier/fence objects the platform adds to the app schema


def _pair(value: Any) -> dict[str, Any] | None:
    return {"src": value.get("src"), "dst": value.get("dst")} if isinstance(value, dict) else None


def receipt_summary(body: dict[str, Any]) -> dict[str, Any]:
    """Flat {src, dst} pairs for the console's summary row, taken verbatim from the receipt.

    The Executor reports per table (``tables[]``) and per sequence (``sequences[]``). When the
    workload has exactly one application table / sequence (the pilot: ``messages``), its
    values are copied up as ``rows``, ``pk_hash``, ``content_hash`` and ``seq``. With several,
    ``rows`` is the sum and the hash pairs stay null: hashes are never combined or invented.
    """
    tables = [t for t in body.get("tables") or [] if isinstance(t, dict)]
    app_tables = [t for t in tables if not str(t.get("name", "")).startswith(PLATFORM_OBJECT_PREFIX)]
    seqs = [s for s in body.get("sequences") or [] if isinstance(s, dict)]
    app_seqs = [s for s in seqs if not str(s.get("name", "")).startswith(PLATFORM_OBJECT_PREFIX)]
    summary: dict[str, Any] = {}
    if len(app_tables) == 1:
        t = app_tables[0]
        summary.update(rows=_pair(t.get("rows")), pk_hash=_pair(t.get("pk_hash")), content_hash=_pair(t.get("content_hash")))
        summary["summary_of"] = {"table": t.get("name")}
    elif app_tables:
        rows = [_pair(t.get("rows")) or {} for t in app_tables]
        both = all(isinstance(r.get(k), int) for r in rows for k in ("src", "dst"))
        summary["rows"] = {"src": sum(r["src"] for r in rows), "dst": sum(r["dst"] for r in rows)} if both else None
        summary.update(pk_hash=None, content_hash=None, summary_of={"tables": [t.get("name") for t in app_tables]})
    if len(app_seqs) == 1:
        s = app_seqs[0]
        if "origin_last" in s:  # Executor receipts: origin = the side that took writes when the run started
            fb = body.get("direction") == "failback"
            src, dst = (s.get("target_set"), s.get("origin_last")) if fb else (s.get("origin_last"), s.get("target_set"))
        else:
            src, dst = s.get("src_last"), s.get("dst_set")
        summary["seq"] = {"src": src, "dst": dst}
        summary.setdefault("summary_of", {})["sequence"] = s.get("name")
    return summary


def verification_scope(body: dict[str, Any]) -> dict[str, Any]:
    """Contract 15 §11.4: what a receipt checked. Receipts written before 2026-11-03 have no ``scope``; the
    scope is then derived from what the body holds, and objects are "not_in_receipt" when it holds none."""
    scope = body.get("scope")
    if isinstance(scope, dict):
        return {"source": "receipt", **scope}
    names = [t.get("name") for t in body.get("tables") or [] if isinstance(t, dict)]
    seqs = [s.get("name") for s in body.get("sequences") or [] if isinstance(s, dict)]
    parts = body.get("verdict_parts")
    objects = parts.get("objects") if isinstance(parts, dict) else "not_in_receipt"
    return {"source": "derived", "hash_rule": body.get("hash_rule"), "tables": names, "sequences": seqs,
            "objects": objects}


def receipt_body(body: Any) -> dict[str, Any]:
    """The Executor's receipt body, unchanged, plus the console summary keys it lacks."""
    if not isinstance(body, dict):
        return {}
    out = dict(body)
    for key, value in receipt_summary(body).items():
        out.setdefault(key, value)
    out.setdefault("verification_scope", verification_scope(body))
    return out


def serialize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["seq"]),
        "plan_id": str(row["plan_id"]),
        "state": row["state"],
        "kind": row["kind"],
        "at": row["at"].isoformat(),
        "mode": row["mode"],
        "run_id": str(row["run_id"]) if row["run_id"] else None,
        "actor": {"kind": row["actor_kind"], "principal": row["email"]},
        "data": row["data"],
    }


async def list_events(conn: Conn, plan_id: UUID, after: int, limit: int) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """SELECT e.*, p.email FROM events e LEFT JOIN principals p ON p.id = e.actor_principal
            WHERE e.plan_id = %s AND e.seq > %s ORDER BY e.seq LIMIT %s""",
        (plan_id, after, limit),
    )
    return [serialize(r) for r in await cur.fetchall()]


def parse_cursor(value: str | None) -> int:
    if value is None or value.strip() == "":
        return 0
    try:
        cursor = int(value)
    except ValueError:
        return 0
    return max(cursor, 0)


def sse_payload(event: dict[str, Any]) -> dict[str, str]:
    return {"id": event["id"], "data": json.dumps(event, separators=(",", ":"), ensure_ascii=False)}
