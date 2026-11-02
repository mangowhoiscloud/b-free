"""psycopg 3 async pool. Every request transaction sets the tenant with SET LOCAL semantics.

``set_config(name, value, true)`` is the parameterised form of ``SET LOCAL``: the value is
dropped at commit/rollback, so it never leaks to the next request on a pooled connection.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .auth import Channel, Principal, Role, VerifiedToken, choose_tenant
from .config import AccessMembers
from .errors import ForbiddenError

Conn = AsyncConnection[dict[str, Any]]
LOG = logging.getLogger("bravo_api.db")


@dataclass(frozen=True, slots=True)
class Bootstrap:
    """First-login bootstrap (A3): ``email`` may become the first member of ``tenant_id`` with ``roles``."""

    email: str
    tenant_id: str
    roles: tuple[str, ...]


class Database:
    def __init__(self, url: str, min_size: int = 1, max_size: int = 10, bootstrap: Bootstrap | None = None,
                 members: AccessMembers | None = None) -> None:
        self._bootstrap = bootstrap
        self._members = members
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
        """Map (iss, sub) to a principal and its roles in one tenant.

        Unknown -> first-login bootstrap if configured and this is that exact email into an empty tenant,
        else 403 ``unknown_principal`` with the token's ``sub`` and ``email`` so an admin can grant them.
        """
        rows = await self._lookup(token)
        if not rows and (await self._try_jit(token) or await self._try_bootstrap(token)):
            rows = await self._lookup(token)
        if not rows:
            raise ForbiddenError("unknown_principal", {"sub": token.sub, "email": token.email})
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

    async def _lookup(self, token: VerifiedToken) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT principal_id, email, tenant_id, role FROM bravo_resolve_principal(%s, %s)",
                    (token.iss, token.sub),
                )
                return await cur.fetchall()

    async def _try_jit(self, token: VerifiedToken) -> bool:
        """First login of an allowlisted Access email -> its configured roles (never any other email)."""
        roles = self._members.roles_for(token.email) if self._members else None
        if not roles:
            return False
        assert self._members is not None
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT bravo_jit_principal(%s, %s, %s, %s, %s) AS created",
                    (token.iss, token.sub, token.email, self._members.tenant_id, list(roles)),
                )
                row = await cur.fetchone()
        created = bool(row and row["created"])
        if created:
            LOG.warning("access JIT membership: tenant=%s iss=%s sub=%s roles=%s", self._members.tenant_id, token.iss,
                        token.sub, ",".join(roles))
        return created

    async def _try_bootstrap(self, token: VerifiedToken) -> bool:
        b = self._bootstrap
        if b is None or token.email.strip().lower() != b.email.strip().lower():
            return False
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT bravo_bootstrap_principal(%s, %s, %s, %s, %s) AS created",
                    (token.iss, token.sub, token.email, b.tenant_id, list(b.roles)),
                )
                row = await cur.fetchone()
        created = bool(row and row["created"])
        if created:
            LOG.warning("first-login bootstrap: tenant=%s iss=%s sub=%s roles=%s", b.tenant_id, token.iss, token.sub,
                        ",".join(b.roles))
        return created

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
