"""psycopg 3 async pool. Every request transaction sets the tenant with SET LOCAL semantics.

``set_config(name, value, true)`` is the parameterised form of ``SET LOCAL``: the value is
dropped at commit/rollback, so it never leaks to the next request on a pooled connection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .auth import Channel, Principal, Role, VerifiedToken, choose_tenant
from .errors import ForbiddenError

Conn = AsyncConnection[dict[str, Any]]


class Database:
    def __init__(self, url: str, min_size: int = 1, max_size: int = 10) -> None:
        self._pool: AsyncConnectionPool[Conn] = AsyncConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"row_factory": dict_row, "autocommit": False},
            name="bravo-platform-db",
        )

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=30)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def tenant_tx(self, tenant_id: str, principal_id: UUID | None = None) -> AsyncIterator[Conn]:
        """One transaction bound to ``tenant_id``. Commits on success, rolls back on error."""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', %s, true), set_config('app.principal_id', %s, true)",
                    (tenant_id, str(principal_id) if principal_id else ""),
                )
                yield conn

    async def resolve_principal(
        self, token: VerifiedToken, channel: Channel, requested_tenant: str | None = None
    ) -> Principal:
        """Map (iss, sub) to a principal and its roles in one tenant. Unknown -> 403."""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT principal_id, email, tenant_id, role FROM bravo_resolve_principal(%s, %s)",
                    (token.iss, token.sub),
                )
                rows = await cur.fetchall()
        if not rows:
            raise ForbiddenError("unknown_principal")
        tenant, roles = choose_tenant([(r["tenant_id"], r["role"]) for r in rows], requested_tenant)
        return Principal(
            id=rows[0]["principal_id"],
            iss=token.iss,
            sub=token.sub,
            email=rows[0]["email"],
            tenant_id=tenant,
            roles=roles,
            channel=channel,
        )

    async def resolve_ticket_principal(self, ticket: Any) -> Principal:
        """Principal for an SSE ticket: re-read current roles, so revocation applies at once."""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', %s, true), set_config('app.principal_id', %s, true)",
                    (ticket.tenant_id, str(ticket.principal_id)),
                )
                cur = await conn.execute(
                    "SELECT p.id, p.iss, p.sub, p.email, m.role FROM principals p "
                    "JOIN memberships m ON m.principal_id = p.id AND m.tenant_id = %s WHERE p.id = %s",
                    (ticket.tenant_id, ticket.principal_id),
                )
                rows = await cur.fetchall()
        if not rows:
            raise ForbiddenError("no_membership")
        roles = frozenset(Role(r["role"]) for r in rows if r["role"] in Role._value2member_map_)
        return Principal(
            id=rows[0]["id"],
            iss=rows[0]["iss"],
            sub=rows[0]["sub"],
            email=rows[0]["email"],
            tenant_id=ticket.tenant_id,
            roles=roles,
            channel=Channel.CONSOLE,
        )

    async def ping(self) -> bool:
        async with self._pool.connection() as conn:
            await conn.execute("SELECT 1")
        return True
