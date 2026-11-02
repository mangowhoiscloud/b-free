"""Idempotency-Key handling (§8.9). Scope (tenant, action, key); different input -> 409.

The claim row is inserted in the same transaction as the write. A concurrent request with
the same key blocks on the primary key until the first commits, then replays its response.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from .db import Conn
from .errors import BusinessError, IdempotencyConflict

_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")


def validate_key(key: str | None) -> str:
    if key is None or key == "":
        raise BusinessError("idempotency_key_required", status=400)
    if not _KEY.fullmatch(key):
        raise BusinessError("idempotency_key_invalid", status=400)
    return key


def request_digest(payload: dict[str, Any]) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


async def claim(
    conn: Conn, *, tenant_id: str, action: str, key: str, digest: str, principal_id: UUID
) -> dict[str, Any] | None:
    """Return None if this request owns the key, else the stored response (replay)."""
    cur = await conn.execute(
        """INSERT INTO idempotency (tenant_id, action, idem_key, request_digest, principal_id)
           VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING idem_key""",
        (tenant_id, action, key, digest, principal_id),
    )
    if await cur.fetchone() is not None:
        return None
    cur = await conn.execute(
        "SELECT request_digest, response FROM idempotency WHERE tenant_id = %s AND action = %s AND idem_key = %s",
        (tenant_id, action, key),
    )
    row = await cur.fetchone()
    if row is None:  # pragma: no cover - only if the owner rolled back between statements
        raise BusinessError("idempotency_retry", status=409)
    if row["request_digest"] != digest:
        raise IdempotencyConflict()
    if row["response"] is None:
        raise BusinessError("idempotency_in_progress", status=409)
    return dict(row["response"])


async def complete(
    conn: Conn, *, tenant_id: str, action: str, key: str, response: dict[str, Any], status_code: int = 200
) -> None:
    await conn.execute(
        """UPDATE idempotency SET response = %s, status_code = %s
            WHERE tenant_id = %s AND action = %s AND idem_key = %s""",
        (Jsonb(response), status_code, tenant_id, action, key),
    )
