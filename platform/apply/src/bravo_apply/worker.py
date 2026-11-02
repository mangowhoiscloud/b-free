"""DB mode: poll ``changes`` as role ``bravo_apply`` and run prepare / apply (migration 0004 contract).

Transitions the worker may make (the ``bravo_changes_guard`` trigger enforces them):
  requested -> preparing -> prepared | prepare_failed
  approved  -> applying  -> applied | refused | failed
The worker never touches ``plans``; platform/api turns ``change_stages`` rows into SSE ``change.stage`` events and
owns every plan transition. A change left in ``preparing`` / ``applying`` by a crashed worker is not resumed
automatically (P0): the API shows it and a person decides.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import Config
from .pipeline import Change, apply, prepare
from .runtime import Result, Stop

LOG = logging.getLogger("bravo_apply.worker")


class DbRecorder:
    """One ``change_stages`` row per step, committed at once so the API can stream it."""

    def __init__(self, dsn: str, change_id: Any) -> None:
        self._dsn = dsn
        self._change_id = change_id

    def record(self, phase: str, name: str, started_at: datetime, ms: int, result: Result, detail: dict[str, Any]) -> None:
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            conn.execute(
                """INSERT INTO change_stages (change_id, seq, phase, name, started_at, ms, result, detail)
                   SELECT %s, COALESCE(max(seq), 0) + 1, %s, %s, %s, %s, %s, %s FROM change_stages WHERE change_id = %s""",
                (self._change_id, phase, name, started_at, ms, result.value, Jsonb(detail), self._change_id),
            )


class Worker:
    def __init__(self, cfg: Config, dsn: str, out_root: Path, worker_id: str | None = None) -> None:
        self.cfg = cfg
        self.dsn = dsn
        self.out_root = out_root
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    def _claim(self, conn: psycopg.Connection[dict[str, Any]], frm: str, to: str) -> dict[str, Any] | None:
        cur = conn.execute(
            """UPDATE changes SET status = %s, worker = %s
                WHERE change_id = (SELECT change_id FROM changes WHERE status = %s ORDER BY created_at
                                    LIMIT 1 FOR UPDATE SKIP LOCKED)
            RETURNING *""",
            (to, self.worker_id, frm),
        )
        return cur.fetchone()

    def _load(self, conn: psycopg.Connection[dict[str, Any]], row: dict[str, Any]) -> Change:
        art = conn.execute("SELECT artifact FROM rehearsals WHERE rehearsal_id = %s", (row["rehearsal_id"],)).fetchone()
        env = conn.execute("SELECT name FROM environments WHERE environment_id = %s", (row["target_environment_id"],)).fetchone()
        approval = None
        if row.get("approval_id"):
            a = conn.execute(
                "SELECT approval_id, change_id, image_digest, diff_digest, policy_diff_digest, artifact_digest FROM approvals "
                "WHERE approval_id = %s AND stage = 'change' AND change_id = %s",
                (row["approval_id"], row["change_id"]),
            ).fetchone()
            approval = {k: (str(v) if k.endswith("_id") and v else v) for k, v in a.items()} if a else None
        return Change(
            change_id=str(row["change_id"]),
            role=row["role"],
            target_env=env["name"] if env else "",
            artifact=art["artifact"] if art else None,
            bound={k: row[k] for k in ("artifact_digest", "diff_digest", "policy_diff_digest", "policy_targets")},
            approval=approval,
            prepared={"image_digest": row["image_digest"], "image_repository": row["image_repository"]},
        )

    def run_once(self) -> int:
        done = 0
        for frm, to, phase in (("requested", "preparing", "prepare"), ("approved", "applying", "apply")):
            with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
                row = self._claim(conn, frm, to)
                ch = self._load(conn, row) if row else None
            if row is None or ch is None:
                continue
            self._run(phase, row, ch)
            done += 1
        return done

    def _run(self, phase: str, row: dict[str, Any], ch: Change) -> None:
        out = self.out_root / ch.change_id
        out.mkdir(parents=True, exist_ok=True)
        rec = DbRecorder(self.dsn, row["change_id"])
        try:
            res = (prepare if phase == "prepare" else apply)(self.cfg, ch, rec, out)
        except Stop as stop:
            status = "prepare_failed" if phase == "prepare" else (stop.status if stop.status in ("refused", "failed") else "failed")
            self._finish(row["change_id"], {"status": status, "reason": stop.reason, "ended_at": "now"})
            LOG.warning("change %s %s: %s %s", ch.change_id, phase, status, stop.reason)
            return
        if phase == "prepare":
            self._finish(row["change_id"], {"status": "prepared", "image_repository": res["image_repository"],
                                            "image_digest": res["image_digest"], "sbom_digest": res["sbom_digest"],
                                            "scan": res["scan"], "build": res["build"], "prepared_at": "now"})
        else:
            self._finish(row["change_id"], {"status": "applied", "signature": res["signature"],
                                            "deployed_digest": res["deployed_digest"],
                                            "policies_applied": res["policies_applied"],
                                            "gate_recheck": res["gate_recheck"], "ended_at": "now"})

    def _finish(self, change_id: Any, values: dict[str, Any]) -> None:
        sets, params = [], []
        for k, v in values.items():
            if v == "now" and k.endswith("_at"):
                sets.append(f"{k} = now()")
                continue
            sets.append(f"{k} = %s")
            params.append(Jsonb(v) if isinstance(v, (dict, list)) else v)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(f"UPDATE changes SET {', '.join(sets)} WHERE change_id = %s", (*params, change_id))

    def loop(self, interval_s: float = 2.0) -> None:
        while True:
            try:
                if self.run_once() == 0:
                    time.sleep(interval_s)
            except psycopg.Error as exc:
                LOG.error("platform DB error: %s", json.dumps(str(exc))[:300])
                time.sleep(interval_s * 5)
