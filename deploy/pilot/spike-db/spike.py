"""DB migration-matrix spikes against a real managed PostgreSQL (12-db-migration-matrix.md 11절).

Runs the Executor's own DB code (platform/executor: ChatDb, fenced, TableDigest, hash rule v1.1)
against a managed endpoint (RDS today, Cloud SQL next) and the laptop plain PostgreSQL 16 (C side).
The Executor package is imported read-only from platform/executor/src; nothing here changes it.

Every command prints one JSON document (also written to $SPIKE_RUN/<name>.json). No secret is
printed: passwords arrive in environment variables set by run.sh from OpenBao / Secrets Manager.

Environment (run.sh sets these):
  SPIKE_RUN            evidence directory
  M_DSN_<ROLE>         libpq DSN to the managed side for executor_ctl, app_writer, master (no password)
  C_DSN_<ROLE>         libpq DSN to the laptop PG for executor_ctl, app_writer, postgres (no password)
  M_PW_<ROLE>, C_PW_<ROLE>   passwords
  M_LINK_HOSTADDR      address the C-side subscriber uses to reach the managed side (forward link)
  M_LINK_PORT, M_NAME  port of that link and the provider name to verify
  C_LINK_HOST          address the managed subscriber uses to reach the C side (reverse link)
  M_SIDE_LABEL         rds | cloudsql
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import AsyncConnection, sql

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "platform" / "executor" / "src"))
from bravo_executor.chatdb import DIGEST_TABLES, SEQUENCES, ChatDb, wait_until  # noqa: E402
from bravo_executor.fence import FenceRejected, read_fence  # noqa: E402
from bravo_executor.hashing import HASH_RULE  # noqa: E402
from bravo_executor.pgbouncer import PgBouncerAdmin  # noqa: E402

RUN = Path(os.environ["SPIKE_RUN"])
LABEL = os.environ.get("M_SIDE_LABEL", "rds")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def dsn(side: str, role: str) -> str:
    base = os.environ[f"{side}_DSN_{role.upper()}"]
    return f"{base} password={os.environ[f'{side}_PW_{role.upper()}']}"


async def conn(side: str, role: str, autocommit: bool = True, app: str = "bravo-spike") -> AsyncConnection:
    return await AsyncConnection.connect(dsn(side, role), autocommit=autocommit, application_name=app)


def err(e: BaseException) -> dict[str, Any]:
    out: dict[str, Any] = {"type": type(e).__name__, "message": str(e).strip().splitlines()[0][:400]}
    diag = getattr(e, "diag", None)
    if diag is not None and getattr(diag, "sqlstate", None):
        out["sqlstate"] = diag.sqlstate
    return out


def emit(name: str, doc: dict[str, Any]) -> None:
    doc = {"spike": name, "side": LABEL, "at": now(), **doc}
    text = json.dumps(doc, indent=2, ensure_ascii=False, default=str)
    (RUN / f"{name}.json").write_text(text + "\n", encoding="utf-8")
    print(text)


async def try_sql(c: AsyncConnection, stmt: Any, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        cur = await c.execute(stmt, params)
        rows = await cur.fetchall() if cur.description else None
        return {"ok": True, "ms": round((time.monotonic() - t0) * 1000, 1), "rows": rows}
    except psycopg.Error as e:
        return {"ok": False, "ms": round((time.monotonic() - t0) * 1000, 1), "error": err(e)}


# ---- facts ----------------------------------------------------------------------------------

async def cmd_facts(_: argparse.Namespace) -> None:
    out: dict[str, Any] = {}
    for side, role in (("M", "master"), ("C", "postgres")):
        c = await conn(side, role)
        cur = await c.execute(
            """SELECT version(), current_setting('wal_level'), current_setting('max_slot_wal_keep_size'),
                      current_setting('max_replication_slots'), current_setting('ssl'),
                      current_setting('password_encryption'), current_setting('server_version_num')"""
        )
        v = await cur.fetchone()
        cur = await c.execute(
            "SELECT datname, datlocprovider, datcollate, datctype, datcollversion, pg_encoding_to_char(encoding) "
            "FROM pg_database WHERE datname = current_database()"
        )
        db = await cur.fetchone()
        cur = await c.execute(
            "SELECT rolname, rolsuper, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles "
            "WHERE rolname = current_user"
        )
        me = await cur.fetchone()
        cur = await c.execute(
            "SELECT b.rolname, m.admin_option FROM pg_auth_members m JOIN pg_roles b ON b.oid = m.roleid "
            "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = current_user ORDER BY 1"
        )
        member_of = await cur.fetchall()
        cur = await c.execute("SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
        ssl_row = await cur.fetchone()
        out["managed" if side == "M" else "self_run"] = {
            "version": v[0], "wal_level": v[1], "max_slot_wal_keep_size": v[2], "max_replication_slots": v[3],
            "ssl": v[4], "password_encryption": v[5], "server_version_num": v[6],
            "database": dict(zip(("datname", "datlocprovider", "datcollate", "datctype", "datcollversion",
                                  "encoding"), db, strict=True)),
            "current_user": dict(zip(("rolname", "super", "createrole", "replication", "bypassrls"), me, strict=True)),
            "member_of": [{"role": r, "admin": a} for r, a in member_of],
            "this_session_ssl": dict(zip(("ssl", "version", "cipher"), ssl_row, strict=True)) if ssl_row else None,
        }
        await c.close()
    emit("facts", out)


# ---- privileges that the stock schema and the Executor need --------------------------------

async def cmd_privileges(_: argparse.Namespace) -> None:
    """Calls the Executor's catalog reads on the managed side as executor_ctl and the master user."""
    out: dict[str, Any] = {}
    c = await conn("M", "executor_ctl")
    db = ChatDb(c, "src")
    for name, coro in (
        ("hba_rules", db.hba_rules()),
        ("role_has_password(app_writer)", db.role_has_password("app_writer")),
        ("writer_state(app_writer)", db.writer_state("app_writer")),
        ("schema_hash", db.schema_hash()),
        ("settings", db.settings(("wal_level", "rds.force_ssl", "ssl", "max_slot_wal_keep_size",
                                  "rds.logical_replication", "cloudsql.logical_decoding"))),
    ):
        try:
            out[name] = {"ok": True, "value": await coro}
        except psycopg.Error as e:
            out[name] = {"ok": False, "error": err(e)}
    await c.close()
    m = await conn("M", "master")
    out["master: SELECT FROM pg_authid"] = await try_sql(m, "SELECT count(*) FROM pg_authid")
    out["master: SELECT FROM pg_shadow (PgBouncer auth_query)"] = await try_sql(
        m, "SELECT usename, passwd IS NOT NULL FROM pg_shadow WHERE usename = 'app_writer'")
    out["master: pg_hba_file_rules"] = await try_sql(m, "SELECT count(*) FROM pg_hba_file_rules")
    out["master: CREATE ROLE x REPLICATION"] = await try_sql(m, "CREATE ROLE spk_repl_attr LOGIN REPLICATION")
    await try_sql(m, "DROP ROLE IF EXISTS spk_repl_attr")
    out["master: ALTER ROLE executor_ctl REPLICATION"] = await try_sql(m, "ALTER ROLE executor_ctl REPLICATION")
    out["master: pg_replication_origin_advance grant"] = await try_sql(
        m, "GRANT EXECUTE ON FUNCTION pg_replication_origin_advance(text, pg_lsn) TO executor_ctl")
    await m.close()
    emit("privileges", out)


# ---- seed -----------------------------------------------------------------------------------

BODIES = ["안녕하세요 반갑습니다", "Hello World", "hello world", "  leading spaces", "tab\there", "pipe|back\\slash",
          "line1\nline2", "日本語テキスト", "émoji 🙂 mixed", "UPPER lower Mixed", "ㄱㄴㄷ", "가나다라"]


async def cmd_seed(args: argparse.Namespace) -> None:
    c = await conn("M", "app_writer")
    t0 = time.monotonic()
    async with c.transaction():
        await c.execute(
            """
            INSERT INTO messages (room, author, body, idem_key, created_at)
            SELECT 'room-' || (g %% 7), 'user-' || (g %% 101), (%s::text[])[1 + g %% %s] || ' #' || g,
                   gen_random_uuid(), now() - make_interval(secs => g)
            FROM generate_series(1, %s) AS g
            """,
            (BODIES, len(BODIES), args.rows),
        )
    seed_ms = (time.monotonic() - t0) * 1000
    # attachments (uuid PK, hash rule v1.1 TEXT_PK branch) + message_attachments
    att = []
    async with c.transaction():
        for i in range(args.attachments):
            sha = "".join(random.choice("0123456789abcdef") for _ in range(64))
            cur = await c.execute(
                """INSERT INTO attachments (logical_id, idem_key, room, author, object_key, sha256, size_bytes,
                                            content_type, filename, store_id)
                   VALUES (gen_random_uuid(), gen_random_uuid(), 'room-1', 'user-1', 'att/' || %s, %s, %s,
                           'text/plain', %s, 'src') RETURNING id""",
                (sha, sha, 100 + i, f"파일 {i}.txt"),
            )
            att.append((await cur.fetchone())[0])
        cur = await c.execute("SELECT id FROM messages ORDER BY id LIMIT %s", (len(att),))
        mids = [r[0] for r in await cur.fetchall()]
        for mid, aid in zip(mids, att, strict=False):
            await c.execute("INSERT INTO message_attachments (message_id, attachment_id) VALUES (%s, %s)", (mid, aid))
    cur = await c.execute("SELECT (SELECT count(*) FROM messages), (SELECT count(*) FROM attachments)")
    counts = await cur.fetchone()
    await c.close()
    emit("seed", {"rows_requested": args.rows, "insert_ms": round(seed_ms), "messages": counts[0],
                  "attachments": counts[1]})


# ---- forward subscription: managed publisher -> C subscriber ----------------------------------

async def cmd_fwd_sub(args: argparse.Namespace) -> None:
    m_name = os.environ["M_NAME"]
    hostaddr = os.environ["M_LINK_HOSTADDR"]
    port = os.environ["M_LINK_PORT"]
    ca = "/tls/rds-ap-northeast-2-bundle.pem" if LABEL == "rds" else "/tls/cloudsql-server-ca.pem"
    base = f"dbname=chat user=replicator password={os.environ['M_PW_REPLICATOR']} port={port} connect_timeout=10"
    out: dict[str, Any] = {"link": "임시 링크(D-16 전): kubectl port-forward -> in-VPC TCP pod -> managed DB"}
    c = await conn("C", "executor_ctl")
    # negative probes first (N6 d): wrong name and missing CA must fail at CREATE SUBSCRIPTION (connect = true)
    probes = {
        "wrong_name_verify_full": f"host=not-the-provider.example hostaddr={hostaddr} {base} sslmode=verify-full sslrootcert={ca}",
        "ip_literal_verify_full": f"host={hostaddr} {base} sslmode=verify-full sslrootcert={ca}",
        "provider_name_wrong_ca": f"host={m_name} hostaddr={hostaddr} {base} sslmode=verify-full sslrootcert=/tls/ca.crt",
        "plaintext_disable": f"host={m_name} hostaddr={hostaddr} {base} sslmode=disable",
    }
    for name, ci in probes.items():
        r = await try_sql(c, sql.SQL("CREATE SUBSCRIPTION {} CONNECTION {} PUBLICATION bravo_fwd "
                                     "WITH (enabled = false, create_slot = false, slot_name = NONE)").format(
            sql.Identifier(f"probe_{name}"), sql.Literal(ci)))
        out[f"probe:{name}"] = r
        if r["ok"]:
            await try_sql(c, f"DROP SUBSCRIPTION probe_{name}")
    good = f"host={m_name} hostaddr={hostaddr} {base} sslmode=verify-full sslrootcert={ca}"
    t0 = time.monotonic()
    r = await try_sql(c, sql.SQL("CREATE SUBSCRIPTION bravo_fwd_sub CONNECTION {} PUBLICATION bravo_fwd "
                                 "WITH (origin = none)").format(sql.Literal(good)))
    out["create_bravo_fwd_sub_as_executor_ctl"] = r
    if not r["ok"]:
        emit("fwd-sub", out)
        return

    async def synced() -> bool:
        cur = await c.execute(
            """SELECT count(*) FILTER (WHERE r.srsubstate <> 'r'),
                      (SELECT count(*) FROM pg_stat_subscription WHERE subname = 'bravo_fwd_sub'
                        AND relid IS NULL AND pid IS NOT NULL)
               FROM pg_subscription_rel r JOIN pg_subscription s ON s.oid = r.srsubid
               WHERE s.subname = 'bravo_fwd_sub'""")
        pending, streaming = await cur.fetchone()
        return pending == 0 and streaming == 1

    ok, waited = await wait_until(synced, args.timeout, 0.5)
    out["initial_sync"] = {"ok": ok, "ms_from_create": round((time.monotonic() - t0) * 1000), "waited_ms": round(waited)}
    db = ChatDb(c, "dst")
    out["subscription"] = (await db.subscription("bravo_fwd_sub")).as_dict()
    cur = await c.execute("SELECT subname, subowner::regrole::text, suborigin, subenabled, subpasswordrequired, "
                          "subrunasowner FROM pg_subscription")
    out["pg_subscription"] = await cur.fetchall()
    m = await conn("M", "master")
    cur = await m.execute("SELECT slot_name, plugin, slot_type, active, wal_status, safe_wal_size FROM pg_replication_slots")
    out["managed_slots"] = await cur.fetchall()
    cur = await m.execute("SELECT usename, application_name, s.ssl, s.version FROM pg_stat_replication r "
                          "LEFT JOIN pg_stat_ssl s USING (pid)")
    out["managed_walsenders"] = await cur.fetchall()
    await m.close()
    await c.close()
    emit("fwd-sub", out)


# ---- digest (hash rule v1.1) ----------------------------------------------------------------

async def digests(side: str, role: str, tz: str | None = None, datestyle: str = "ISO, DMY") -> dict[str, Any]:
    c = await conn(side, role)
    if tz:
        await c.execute(sql.SQL("SET TimeZone = {}").format(sql.Literal(tz)))
        await c.execute(sql.SQL("SET DateStyle = {}").format(sql.Literal(datestyle)))
        await c.execute("SET extra_float_digits = 0")
        await c.execute("SET IntervalStyle = 'sql_standard'")
    db = ChatDb(c, side)
    t0 = time.monotonic()
    tables = {t: (await db.table_digest(t)).as_dict() for t in DIGEST_TABLES}
    seqs = {s: await db.sequence_last(s) for s in SEQUENCES}
    ms = round((time.monotonic() - t0) * 1000)
    await c.close()
    return {"tables": tables, "sequences": seqs, "ms": ms}


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    rows = {}
    for t in DIGEST_TABLES:
        rows[t] = {"rows": [a["tables"][t]["rows"], b["tables"][t]["rows"]],
                   "pk_hash_equal": a["tables"][t]["pk_hash"] == b["tables"][t]["pk_hash"],
                   "content_hash_equal": a["tables"][t]["content_hash"] == b["tables"][t]["content_hash"],
                   "content_hash": a["tables"][t]["content_hash"][:16]}
    match = all(r["pk_hash_equal"] and r["content_hash_equal"] and r["rows"][0] == r["rows"][1] for r in rows.values())
    return {"verdict": "MATCH" if match else "MISMATCH", "tables": rows}


async def cmd_digest(args: argparse.Namespace) -> None:
    m = await digests("M", "executor_ctl")
    c = await digests("C", "executor_ctl")
    out: dict[str, Any] = {"hash_rule": HASH_RULE, "managed_ms": m["ms"], "self_run_ms": c["ms"],
                           "managed_vs_self_run": compare(m, c),
                           "sequences": {"managed": m["sequences"], "self_run": c["sequences"]}}
    if args.session_variants:
        mt = await digests("M", "executor_ctl", tz="Asia/Tokyo")
        out["managed_default_vs_tokyo_iso_dmy_session"] = compare(m, mt)["verdict"]
        try:
            await digests("M", "executor_ctl", tz="Asia/Tokyo", datestyle="SQL, DMY")
            out["managed_datestyle_sql_dmy"] = "digest completed"
        except Exception as e:  # noqa: BLE001
            out["managed_datestyle_sql_dmy"] = {"fails_closed": True, **err(e)}
    emit(args.name, out)


# ---- reverse subscription on the managed side (SPK-02, SPK-03) -------------------------------

async def cmd_rev_sub(args: argparse.Namespace) -> None:
    host = os.environ["C_LINK_HOST"]
    pw = os.environ["C_PW_REPLICATOR"]
    base = f"host={host} port=5432 dbname=chat user=replicator password={pw} connect_timeout=10"
    out: dict[str, Any] = {"link": "managed DB -> in-VPC relay pod IP -> 임시 링크(D-16 전) reverse tunnel -> laptop PG",
                           "c_link_host": host}
    c = await conn("M", "executor_ctl")
    cur = await c.execute("SELECT b.rolname FROM pg_auth_members m JOIN pg_roles b ON b.oid = m.roleid "
                          "WHERE m.member = 'executor_ctl'::regrole ORDER BY 1")
    out["executor_ctl_member_of"] = [r[0] for r in await cur.fetchall()]
    probes = {
        # (a) design option 1: public-CA cert + system trust store. Our publisher has a private CA cert,
        #     so success is not expected; the error shows whether the server's libpq knows "system".
        "verify_full_sslrootcert_system": f"{base} sslmode=verify-full sslrootcert=system",
        # no CA file can be placed on a managed server: default ~/.postgresql/root.crt
        "verify_full_default_rootcert": f"{base} sslmode=verify-full",
        "verify_ca_default_rootcert": f"{base} sslmode=verify-ca",
        # (b) design option 2: encryption + SCRAM channel binding, no name check
        "require_channel_binding": f"{base} sslmode=require channel_binding=require",
        "plaintext_disable": f"{base} sslmode=disable",
    }
    for name, ci in probes.items():
        r = await try_sql(c, sql.SQL("CREATE SUBSCRIPTION {} CONNECTION {} PUBLICATION bravo_rev "
                                     "WITH (enabled = false, create_slot = false, slot_name = NONE, "
                                     "copy_data = false, origin = none)").format(
            sql.Identifier(f"probe_{name}"), sql.Literal(ci)))
        out[f"probe:{name}"] = r
        if r["ok"]:
            await try_sql(c, f"DROP SUBSCRIPTION probe_{name}")
    mode = args.sslmode
    ci = f"{base} sslmode={mode}" + (" channel_binding=require" if mode == "require" else "")
    r = await try_sql(c, sql.SQL(
        "CREATE SUBSCRIPTION bravo_rev_sub CONNECTION {} PUBLICATION bravo_rev WITH (enabled = false, "
        "copy_data = false, origin = none, create_slot = false, slot_name = 'bravo_rev_sub')").format(
        sql.Literal(ci)))
    out["create_bravo_rev_sub_as_executor_ctl"] = r
    cur = await c.execute("SELECT subname, subowner::regrole::text, suborigin, subenabled, subpasswordrequired, "
                          "subrunasowner, subslotname FROM pg_subscription")
    out["pg_subscription"] = await cur.fetchall()
    out["origin_advance_as_executor_ctl"] = await try_sql(
        c, "SELECT pg_replication_origin_advance('pg_' || oid, '0/0') FROM pg_subscription "
           "WHERE subname = 'bravo_rev_sub'")
    await c.close()
    m = await conn("M", "master")
    out["origin_advance_as_master"] = await try_sql(
        m, "SELECT pg_replication_origin_advance('pg_' || oid, pg_replication_origin_progress('pg_' || oid, false)) "
           "FROM pg_subscription WHERE subname = 'bravo_rev_sub'")
    out["subconninfo_readable_by_master"] = await try_sql(
        m, "SELECT subconninfo IS NOT NULL FROM pg_subscription WHERE subname = 'bravo_rev_sub'")
    await m.close()
    emit("rev-sub", out)


# ---- cutover DB steps with the Executor's code (SPK-04 + both directions) -------------------

class Writer:
    """app_writer load on one side: one INSERT per interval, reconnecting on failure."""

    def __init__(self, side: str, interval: float) -> None:
        self.side, self.interval = side, interval
        self.ok: list[float] = []
        self.errors: list[dict[str, Any]] = []
        self.stop = asyncio.Event()

    async def run(self) -> None:
        c: AsyncConnection | None = None
        while not self.stop.is_set():
            try:
                if c is None or c.closed:
                    load_dsn = dsn(self.side, "app_writer")
                    if os.environ.get("M_LOAD_PORT") and self.side == "M":
                        load_dsn = load_dsn.replace(f"port={os.environ['M_LINK_PORT']}", f"port={os.environ['M_LOAD_PORT']}")
                    c = await AsyncConnection.connect(load_dsn, autocommit=True, application_name="bravo-spike-writer")
                await c.execute("INSERT INTO messages (room, author, body, idem_key) VALUES ('load', 'w', %s, %s)",
                                (f"load {time.time():.3f}", uuid.uuid4()))
                self.ok.append(time.time())
            except psycopg.Error as e:
                e2 = err(e)
                e2["t"] = time.time()
                if not self.errors or self.errors[-1]["message"] != e2["message"]:
                    self.errors.append(e2)
                if c is not None:
                    await c.close()
                c = None
            await asyncio.sleep(self.interval)
        if c is not None:
            await c.close()


async def timed(rec: dict[str, Any], name: str, coro: Any) -> Any:
    t0 = time.monotonic()
    try:
        v = await coro
        rec[name] = {"ok": True, "ms": round((time.monotonic() - t0) * 1000, 1)}
        return v
    except (psycopg.Error, FenceRejected) as e:
        rec[name] = {"ok": False, "ms": round((time.monotonic() - t0) * 1000, 1), "error": err(e)}
        raise


async def cmd_cutover(args: argparse.Namespace) -> None:
    src = ChatDb(await conn("M", "executor_ctl", app="bravo-spike-executor"), "src")
    dst = ChatDb(await conn("C", "executor_ctl", app="bravo-spike-executor"), "dst")
    epoch = max(await read_fence(src.conn) or 0, await read_fence(dst.conn) or 0) + 1
    steps: dict[str, Any] = {}
    out: dict[str, Any] = {"epoch": epoch, "direction": "cutover managed(src) -> self-run(dst)", "steps": steps}
    out["pre"] = {"fwd": (await dst.subscription("bravo_fwd_sub")).as_dict(),
                  "rev": (await src.subscription("bravo_rev_sub")).as_dict(),
                  "fwd_slot_lag_bytes": await src.slot_lag_bytes(
                      await dst.subscription_slot_name("bravo_fwd_sub") or "bravo_fwd_sub"),
                  "rev_slot_on_dst": await dst.slot("bravo_rev_sub")}
    w = Writer("M", args.write_interval)
    wt = asyncio.ensure_future(w.run())
    await asyncio.sleep(args.warmup)
    try:
        await timed(steps, "fence_touch_src", src.touch_fence(epoch))
        await timed(steps, "fence_touch_dst", dst.touch_fence(epoch))
        t4 = time.monotonic()
        fence_wall = time.time()
        await timed(steps, "4a_src_app_writer_NOLOGIN", src.set_role_login(epoch, "app_writer", False))
        await timed(steps, "4b_src_REVOKE_INSERT", src.set_insert_privilege(epoch, "app_writer", False))
        revoke_wall = time.time()
        killed = await timed(steps, "4c_src_terminate_sessions", src.terminate_role_sessions(epoch, "app_writer"))
        steps["4_total_ms"] = round((time.monotonic() - t4) * 1000, 1)
        steps["4c_sessions_terminated"] = killed
        steps["4_writer_state"] = await src.writer_state("app_writer")
        steps["4_remaining_sessions"] = await src.role_session_count("app_writer")
        await asyncio.sleep(1.0)
        w.stop.set()
        await wt
        out["writer"] = {"ok_writes": len(w.ok), "revoke_committed_after_fence_start_s": round(revoke_wall - fence_wall, 3),
                         "last_ok_after_fence_start_s": round(max(w.ok) - fence_wall, 3) if w.ok else None,
                         "ok_writes_after_revoke_commit": sum(1 for t in w.ok if t > revoke_wall),
                         "errors": w.errors[:6]}
        bid, bat = await timed(steps, "5a_barrier_insert_src", src.insert_barrier(epoch, "spike-plan"))

        async def barrier_on_dst() -> bool:
            return await dst.barrier_present(bid)

        ok, waited = await wait_until(barrier_on_dst, 30, 0.02)
        steps["5b_barrier_seen_on_dst"] = {"ok": ok, "ms": round(waited, 1)}
        for s in SEQUENCES:
            v = await src.sequence_last(s)
            await timed(steps, f"5c_raise_{s}", dst.raise_sequence(epoch, s, v))
        t5 = time.monotonic()
        m = await digests("M", "executor_ctl")
        c = await digests("C", "executor_ctl")
        steps["5d_compare_ms"] = round((time.monotonic() - t5) * 1000, 1)
        out["receipt_cutover"] = {"hash_rule": HASH_RULE, "barrier_id": bid, "barrier_at": bat, **compare(m, c),
                                  "sequences": {"src": m["sequences"], "dst": c["sequences"]}}
        await timed(steps, "6a_dst_disable_fwd", dst.set_subscription_enabled(epoch, "bravo_fwd_sub", False))
        await timed(steps, "6a_src_enable_rev", src.set_subscription_enabled(epoch, "bravo_rev_sub", True))

        async def rev_streaming() -> bool:
            return (await src.subscription("bravo_rev_sub")).streaming

        ok, waited = await wait_until(rev_streaming, 60, 0.1)
        steps["6a_rev_streaming"] = {"ok": ok, "ms": round(waited, 1)}
        slot = await dst.subscription_slot_name("bravo_fwd_sub")
        await timed(steps, "6b_dst_detach_fwd_slot", dst.detach_subscription_slot(epoch, "bravo_fwd_sub"))
        try:
            await timed(steps, "6b_src_drop_fwd_slot_as_executor_ctl", src.drop_slot(epoch, slot or "bravo_fwd_sub"))
        except psycopg.Error:
            pass
        await timed(steps, "7_dst_app_writer_LOGIN", dst.set_role_login(epoch, "app_writer", True))
        # writes on the new primary flow back to the managed DB through the reverse subscription
        wc = await conn("C", "app_writer")
        t8 = time.monotonic()
        last = None
        for i in range(args.reverse_rows):
            cur = await wc.execute("INSERT INTO messages (room, author, body, idem_key) VALUES ('after', 'dst', %s, %s) "
                                   "RETURNING id", (f"after cutover {i} 한글", uuid.uuid4()))
            last = (await cur.fetchone())[0]
        await wc.close()

        async def back_on_src() -> bool:
            cur = await src.conn.execute("SELECT 1 FROM messages WHERE id = %s", (last,))
            return await cur.fetchone() is not None

        ok, waited = await wait_until(back_on_src, 60, 0.05)
        steps["8_reverse_rows_visible_on_src"] = {"ok": ok, "rows": args.reverse_rows, "ms_after_last_insert":
                                                  round(waited, 1), "ms_total": round((time.monotonic() - t8) * 1000)}
        m = await digests("M", "executor_ctl")
        c = await digests("C", "executor_ctl")
        out["receipt_after_reverse"] = {"hash_rule": HASH_RULE, **compare(m, c)}
        out["slots_after"] = {"src": await src.slot(slot or "bravo_fwd_sub"), "dst_rev": await dst.slot("bravo_rev_sub")}
        # E5: a late command with the old epoch must be rejected by the resource
        try:
            await src.touch_fence(epoch - 1)
            out["E5_old_epoch_on_src"] = "ACCEPTED (unexpected)"
        except FenceRejected as e:
            out["E5_old_epoch_on_src"] = f"rejected: {e}"
    except Exception as e:  # noqa: BLE001 - record and stop; this is a spike
        out["stopped_on"] = err(e)
        w.stop.set()
    finally:
        await src.close()
        await dst.close()
    emit(args.name, out)


# ---- SPK-09: text PK collation and session settings ------------------------------------------

SPK09_KEYS = ["a", "B", "b", "A", "가", "나", "ㄱ", "a b", "ab", "a-b", "a_b", "Z", "z", "é", "e", "日本", "1", "10",
              "9", " leading", "trailing ", "a\tb", "Ä", "ä", "ß", "ss", "🙂", "Ω", "ω", "가나", "가a", "a가"]


async def cmd_spk09(args: argparse.Namespace) -> None:
    out: dict[str, Any] = {"keys": len(SPK09_KEYS)}
    for side, role in (("M", "master"), ("C", "postgres")):
        c = await conn(side, role)
        await c.execute("DROP TABLE IF EXISTS spk09_text_pk")
        await c.execute("CREATE TABLE spk09_text_pk (id text PRIMARY KEY, ts timestamptz, f float8, n numeric, "
                        "j jsonb, b bytea)")
        for i, k in enumerate(SPK09_KEYS):
            await c.execute("INSERT INTO spk09_text_pk VALUES (%s, '2026-11-02 12:34:56.789012+09', %s, %s, %s, %s)",
                            (k, 0.1 * (i + 1) / 3, f"{i}.{i}00", json.dumps({"k": k, "i": i}), k.encode()))
        res: dict[str, Any] = {}
        for label, settings in (("utc_iso", ["SET TimeZone='UTC'", "SET DateStyle='ISO'", "SET IntervalStyle='postgres'",
                                              "SET extra_float_digits=3", "SET bytea_output='hex'"]),
                                ("tokyo_sql_dmy", ["SET TimeZone='Asia/Tokyo'", "SET DateStyle='SQL, DMY'",
                                                   "SET extra_float_digits=0", "SET bytea_output='escape'"])):
            for s in settings:
                await c.execute(s)
            cur = await c.execute(
                """SELECT md5(string_agg(t::text, '|' ORDER BY id COLLATE "C")),
                          md5(string_agg(t::text, '|' ORDER BY id)),
                          string_agg(id, ',' ORDER BY id COLLATE "C"),
                          string_agg(id, ',' ORDER BY id)
                   FROM spk09_text_pk t""")
            c_hash, db_hash, c_order, db_order = await cur.fetchone()
            res[label] = {"c_hash": c_hash, "db_hash": db_hash, "c_order": c_order, "db_order": db_order}
        cur = await c.execute("SELECT datlocprovider, datcollate, datcollversion FROM pg_database "
                              "WHERE datname = current_database()")
        res["collation"] = await cur.fetchone()
        cur = await c.execute("SELECT extname, extversion FROM pg_extension ORDER BY 1")
        res["extensions"] = await cur.fetchall()
        out["managed" if side == "M" else "self_run"] = res
        await c.execute("DROP TABLE spk09_text_pk")
        await c.close()
    m, s = out["managed"], out["self_run"]
    out["verdict"] = {
        "C_collate_hash_equal_across_engines_utc_iso": m["utc_iso"]["c_hash"] == s["utc_iso"]["c_hash"],
        "default_collate_order_equal_across_engines": m["utc_iso"]["db_order"] == s["utc_iso"]["db_order"],
        "C_vs_default_order_differs_within_engine": m["utc_iso"]["c_order"] != m["utc_iso"]["db_order"],
        "sql_text_hash_changes_with_session_settings": m["utc_iso"]["c_hash"] != m["tokyo_sql_dmy"]["c_hash"],
    }
    emit("spk09", out)


async def cmd_probe(args: argparse.Namespace) -> None:
    """New connection + SELECT current_setting('wal_level') every interval; JSON line per attempt."""
    out = RUN / f"{args.name}.jsonl"
    end = time.monotonic() + args.seconds
    with out.open("a", encoding="utf-8") as fh:
        while time.monotonic() < end:
            t = time.time()
            rec: dict[str, Any] = {"t": round(t, 3)}
            try:
                c = await asyncio.wait_for(AsyncConnection.connect(dsn("M", "master") + " connect_timeout=2",
                                                                   autocommit=True), 3)
                cur = await c.execute("SELECT current_setting('wal_level'), pg_postmaster_start_time()")
                wl, started = await cur.fetchone()
                await c.close()
                rec.update(ok=True, wal_level=wl, postmaster_start=started.isoformat())
            except (psycopg.Error, TimeoutError, OSError) as e:
                rec.update(ok=False, error=err(e)["message"][:160])
            rec["ms"] = round((time.time() - t) * 1000)
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            await asyncio.sleep(max(0.0, args.interval - (time.time() - t)))


async def cmd_failback(args: argparse.Namespace) -> None:
    """Failback with the Executor's code: self-run (now primary) -> managed. RDS-side setval, subscription
    disable, LOGIN/GRANT run as executor_ctl on the managed DB."""
    cur_p = ChatDb(await conn("C", "executor_ctl", app="bravo-spike-executor"), "dst")
    old = ChatDb(await conn("M", "executor_ctl", app="bravo-spike-executor"), "src")
    epoch = max(await read_fence(cur_p.conn) or 0, await read_fence(old.conn) or 0) + 1
    steps: dict[str, Any] = {}
    out: dict[str, Any] = {"epoch": epoch, "direction": "failback self-run(primary) -> managed", "steps": steps}
    try:
        await timed(steps, "fence_touch_self_run", cur_p.touch_fence(epoch))
        await timed(steps, "fence_touch_managed", old.touch_fence(epoch))
        t4 = time.monotonic()
        await timed(steps, "4a_self_run_NOLOGIN", cur_p.set_role_login(epoch, "app_writer", False))
        await timed(steps, "4b_self_run_REVOKE_INSERT", cur_p.set_insert_privilege(epoch, "app_writer", False))
        steps["4c_terminated"] = await timed(steps, "4c_self_run_terminate", cur_p.terminate_role_sessions(epoch, "app_writer"))
        steps["4_total_ms"] = round((time.monotonic() - t4) * 1000, 1)
        bid, _ = await timed(steps, "5a_barrier_self_run", cur_p.insert_barrier(epoch, "spike-failback"))

        async def seen() -> bool:
            return await old.barrier_present(bid)

        ok, waited = await wait_until(seen, 30, 0.02)
        steps["5b_barrier_seen_on_managed"] = {"ok": ok, "ms": round(waited, 1)}
        for sq in SEQUENCES:
            await timed(steps, f"5c_managed_setval_{sq}", old.raise_sequence(epoch, sq, await cur_p.sequence_last(sq)))
        m = await digests("M", "executor_ctl")
        c = await digests("C", "executor_ctl")
        out["receipt_failback"] = {"hash_rule": HASH_RULE, "barrier_id": bid, **compare(c, m),
                                   "sequences": {"self_run": c["sequences"], "managed": m["sequences"]}}
        await timed(steps, "6a_managed_disable_rev_sub", old.set_subscription_enabled(epoch, "bravo_rev_sub", False))
        await timed(steps, "7a_managed_LOGIN", old.set_role_login(epoch, "app_writer", True))
        await timed(steps, "7b_managed_GRANT_INSERT", old.set_insert_privilege(epoch, "app_writer", True))
        wc = await conn("M", "app_writer")
        cur = await wc.execute("INSERT INTO messages (room, author, body, idem_key) VALUES ('failback', 'src', 'back on RDS', %s) "
                               "RETURNING id", (uuid.uuid4(),))
        out["managed_write_after_failback_id"] = (await cur.fetchone())[0]
        await wc.close()
        out["managed_writer_state"] = await old.writer_state("app_writer")
    except Exception as e:  # noqa: BLE001
        out["stopped_on"] = err(e)
    finally:
        await cur_p.close()
        await old.close()
    emit(args.name, out)


async def cmd_pgb(args: argparse.Namespace) -> None:
    """SPK-05 through the laptop PgBouncer (127.0.0.1:46432) in front of the managed DB."""
    pw = os.environ["M_PW_APP_WRITER"]
    base = f"host=127.0.0.1 port={args.port} user=app_writer password={pw} sslmode=disable connect_timeout=10"
    admin = f"host=127.0.0.1 port={args.port} dbname=pgbouncer user=pgb_admin password={os.environ['PGB_ADMIN_PW']} sslmode=disable"
    out: dict[str, Any] = {}
    try:
        c = await AsyncConnection.connect(base + " dbname=chat", autocommit=True)
        cur = await c.execute("SELECT inet_server_addr()::text, current_setting('server_version'), "
                              "(SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()), "
                              "(SELECT version FROM pg_stat_ssl WHERE pid = pg_backend_pid()), "
                              "(SELECT client_addr::text FROM pg_stat_activity WHERE pid = pg_backend_pid())")
        out["login_via_pgbouncer_provider_name"] = dict(zip(("server_addr", "server_version", "server_side_ssl",
                                                             "tls_version", "client_addr_seen_by_rds"),
                                                            await cur.fetchone(), strict=True))
        await c.close()
    except psycopg.Error as e:
        out["login_via_pgbouncer_provider_name"] = err(e)
    try:
        c = await AsyncConnection.connect(base + " dbname=chat_wrongname", autocommit=True)
        await c.execute("SELECT 1")
        out["login_via_pgbouncer_wrong_name"] = "CONNECTED (unexpected)"
        await c.close()
    except psycopg.Error as e:
        out["login_via_pgbouncer_wrong_name"] = err(e)
    a = await AsyncConnection.connect(admin, autocommit=True)
    cur = await a.execute(b"SHOW SERVERS")
    cols = [d.name for d in cur.description]
    out["show_servers"] = [{k: v for k, v in zip(cols, r, strict=True) if k in ("database", "user", "state", "addr",
                                                                                  "port", "tls")}
                           for r in await cur.fetchall()]
    # load: N writers through PgBouncer, short transactions
    stop = asyncio.Event()
    stats: dict[str, Any] = {"ok": 0, "err": 0, "reconnects": 0, "max_wait_ms": 0.0, "errors": {}}

    async def writer() -> None:
        wc: AsyncConnection | None = None
        while not stop.is_set():
            t = time.monotonic()
            try:
                if wc is None or wc.closed:
                    wc = await AsyncConnection.connect(base + " dbname=chat", autocommit=True)
                    stats["reconnects"] += 1
                await wc.execute("INSERT INTO messages (room, author, body, idem_key) VALUES ('pgb', 'w', 'via pgbouncer', %s)",
                                 (uuid.uuid4(),), prepare=False)
                stats["ok"] += 1
                stats["max_wait_ms"] = max(stats["max_wait_ms"], (time.monotonic() - t) * 1000)
            except psycopg.Error as e:
                stats["err"] += 1
                m = err(e)["message"][:120]
                stats["errors"][m] = stats["errors"].get(m, 0) + 1
                if wc is not None:
                    await wc.close()
                wc = None
            await asyncio.sleep(0.02)
        if wc is not None:
            await wc.close()

    a.prepare_threshold = None  # Executor bug workaround: default 5 auto-prepares the 6th PAUSE (spk05-admin-autoprepare.txt)
    pgb_admin = PgBouncerAdmin(a)  # the Executor admin-console client
    ws = [asyncio.ensure_future(writer()) for _ in range(args.writers)]
    await asyncio.sleep(2)
    pause_ms, resume_ms, total_ms = [], [], []
    for _ in range(args.rounds):
        t0 = time.monotonic()
        await pgb_admin.command("PAUSE", "chat")
        t1 = time.monotonic()
        await pgb_admin.command("RESUME", "chat")
        t2 = time.monotonic()
        pause_ms.append((t1 - t0) * 1000)
        resume_ms.append((t2 - t1) * 1000)
        total_ms.append((t2 - t0) * 1000)
        await asyncio.sleep(0.3)
    stop.set()
    await asyncio.gather(*ws, return_exceptions=True)
    await a.close()

    def pct(xs: list[float], q: float) -> float:
        ys = sorted(xs)
        return round(ys[min(len(ys) - 1, int(round(q * (len(ys) - 1))))], 1)

    out["pause_resume"] = {"rounds": args.rounds, "writers": args.writers,
                           "pause_ms": {"p50": pct(pause_ms, 0.5), "p95": pct(pause_ms, 0.95), "max": round(max(pause_ms), 1)},
                           "resume_ms": {"p50": pct(resume_ms, 0.5), "p95": pct(resume_ms, 0.95)},
                           "round_trip_ms": {"p50": pct(total_ms, 0.5), "p95": pct(total_ms, 0.95), "max": round(max(total_ms), 1)},
                           "load": stats}
    emit(args.name, out)


async def cmd_restore_writer(args: argparse.Namespace) -> None:
    """ABORT_PRE_WRITE equivalent for the spike: restore LOGIN and INSERT on src under a new epoch."""
    src = ChatDb(await conn("M", "executor_ctl"), "src")
    epoch = (await read_fence(src.conn) or 0) + 1
    out: dict[str, Any] = {"epoch": epoch, "steps": {}}
    await timed(out["steps"], "src_app_writer_LOGIN", src.set_role_login(epoch, "app_writer", True))
    await timed(out["steps"], "src_GRANT_INSERT", src.set_insert_privilege(epoch, "app_writer", True))
    out["writer_state"] = await src.writer_state("app_writer")
    await src.close()
    emit(args.name, out)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("facts")
    sub.add_parser("privileges")
    s = sub.add_parser("seed")
    s.add_argument("--rows", type=int, default=100_000)
    s.add_argument("--attachments", type=int, default=20)
    f = sub.add_parser("fwd-sub")
    f.add_argument("--timeout", type=float, default=600)
    d = sub.add_parser("digest")
    d.add_argument("--name", default="digest")
    d.add_argument("--session-variants", action="store_true")
    r = sub.add_parser("rev-sub")
    r.add_argument("--sslmode", default="require")
    c = sub.add_parser("cutover")
    c.add_argument("--name", default="cutover")
    c.add_argument("--write-interval", type=float, default=0.05)
    c.add_argument("--warmup", type=float, default=3.0)
    c.add_argument("--reverse-rows", type=int, default=200)
    sub.add_parser("spk09")
    pg = sub.add_parser("pgb")
    pg.add_argument("--name", default="spk05-pgbouncer")
    pg.add_argument("--port", type=int, default=46432)
    pg.add_argument("--rounds", type=int, default=20)
    pg.add_argument("--writers", type=int, default=4)
    fb = sub.add_parser("failback")
    fb.add_argument("--name", default="failback-pg-to-rds")
    rw = sub.add_parser("restore-writer")
    rw.add_argument("--name", default="restore-writer")
    pr = sub.add_parser("probe")
    pr.add_argument("--name", default="probe")
    pr.add_argument("--seconds", type=float, default=600)
    pr.add_argument("--interval", type=float, default=0.5)
    args = p.parse_args()
    fn = {"facts": cmd_facts, "privileges": cmd_privileges, "seed": cmd_seed, "fwd-sub": cmd_fwd_sub,
          "digest": cmd_digest, "rev-sub": cmd_rev_sub, "cutover": cmd_cutover, "spk09": cmd_spk09,
          "probe": cmd_probe, "restore-writer": cmd_restore_writer,
          "failback": cmd_failback, "pgb": cmd_pgb}[args.cmd]
    asyncio.run(fn(args))


if __name__ == "__main__":
    main()
