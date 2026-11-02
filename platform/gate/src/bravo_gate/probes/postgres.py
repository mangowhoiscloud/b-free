"""PostgreSQL probes. The probe role is read-only (``collector_ro`` grants); every session is read-only.

DB-ANON-READ  PUBLIC cannot SELECT the app tables                               (IAM-01)
DB-AUTH       the app role cannot log in without a password (real attempt + pg_hba scan)   (IAM-01)
DB-TLS        a non-TLS connection is refused, ssl=on, TLS >= 1.2, verify-full with the CA works   (ENC-01)
DB-AUDIT      pgaudit loaded, pgaudit.log covers ddl and role, log_connections on   (AUD-01)
DR-ARCHIVE    WAL archiving on, archived at least once, last failure older than last success   (DR-01)
"""

from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ..environments import PostgresSection
from ..result import Obs, error_text, failed, passed, unknown, worst

NO_PASSWORD_METHODS = frozenset({"trust", "peer", "ident"})
NETWORK_TYPES = frozenset({"host", "hostssl", "hostnossl", "hostgssenc", "hostnogssenc"})


def judge_hba(rules: list[dict[str, Any]], role: str, dbname: str) -> Obs:
    """DB-AUTH pg_hba rule scan. Judged:
        network rules (host, hostssl, hostnossl, hostgssenc, hostnogssenc) that can match the app role and DB:
            trust, ident, peer -> FAIL (no password over the network); any other method -> ok
        ``trust`` on any rule type, ``local`` included -> FAIL (anyone with a shell in the pod logs in)
    Not judged: ``local`` rules with peer or ident (CNPG's default ``local all all peer map=local``). They
    authenticate the OS user of the Unix socket inside the DB pod, which is not a network path.
    The evidence lists every rule with how it was judged. Parse errors -> UNKNOWN.
    """
    rows: list[dict[str, Any]] = []
    bad: list[int] = []
    errors = [r["line"] for r in rules if r.get("error")]
    for r in rules:
        typ, method = str(r.get("type") or "").lower(), str(r.get("method") or "").lower()
        covers = (_covers(r.get("database"), dbname, {"all", "sameuser", "samerole"})
                  and _covers(r.get("user_name"), role, {"all"}))
        if r.get("error"):
            judged = "unknown: parse error"
        elif not covers:
            judged = "not applicable: does not match the app role and database"
        elif method == "trust":
            judged = "FAIL: trust (no password)" + ("; local socket" if typ == "local" else "")
        elif typ in NETWORK_TYPES and method in NO_PASSWORD_METHODS:
            judged = f"FAIL: {method} on a network rule (no password)"
        elif typ in NETWORK_TYPES:
            judged = f"ok: {method} requires a credential"
        elif typ == "local":
            judged = f"not judged: local {method} (Unix socket inside the DB pod, not a network path)"
        else:
            judged = f"not judged: rule type {typ}"
        if judged.startswith("FAIL"):
            bad.append(int(r["line"]))
        rows.append({"line": r.get("line"), "type": typ, "database": r.get("database"), "user": r.get("user_name"),
                     "method": method, "judged": judged})
    if errors:
        return unknown("hba_parse_error", lines=errors, rules=rows)
    if bad:
        return failed("no_password_hba_rule", lines=bad, rules=rows)
    return passed("hba_requires_password", rules=rows)
TLS_OK = frozenset({"TLSv1.2", "TLSv1.3"})
AUTH_REFUSED_MARKERS = ("password authentication failed", "no password supplied", "password is required",
                        "fe_sendauth", "authentication failed", "pg_hba.conf rejects")
PLAINTEXT_REFUSED_MARKERS = ("no encryption", "pg_hba.conf rejects", "SSL off", "no pg_hba.conf entry")


class PostgresProbe:
    def __init__(self, section: PostgresSection) -> None:
        self.s = section
        self._facts: dict[str, Any] | None = None
        self._facts_error: str | None = None

    # --- connection helpers ---------------------------------------------------------------------------
    def dsn(self) -> str | None:
        return os.environ.get(self.s.dsn_env) or None

    def _connect(self, conninfo: str) -> psycopg.Connection:
        return psycopg.connect(conninfo, connect_timeout=self.s.connect_timeout_s,
                               options="-c default_transaction_read_only=on", autocommit=True)

    def _target(self) -> dict[str, Any]:
        d = conninfo_to_dict(self.dsn() or "")
        return {"host": d.get("host"), "port": d.get("port", "5432"), "dbname": d.get("dbname")}

    def facts(self) -> dict[str, Any] | None:
        if self._facts is not None or self._facts_error is not None:
            return self._facts
        dsn = self.dsn()
        if not dsn:
            self._facts_error = f"dsn_env {self.s.dsn_env} is not set"
            return None
        try:
            with self._connect(dsn) as conn:
                names = ("ssl", "ssl_min_protocol_version", "password_encryption", "log_connections",
                         "shared_preload_libraries", "archive_mode", "pgaudit.log")
                rows = conn.execute("SELECT name, setting FROM pg_settings WHERE name = ANY(%s)", (list(names),))
                settings = {n: v for n, v in rows.fetchall()}
                try:
                    hba = conn.execute(
                        "SELECT line_number, type, database, user_name, auth_method, error "
                        "FROM pg_hba_file_rules ORDER BY line_number").fetchall()
                    hba_rules: list[dict[str, Any]] | None = [
                        {"line": r[0], "type": r[1], "database": r[2], "user_name": r[3], "method": r[4],
                         "error": r[5]} for r in hba]
                except psycopg.Error as exc:
                    hba_rules = None
                    settings["_hba_error"] = error_text(exc)
                public: dict[str, Any] = {}
                for t in sorted(self.s.app_tables):
                    if conn.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0] is None:
                        public[t] = None
                        continue
                    public[t] = conn.execute("SELECT has_table_privilege('public', %s, 'SELECT')", (t,)).fetchone()[0]
                role_row = (conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s",
                                         (self.s.app_role,)).fetchone() if self.s.app_role else None)
                arch = conn.execute("SELECT archived_count, last_archived_time, failed_count, last_failed_time "
                                    "FROM pg_stat_archiver").fetchone()
                ssl_row = conn.execute("SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()").fetchone()
            self._facts = {
                "settings": settings, "hba": hba_rules, "public_select": public,
                "archiver": {"archived_count": arch[0], "last_archived": arch[1].isoformat() if arch[1] else None,
                             "failed_count": arch[2], "last_failed": arch[3].isoformat() if arch[3] else None},
                "probe_session_ssl": {"ssl": ssl_row[0], "version": ssl_row[1]} if ssl_row else None,
                "app_role": None if role_row is None else {"can_login": bool(role_row[0])},
            }
        except psycopg.Error as exc:
            self._facts_error = error_text(exc)
        return self._facts

    def _no_facts(self) -> Obs:
        return unknown("db_unreachable", error=self._facts_error, target=self._target())

    # --- DB-ANON-READ ---------------------------------------------------------------------------------
    def anon_read(self) -> Obs:
        if not self.s.app_tables:
            return unknown("app_tables_not_configured")
        f = self.facts()
        if f is None:
            return self._no_facts()
        pub = f["public_select"]
        ev = {"public_select": pub}
        if any(v is True for v in pub.values()):
            return failed("public_can_select", **ev)
        if any(v is None for v in pub.values()):
            return unknown("app_table_missing", **ev)
        return passed("public_cannot_select", **ev)

    # --- DB-AUTH --------------------------------------------------------------------------------------
    def auth(self) -> Obs:
        role = self.s.app_role
        if not role:
            return unknown("app_role_not_configured")
        if os.environ.get("PGPASSWORD") or os.environ.get("PGPASSFILE"):
            return unknown("probe_environment_has_password", detail="PGPASSWORD/PGPASSFILE set in gate process")
        dsn = self.dsn()
        if not dsn:
            return unknown("db_unreachable", error=f"dsn_env {self.s.dsn_env} is not set")
        facts = self.facts()
        if facts is None:
            return self._no_facts()
        if facts["app_role"] is None:
            # A refused login for a role that does not exist proves nothing (audit B17).
            return unknown("app_role_missing", role=role)
        params = conninfo_to_dict(dsn)
        params.pop("password", None)
        # A path that does not exist: libpq then reads no password file (os.devnull triggers a warning).
        params.update(user=role, passfile=os.path.join(os.path.dirname(os.devnull), ".bravo-gate-no-passfile"))
        attempt: str
        try:
            with psycopg.connect(make_conninfo(**params), connect_timeout=self.s.connect_timeout_s):
                attempt = "connected"
        except psycopg.OperationalError as exc:
            text = str(exc)
            attempt = "refused" if any(m in text for m in AUTH_REFUSED_MARKERS) else "error"
            if attempt == "error":
                return unknown("probe_error", attempt="no-password login", error=error_text(exc))
        parts = [failed("passwordless_login_succeeded", attempt="no-password login", role=role)
                 if attempt == "connected" else passed("passwordless_login_refused", role=role)]
        f = self.facts()
        if f is None:
            parts.append(self._no_facts())
        elif f["hba"] is None:
            parts.append(unknown("hba_unreadable", error=f["settings"].get("_hba_error")))
        else:
            parts.append(judge_hba(f["hba"], role, self._target()["dbname"] or ""))
            if f["settings"].get("password_encryption") != "scram-sha-256":
                parts.append(failed("password_encryption_weak", value=f["settings"].get("password_encryption")))
        return worst(parts)

    # --- DB-TLS ---------------------------------------------------------------------------------------
    def tls(self) -> Obs:
        dsn = self.dsn()
        f = self.facts()
        if f is None or not dsn:
            return self._no_facts()
        st = f["settings"]
        parts: list[Obs] = []
        parts.append(passed("ssl_on") if st.get("ssl") == "on" else failed("ssl_off", ssl=st.get("ssl")))
        v = st.get("ssl_min_protocol_version")
        parts.append(passed("min_tls_ok", min=v) if v in TLS_OK else failed("min_tls_weak", min=v))
        try:
            with self._connect(make_conninfo(dsn, sslmode="disable")):
                parts.append(failed("plaintext_accepted", attempt="sslmode=disable"))
        except psycopg.OperationalError as exc:
            text = str(exc)
            if any(m in text for m in PLAINTEXT_REFUSED_MARKERS):
                parts.append(passed("plaintext_refused", attempt="sslmode=disable"))
            else:
                parts.append(unknown("probe_error", attempt="sslmode=disable", error=error_text(exc)))
        ca_env = self.s.tls.sslrootcert_env
        ca = os.environ.get(ca_env) if ca_env else None
        if not ca:
            parts.append(unknown("ca_not_configured", detail="verify-full needs tls.sslrootcert_env"))
        elif not os.path.isfile(ca):
            parts.append(unknown("ca_file_missing", env=ca_env))
        else:
            try:
                with self._connect(make_conninfo(dsn, sslmode="verify-full", sslrootcert=ca)) as conn:
                    ver = conn.execute("SELECT version FROM pg_stat_ssl WHERE pid = pg_backend_pid()").fetchone()
                parts.append(passed("verify_full_ok", tls=ver[0] if ver else None))
            except psycopg.OperationalError as exc:
                parts.append(failed("verify_full_failed", error=error_text(exc)))
        return worst(parts)

    # --- DB-AUDIT -------------------------------------------------------------------------------------
    def audit(self) -> Obs:
        f = self.facts()
        if f is None:
            return self._no_facts()
        st = f["settings"]
        libs = {x.strip() for x in str(st.get("shared_preload_libraries", "")).split(",") if x.strip()}
        log = {x.strip().lower() for x in str(st.get("pgaudit.log") or "").split(",") if x.strip()}
        parts = [
            passed("pgaudit_loaded") if "pgaudit" in libs else failed("pgaudit_not_loaded", libs=sorted(libs)),
            passed("pgaudit_covers_ddl_role", log=sorted(log)) if ("all" in log or {"ddl", "role"} <= log)
            else failed("pgaudit_log_incomplete", log=sorted(log)),
            passed("log_connections_on") if st.get("log_connections") == "on"
            else failed("log_connections_off", value=st.get("log_connections")),
        ]
        return worst(parts)

    # --- DR-ARCHIVE -----------------------------------------------------------------------------------
    def archive(self) -> Obs:
        f = self.facts()
        if f is None:
            return self._no_facts()
        mode = f["settings"].get("archive_mode")
        a = f["archiver"]
        ev = {"archive_mode": mode, "archived_count": a["archived_count"], "failed_count": a["failed_count"]}
        if mode not in ("on", "always"):
            return failed("archive_mode_off", **ev)
        if not a["archived_count"]:
            return failed("nothing_archived", **ev)
        if a["last_failed"] and a["last_archived"] and a["last_failed"] > a["last_archived"]:
            return failed("last_archive_failed", **ev)
        return passed("wal_archiving_healthy", **ev)


def _covers(values: Any, wanted: str, wildcards: set[str]) -> bool:
    for v in values or []:
        if v in wildcards or v == wanted or str(v).startswith("+"):
            return True
    return False
