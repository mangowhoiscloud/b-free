"""Dev-only seed: one tenant and the three pilot principals (CONTRACT.md 7).

Subjects are derived, not stored anywhere else: ``uuid5(NAMESPACE_URL, 'bravo-dev:<email>')``.
``deploy/pilot/devauth/gen.py`` imports this module so tokens and DB rows always agree.
The issuer comes from env; nothing here is environment-specific.
"""

from __future__ import annotations

import uuid
from typing import Any

DEV_USERS: dict[str, tuple[str, str]] = {
    # short name -> (email, role)
    "operator": ("operator@bravo.test", "operator"),
    "approver": ("approver@bravo.test", "approver"),
    "viewer": ("viewer@bravo.test", "viewer"),
}


def dev_sub(email: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"bravo-dev:{email}"))


def seed(conn: Any, *, issuer: str, tenant_id: str) -> None:
    """Idempotent. ``conn`` is a sync psycopg connection owned by the migration runner."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tenant_id, f"Pilot tenant {tenant_id}"),
        )
        for email, role in DEV_USERS.values():
            conn.execute(
                "INSERT INTO principals (iss, sub, email) VALUES (%s, %s, %s) ON CONFLICT (iss, sub) DO NOTHING",
                (issuer, dev_sub(email), email),
            )
            conn.execute(
                """INSERT INTO memberships (principal_id, tenant_id, role)
                   SELECT id, %s, %s FROM principals WHERE iss = %s AND sub = %s
                   ON CONFLICT DO NOTHING""",
                (tenant_id, role, issuer, dev_sub(email)),
            )
