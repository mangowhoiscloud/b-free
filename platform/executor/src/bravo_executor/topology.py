"""Cluster topology for the Executor (CLOUD-E2E-CONTRACT.md 2, 7-9).

``BRAVO_TOPOLOGY_FILE`` points to a JSON file without secrets. Passwords and tokens are named
by OpenBao path and field and read once at start-up with the ``executor`` AppRole (D-26).

{
  "label": "임시 링크(D-16 전)",
  "chat_db": "chat",
  "store_ids": {"src": "src", "dst": "dst"},
  "cluster_names": {"src": "chat-db", "dst": "chat-db"},
  "db": {   # direct control/observe connections, one per side (through the link)
    "src": {"conninfo": "host=127.0.0.1 port=15432", "secret": "envs/source-aws/chat-db"},
    "dst": {"conninfo": "host=127.0.0.1 port=25432", "secret": "envs/target-onprem/chat-db"}
  },
  "pools": [
    {"name": "src", "conninfo": "host=127.0.0.1 port=16432", "secret": "envs/source-aws/chat-db",
     "switch": {"kind": "k8s-exec", "context": "bravo-source", "namespace": "chat",
                "workload": "deploy/chat-pgbouncer"}}
  ],
  "entries": [
    {"name": "aws", "side": "src", "url": "http://127.0.0.1:18081", "secret": "envs/source-aws/chat-admin"}
  ]
}

Secret fields (``secret/<path>``): chat-db has ``executor_ctl``, ``executor_observe``,
``app_writer``, ``pgb_admin``, ``pgb_stats``; chat-admin has ``token``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from psycopg.conninfo import make_conninfo

from .attachment_copy import StoreConfig
from .config import ConfigError, Settings, tuning_from_env
from .entry import EntrySpec
from .pools import K8sExecSwitch, PoolSpec

Lookup = Callable[[str, str], Awaitable[str]]
log = logging.getLogger(__name__)


def _need(doc: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in doc:
        raise ConfigError(f"topology: {where}.{key} is required")
    return doc[key]


async def settings_from_topology(
    path: Path, lookup: Lookup, platform_dsn: str, env: Mapping[str, str] | None = None,
    require_entries: bool = True,
) -> Settings:
    """``require_entries=False`` (operator tools that never block writes) skips entries whose token
    is not delivered yet; the Executor itself always requires every entry."""
    doc = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    e = os.environ if env is None else env
    chat_db = _need(doc, "chat_db", "topology")
    db = _need(doc, "db", "topology")

    async def dsn(side_doc: Mapping[str, Any], user: str, dbname: str) -> str:
        password = await lookup(_need(side_doc, "secret", "db"), user)
        return make_conninfo(_need(side_doc, "conninfo", "db"), dbname=dbname, user=user, password=password,
                             application_name="bravo-executor")

    dsns: dict[str, dict[str, str]] = {}
    for side in ("src", "dst"):
        sd = _need(db, side, "db")
        dsns[side] = {
            "ctl": await dsn(sd, "executor_ctl", chat_db),
            "observe": await dsn(sd, "executor_observe", chat_db),
            "app": await dsn(sd, "app_writer", chat_db),
        }

    pools: list[PoolSpec] = []
    stats: list[tuple[str, str]] = []
    for p in _need(doc, "pools", "topology"):
        name = _need(p, "name", "pools[]")
        sw = _need(p, "switch", f"pools[{name}]")
        if sw.get("kind") != "k8s-exec":
            raise ConfigError(f"topology: pools[{name}].switch.kind must be k8s-exec")
        db_name = p.get("db_name", chat_db)
        secret = _need(p, "secret", f"pools[{name}]")
        conninfo = _need(p, "conninfo", f"pools[{name}]")
        pools.append(
            PoolSpec(
                name=name,
                admin_dsn=make_conninfo(conninfo, dbname="pgbouncer", user="pgb_admin",
                                        password=await lookup(secret, "pgb_admin")),
                app_dsn=make_conninfo(conninfo, dbname=db_name, user="app_writer",
                                      password=await lookup(secret, "app_writer")),
                db_name=db_name,
                switch=K8sExecSwitch(
                    context=_need(sw, "context", "switch"),
                    namespace=_need(sw, "namespace", "switch"),
                    workload=_need(sw, "workload", "switch"),
                    db_name=db_name,
                    targets_dir=sw.get("targets_dir", "/etc/pgbouncer/targets"),
                    active_path=sw.get("active_path", "/run/pgbouncer/active.ini"),
                    container=sw.get("container"),
                ),
            )
        )
        stats.append((name, make_conninfo(conninfo, dbname="pgbouncer", user="pgb_stats",
                                          password=await lookup(secret, "pgb_stats"))))

    entries: list[EntrySpec] = []
    for en in doc.get("entries", []):
        try:
            token = await lookup(_need(en, "secret", "entries[]"), "token")
        except Exception:
            if require_entries:
                raise
            log.warning("entry %s skipped: token not delivered", en.get("name"))
            continue
        entries.append(EntrySpec(name=_need(en, "name", "entries[]"), side=_need(en, "side", "entries[]"),
                                 url=_need(en, "url", "entries[]").rstrip("/"), token=token))
    store_ids = _need(doc, "store_ids", "topology")
    stores: list[tuple[str, StoreConfig]] = []
    for side, od in (doc.get("object_stores") or {}).items():
        secret = _need(od, "secret", f"object_stores.{side}")
        try:
            await lookup(secret, "bucket")
        except Exception as exc:  # noqa: BLE001 - no store for this side: receipts say NOT_CHECKED
            log.warning("object store %s not configured (%s): attachment objects are not copied", side,
                        type(exc).__name__)
            continue

        async def opt(name: str, secret: str = secret) -> str:
            try:
                return await lookup(secret, name)
            except Exception:  # noqa: BLE001 - optional field absent or empty
                return ""

        stores.append((side, StoreConfig(
            store_id=store_ids[side],
            bucket=await lookup(secret, "bucket"),
            endpoint_url=od.get("endpoint_url") or (await opt("endpoint_url")) or None,
            region=(await opt("region")) or None,
            addressing_style=od.get("addressing_style") or (await opt("addressing_style")) or "auto",
            access_key_id=(await opt("access_key_id")) or None,
            secret_access_key=(await opt("secret_access_key")) or None,
            conditional_put=od.get("conditional_put", True),
        )))
    names = doc.get("cluster_names", {"src": "src", "dst": "dst"})
    return Settings(
        platform_dsn=platform_dsn,
        src_dsn=dsns["src"]["ctl"],
        dst_dsn=dsns["dst"]["ctl"],
        src_app_dsn=dsns["src"]["app"],
        dst_app_dsn=dsns["dst"]["app"],
        src_observe_dsn=dsns["src"]["observe"],
        dst_observe_dsn=dsns["dst"]["observe"],
        chat_db=chat_db,
        src_store_id=store_ids["src"],
        dst_store_id=store_ids["dst"],
        pools=tuple(pools),
        pool_stats_dsns=tuple(stats),
        entries=tuple(entries),
        link_label=doc.get("label"),
        object_stores=tuple(stores),
        src_cluster_name=names["src"],
        dst_cluster_name=names["dst"],
        **tuning_from_env(e),
    )


async def _dsn_cli(argv: list[str]) -> int:
    """``python -m bravo_executor.topology dsn <pool> app|admin``: print one DSN to stdout for a
    child process environment (run under executor-bao.sh with-executor; never redirect to a file)."""
    import sys

    from .vault import SecretBroker

    if len(argv) != 3 or argv[0] != "dsn" or argv[2] not in ("app", "admin"):
        print("usage: python -m bravo_executor.topology dsn <pool-name> app|admin", file=sys.stderr)
        return 2
    broker = SecretBroker.from_env()
    try:
        s = await settings_from_topology(Path(os.environ["BRAVO_TOPOLOGY_FILE"]), broker.field, "unused",
                                         require_entries=False)
    finally:
        await broker.close()
    pool = next((p for p in s.pools if p.name == argv[1]), None)
    if pool is None:
        print(f"no pool {argv[1]!r}", file=sys.stderr)
        return 2
    sys.stdout.write(pool.app_dsn if argv[2] == "app" else pool.admin_dsn)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(_dsn_cli(sys.argv[1:])))


class FileSecrets:
    """In-cluster lookup: the platform delivers the OpenBao fields the topology names into a mounted
    Secret (deploy/platform/scripts/deliver-secrets.sh), one file per field:
    ``<BRAVO_SECRETS_DIR>/<path with "/" as "__">/<field>`` e.g. envs__source-aws__chat-db/executor_ctl.
    Read on demand; values never logged."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def field(self, path: str, name: str) -> str:
        f = self._root / path.replace("/", "__") / name
        value = (await asyncio.to_thread(f.read_text, encoding="utf-8")).strip() if f.exists() else ""
        if not value:
            raise ConfigError(f"secret file for {path} field {name} missing or empty")
        return value
