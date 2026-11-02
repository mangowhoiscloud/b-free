"""Platform admin: grant a real Cloudflare Access identity a role in a tenant (audit A3).

    python -m bravo_api.admin grant --iss https://<team>.cloudflareaccess.com --sub <Access sub> \\
        --email <email> --tenant <tenant_id> --role operator

    python -m bravo_api.admin register-envs --file deploy/platform/environments.json --created-by <email>

Runs as the database owner (``BRAVO_MIGRATE_DATABASE_URL``). The ``sub`` is the one the API reports in its
403 ``unknown_principal`` answer (``detail.sub``) after that person's first Access login, so nobody is
invented. ``operator`` and an approver role are never given to the same person in one tenant (D-18).
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg

ROLES = ("viewer", "operator", "approver", "security_approver")
APPROVERS = frozenset({"approver", "security_approver"})


def grant(url: str, *, iss: str, sub: str, email: str, tenant: str, role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if not iss.startswith("https://") or not sub or "@" not in email:
        raise ValueError("iss must be https, sub non-empty, email an address")
    with psycopg.connect(url, autocommit=True) as conn, conn.transaction():
        conn.execute("INSERT INTO tenants (tenant_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (tenant, tenant))
        row = conn.execute(
            """INSERT INTO principals (iss, sub, email) VALUES (%s, %s, %s)
               ON CONFLICT (iss, sub) DO UPDATE SET email = EXCLUDED.email RETURNING id""",
            (iss, sub, email),
        ).fetchone()
        assert row is not None
        pid = row[0]
        held = {r[0] for r in conn.execute(
            "SELECT role FROM memberships WHERE principal_id = %s AND tenant_id = %s", (pid, tenant))}
        if (role == "operator" and held & APPROVERS) or (role in APPROVERS and "operator" in held):
            raise ValueError("separation of duties: operator and an approver role in one tenant")
        conn.execute(
            "INSERT INTO memberships (principal_id, tenant_id, role) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (pid, tenant, role),
        )
    return str(pid)


def register_envs(url: str, entries: list[dict], *, tenant: str, created_by_email: str, resolve) -> list[str]:
    """Register the real environments named in a config file (beginner landing: the platform registers them).

    Every ``secret_ref`` is resolved against the platform OpenBao first (metadata only, ``resolve`` =
    ``SecretRefResolver.resolve``); anything not ``resolved`` is refused and nothing is written for it. The
    creator must be an existing member of the tenant (an Access identity that has logged in). Already
    registered names are left as they are.
    """
    import asyncio
    import uuid

    out: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT p.id FROM principals p JOIN memberships m ON m.principal_id = p.id "
            "WHERE lower(p.email) = lower(%s) AND m.tenant_id = %s AND m.role = 'operator' LIMIT 1",
            (created_by_email, tenant),
        ).fetchone()
        if row is None:
            raise ValueError(f"{created_by_email} is not an operator of {tenant} (log in through Access first)")
        creator = row[0]
        for e in entries:
            name, role, ref = e["name"], e["role"], e["secret_ref"]
            if conn.execute("SELECT 1 FROM environments WHERE tenant_id = %s AND name = %s", (tenant, name)).fetchone():
                out.append(f"{name}: already registered")
                continue
            res = asyncio.run(resolve(ref))
            if res.status != "resolved":
                out.append(f"{name}: refused ({ref}: {res.status} {res.reason})")
                continue
            conn.execute(
                """INSERT INTO environments (environment_id, tenant_id, name, role, engine, secret_ref, created_by,
                                            secret_status, secret_reason, secret_checked_at)
                   VALUES (%s, %s, %s, %s, 'postgres', %s, %s, 'resolved', NULL, now())""",
                (uuid.uuid4(), tenant, name, role, ref, creator),
            )
            out.append(f"{name}: registered ({role}, {ref} resolved)")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bravo_api.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grant", help="give an Access identity (iss, sub) a role in a tenant")
    for name in ("iss", "sub", "email", "tenant"):
        g.add_argument(f"--{name}", required=True)
    g.add_argument("--role", required=True, choices=ROLES)
    r = sub.add_parser("register-envs", help="register the real environments from a config file")
    r.add_argument("--file", required=True)
    r.add_argument("--created-by", required=True, help="email of an operator of the file's tenant")
    args = ap.parse_args(argv)
    url = os.environ.get("BRAVO_MIGRATE_DATABASE_URL")
    if not url:
        print("BRAVO_MIGRATE_DATABASE_URL is required (database owner)", file=sys.stderr)
        return 2
    if args.cmd == "register-envs":
        import json

        from .config import Settings
        from .secretrefs import BaoSettings, SecretRefResolver

        cfg = json.loads(open(args.file, encoding="utf-8").read())
        st = Settings.bao_from_env()
        resolver = SecretRefResolver(BaoSettings(*st))
        if not resolver.configured:
            print("refused: platform OpenBao not configured (BRAVO_BAO_ADDR + credentials): 연결 안 됨", file=sys.stderr)
            return 1
        try:
            lines = register_envs(url, cfg["environments"], tenant=cfg["tenant"], created_by_email=args.created_by,
                                  resolve=resolver.resolve)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print("\n".join(lines))
        return 0 if all("refused" not in line for line in lines) else 1
    try:
        pid = grant(url, iss=args.iss, sub=args.sub, email=args.email, tenant=args.tenant, role=args.role)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"granted {args.role} in {args.tenant} to principal {pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
