"""Server-side tools that need platform credentials (13 4, 13 6 "비밀", D-26).

``controls_describe``, ``evidence_read``, ``db_inspect`` and ``db_rehearse`` run in the rehearsal
service, never in the sandbox. Credentials come from the platform OpenBao at call time and stay
in this process: no tool result carries a secret value, and every string in a result passes
``redact``. Each tool is read-only toward the customer:

- ``controls_describe``: ``GET <BRAVO_GATE_URL>/v1/controls?source=&target=`` on the gate service
  (:8084). Source and target come from the Trial (state machine), not from tool input.
- ``evidence_read``: one stored evidence document by ID from the evidence store
  (``BRAVO_EVIDENCE_DIR``, default ``<trials dir>/evidence``). Only IDs listed in the Trial's
  envelope are readable. The platform DB as a second store is not wired yet.
- ``db_inspect``: one of the role's fixed catalog queries (``db_queries`` in the allowlist) through
  the read-only ``collector_ro`` role, in a ``READ ONLY`` transaction with a statement timeout.
- ``db_rehearse``: the proposed SQL in one transaction on the synthetic database that is always
  rolled back. Chosen over "a new synthetic DB per Trial" because it needs no CREATE DATABASE
  right and no cleanup (13 9 질문 6). Transaction control, session changes and server-side
  file/process functions are refused before anything is sent.

Fail closed: a missing setting or secret yields a ``blocked`` reason (``gate_unconfigured``,
``openbao_unconfigured``, ``secret_missing``, ...) and nothing is called.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .proposal import SECRET_LIKE

MAX_ROWS = 200
MAX_EVIDENCE_BYTES = 48_000
MAX_STATEMENTS = 50
STATEMENT_TIMEOUT_MS = 5_000
LOCK_TIMEOUT_MS = 2_000
ESCAPE_STRING = re.compile(r"(?<![A-Za-z0-9_$])[Ee]'")
EVIDENCE_ID = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
ENV_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Fixed catalog SQL for db_inspect. The allowlist's db_queries chooses which of these a role may run.
DB_QUERIES: dict[str, str] = {
    "server_version": "SELECT current_setting('server_version') AS server_version, "
                      "current_setting('server_version_num') AS server_version_num",
    "settings_replication": "SELECT name, setting FROM pg_settings WHERE name IN ('wal_level', "
                            "'max_replication_slots', 'max_wal_senders', 'max_logical_replication_workers', "
                            "'rds.logical_replication', 'cloudsql.logical_decoding') ORDER BY name",
    "extensions": "SELECT extname, extversion FROM pg_extension ORDER BY extname",
    "collations": "SELECT datname, datcollate, datctype, datlocprovider::text AS provider FROM pg_database "
                  "WHERE datname = current_database()",
    "tables_without_pk": "SELECT n.nspname AS schema, c.relname AS table_name FROM pg_class c "
                         "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r' "
                         "AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND NOT EXISTS "
                         "(SELECT 1 FROM pg_constraint k WHERE k.conrelid = c.oid AND k.contype = 'p') ORDER BY 1, 2",
    "replica_identity": "SELECT n.nspname AS schema, c.relname AS table_name, c.relreplident::text AS replica_identity "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r' "
                        "AND n.nspname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2",
    "sequences": "SELECT schemaname, sequencename, last_value, increment_by FROM pg_sequences ORDER BY 1, 2",
    "publications": "SELECT pubname, puballtables, pubinsert, pubupdate, pubdelete FROM pg_publication ORDER BY 1",
    "subscriptions": "SELECT subname, subenabled FROM pg_subscription ORDER BY 1",
    "replication_slots": "SELECT slot_name, plugin, slot_type, active, wal_status FROM pg_replication_slots ORDER BY 1",
    "replication_lag": "SELECT application_name, state, write_lag::text, flush_lag::text, replay_lag::text "
                       "FROM pg_stat_replication ORDER BY 1",
}

# db_rehearse: statements whose first words would leave or change the transaction or the session,
# or reach the server's files and processes. Matched case-insensitively on the first words.
REHEARSE_DENY_FIRST = ("BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE", "PREPARE",
                       "VACUUM", "CHECKPOINT", "COPY", "LISTEN", "NOTIFY", "UNLISTEN", "LOAD", "DISCARD", "RESET",
                       "SET", "DO", "CALL", "CLUSTER", "REINDEX", "SECURITY", "IMPORT")
REHEARSE_DENY_PAIRS = (("CREATE", "DATABASE"), ("DROP", "DATABASE"), ("ALTER", "DATABASE"), ("ALTER", "SYSTEM"),
                       ("CREATE", "ROLE"), ("ALTER", "ROLE"), ("DROP", "ROLE"), ("CREATE", "USER"),
                       ("ALTER", "USER"), ("DROP", "USER"), ("CREATE", "SUBSCRIPTION"), ("CREATE", "EXTENSION"),
                       ("CREATE", "TABLESPACE"))
REHEARSE_DENY_CALLS = re.compile(r"\b(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_read_file|"
                                 r"pg_read_binary_file|pg_ls_dir|pg_stat_file|lo_import|lo_export|dblink\w*|"
                                 r"pg_sleep\w*|set_config|pg_create_\w+_slot|pg_drop_replication_slot)\s*\(", re.I)


class ToolBlocked(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def redact(value: Any) -> Any:
    """Every string in a tool result: secret-like substrings are replaced (D-26)."""
    if isinstance(value, str):
        return SECRET_LIKE.sub("[redacted]", value)
    if isinstance(value, dict):
        return {str(k): redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


# SQL splitting for db_rehearse ------------------------------------------------------------------


def split_sql(sql: str) -> list[str]:
    """Split on top-level ``;`` while respecting quotes, comments and dollar quoting. Comments are dropped."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            buf.append(" ")
            continue
        if c == "/" and sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            if depth:
                raise ToolBlocked("sql_unterminated_comment")
            buf.append(" ")
            i = j
            continue
        if c in ("'", '"'):
            j = i + 1
            while True:
                j = sql.find(c, j)
                if j < 0:
                    raise ToolBlocked("sql_unterminated_quote")
                if sql.startswith(c * 2, j):
                    j += 2
                    continue
                break
            buf.append(sql[i:j + 1])
            i = j + 1
            continue
        if c == "$":
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                if j < 0:
                    raise ToolBlocked("sql_unterminated_dollar_quote")
                buf.append(sql[i:j + len(tag)])
                i = j + len(tag)
                continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    stmt = "".join(buf).strip()
    if stmt:
        out.append(stmt)
    return out


def rehearse_problem(stmt: str) -> str | None:
    words = re.findall(r"[A-Za-z_]+", stmt[:200].upper())
    first = words[0] if words else ""
    if first in REHEARSE_DENY_FIRST:
        return f"statement_not_allowed:{first.lower()}"
    if len(words) >= 2 and (words[0], words[1]) in REHEARSE_DENY_PAIRS:
        return f"statement_not_allowed:{words[0].lower()}_{words[1].lower()}"
    if len(words) >= 3 and words[0] == "CREATE" and words[1] in ("OR", "TEMP", "TEMPORARY", "UNLOGGED") \
            and words[2] in ("ROLE", "USER", "DATABASE"):
        return "statement_not_allowed:create_role"
    if REHEARSE_DENY_CALLS.search(stmt):
        return "function_not_allowed"
    return None


# OpenBao (D-26) --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenBaoConfig:
    addr: str | None
    kv_mount: str
    token_file: Path | None
    k8s_role: str | None
    k8s_mount: str
    k8s_jwt_file: Path
    ca_cert: str | None

    @classmethod
    def from_env(cls) -> OpenBaoConfig:
        e = os.environ.get
        token_file = (e("BRAVO_OPENBAO_TOKEN_FILE") or "").strip()
        return cls(addr=(e("BRAVO_OPENBAO_ADDR") or "").rstrip("/") or None,
                   kv_mount=(e("BRAVO_OPENBAO_KV_MOUNT") or "secret").strip("/"),
                   token_file=Path(token_file) if token_file else None,
                   k8s_role=e("BRAVO_OPENBAO_K8S_ROLE") or None,
                   k8s_mount=(e("BRAVO_OPENBAO_K8S_MOUNT") or "kubernetes").strip("/"),
                   k8s_jwt_file=Path(e("BRAVO_OPENBAO_K8S_JWT_FILE")
                                     or "/var/run/secrets/kubernetes.io/serviceaccount/token"),
                   ca_cert=e("BRAVO_OPENBAO_CACERT") or None)


class OpenBao:
    """KV v2 reads with the service's own identity (token file, or Kubernetes auth login)."""

    def __init__(self, cfg: OpenBaoConfig) -> None:
        self.cfg = cfg
        self._token: str | None = None
        self._token_exp = 0.0

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=str(self.cfg.addr), timeout=10.0, verify=self.cfg.ca_cert or True)

    async def _login(self, http: httpx.AsyncClient) -> str:
        if self.cfg.token_file is not None:
            try:
                return self.cfg.token_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ToolBlocked("openbao_token_unreadable") from exc
        if self.cfg.k8s_role is None:
            raise ToolBlocked("openbao_unconfigured")
        if self._token and self._token_exp - time.time() > 60:
            return self._token
        try:
            jwt = self.cfg.k8s_jwt_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ToolBlocked("openbao_token_unreadable") from exc
        r = await http.post(f"/v1/auth/{self.cfg.k8s_mount}/login", json={"role": self.cfg.k8s_role, "jwt": jwt})
        if r.status_code != 200:
            raise ToolBlocked("openbao_denied")
        auth = r.json().get("auth") or {}
        self._token, self._token_exp = auth.get("client_token"), time.time() + float(auth.get("lease_duration", 300))
        if not self._token:
            raise ToolBlocked("openbao_denied")
        return self._token

    async def read(self, path: str) -> dict[str, str]:
        if not self.cfg.addr:
            raise ToolBlocked("openbao_unconfigured")
        try:
            async with self._client() as http:
                token = await self._login(http)
                r = await http.get(f"/v1/{self.cfg.kv_mount}/data/{path.strip('/')}",
                                   headers={"X-Vault-Token": token})
        except httpx.HTTPError as exc:
            raise ToolBlocked("openbao_unreachable") from exc
        if r.status_code == 404:
            raise ToolBlocked("secret_missing")
        if r.status_code in (401, 403):
            raise ToolBlocked("openbao_denied")
        if r.status_code != 200:
            raise ToolBlocked(f"openbao_status_{r.status_code}")
        data = ((r.json().get("data") or {}).get("data")) or {}
        return {str(k): str(v) for k, v in data.items()}


# Tools -------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServerToolSettings:
    gate_url: str | None
    gate_secret_path: str | None
    evidence_dir: Path
    db_ro_path: str | None  # e.g. "bravo/db/{env}/collector_ro"
    db_synthetic_path: str | None  # e.g. "bravo/db/synthetic/rehearse"
    openbao: OpenBaoConfig

    @classmethod
    def from_env(cls, trials_dir: Path) -> ServerToolSettings:
        e = os.environ.get
        evidence = (e("BRAVO_EVIDENCE_DIR") or "").strip()
        return cls(gate_url=(e("BRAVO_GATE_URL") or "").rstrip("/") or None,
                   gate_secret_path=e("BRAVO_OPENBAO_GATE_PATH") or None,
                   evidence_dir=Path(evidence) if evidence else trials_dir / "evidence",
                   db_ro_path=e("BRAVO_OPENBAO_DB_RO_PATH") or None,
                   db_synthetic_path=e("BRAVO_OPENBAO_DB_SYNTHETIC_PATH") or None,
                   openbao=OpenBaoConfig.from_env())


def _conninfo(secret: dict[str, str]) -> dict[str, Any]:
    missing = [k for k in ("host", "dbname", "user", "password") if not secret.get(k)]
    if missing:
        raise ToolBlocked("secret_incomplete")
    return {"host": secret["host"], "port": int(secret.get("port") or 5432), "dbname": secret["dbname"],
            "user": secret["user"], "password": secret["password"], "sslmode": secret.get("sslmode") or "prefer",
            "connect_timeout": 5, "application_name": "bravo-tool-layer"}


def _jsonable(v: Any) -> Any:
    return v if v is None or isinstance(v, (bool, int, float, str)) else str(v)


class ServerTools:
    def __init__(self, settings: ServerToolSettings) -> None:
        self.s = settings
        self.bao = OpenBao(settings.openbao)

    @staticmethod
    def _env_id(inputs: dict[str, Any], key: str) -> str:
        value = inputs.get(key)
        if not isinstance(value, str) or not ENV_ID.match(value):
            raise ToolBlocked("environments_unconfigured")
        return value

    async def controls_describe(self, inputs: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not self.s.gate_url:
            raise ToolBlocked("gate_unconfigured")
        level = args.get("level", "question")
        if level not in ("question", "control"):
            raise ToolBlocked("bad_level")
        params = {"source": self._env_id(inputs, "source_env"), "target": self._env_id(inputs, "target_env")}
        if level == "control":
            params["level"] = "control"
        headers = {}
        if self.s.gate_secret_path:
            token = (await self.bao.read(self.s.gate_secret_path)).get("token")
            if not token:
                raise ToolBlocked("secret_incomplete")
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as http:
                r = await http.get(f"{self.s.gate_url}/v1/controls", params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise ToolBlocked("gate_unreachable") from exc
        if r.status_code != 200:
            raise ToolBlocked(f"gate_status_{r.status_code}")
        rows = r.json()
        if not isinstance(rows, list):
            raise ToolBlocked("gate_bad_response")
        return {"source": params["source"], "target": params["target"], "level": level, "rows": redact(rows)}

    async def evidence_read(self, evidence_ids: set[str], args: dict[str, Any]) -> dict[str, Any]:
        ev = args.get("id")
        if not isinstance(ev, str) or not EVIDENCE_ID.match(ev):
            raise ToolBlocked("bad_evidence_id")
        if ev not in evidence_ids:
            raise ToolBlocked("evidence_not_in_scope")
        path = self.s.evidence_dir / f"{ev}.json"
        if path.is_symlink() or not path.is_file():
            raise ToolBlocked("evidence_missing")
        raw = path.read_bytes()
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise ToolBlocked("evidence_too_large")
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise ToolBlocked("evidence_unreadable") from exc
        return {"id": ev, "content": redact(doc)}

    async def db_inspect(self, inputs: dict[str, Any], allowed: frozenset[str], args: dict[str, Any]) -> dict[str, Any]:
        query, target = args.get("query"), args.get("target", "source")
        if not isinstance(query, str) or query not in allowed or query not in DB_QUERIES:
            raise ToolBlocked("query_not_allowed")
        if target not in ("source", "destination"):
            raise ToolBlocked("bad_target")
        if not self.s.db_ro_path:
            raise ToolBlocked("db_unconfigured")
        env_id = self._env_id(inputs, "source_env" if target == "source" else "target_env")
        conninfo = _conninfo(await self.bao.read(self.s.db_ro_path.format(env=env_id)))
        sql = DB_QUERIES[query]

        def work() -> dict[str, Any]:
            import psycopg

            try:
                with psycopg.connect(**conninfo) as conn:
                    conn.read_only = True
                    with conn.cursor() as cur:
                        cur.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                        cur.execute(sql)
                        cols = [d.name for d in cur.description or []]
                        rows = cur.fetchmany(MAX_ROWS + 1)
                    conn.rollback()
            except psycopg.Error as exc:
                return {"error": getattr(exc, "sqlstate", None) or type(exc).__name__}
            return {"columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows[:MAX_ROWS]],
                    "truncated": len(rows) > MAX_ROWS}

        out = await asyncio.to_thread(work)
        return {"query": query, "target": target, **redact(out)}

    async def db_rehearse(self, args: dict[str, Any]) -> dict[str, Any]:
        sql = args.get("sql")
        if not isinstance(sql, str) or not sql.strip() or len(sql) > 20_000:
            raise ToolBlocked("bad_sql")
        if ESCAPE_STRING.search(sql):
            # E'...' strings take backslash escapes; the splitter follows standard quoting only, so they
            # could hide a statement boundary. Refused rather than parsed differently from the server.
            raise ToolBlocked("escape_string_not_allowed")
        statements = split_sql(sql)
        if not statements or len(statements) > MAX_STATEMENTS:
            raise ToolBlocked("bad_sql")
        for stmt in statements:
            problem = rehearse_problem(stmt)
            if problem:
                raise ToolBlocked(problem)
        if not self.s.db_synthetic_path:
            raise ToolBlocked("db_unconfigured")
        conninfo = _conninfo(await self.bao.read(self.s.db_synthetic_path))

        def work() -> list[dict[str, Any]]:
            import psycopg

            results: list[dict[str, Any]] = []
            with psycopg.connect(**conninfo) as conn:  # autocommit off: psycopg opens one transaction
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                        cur.execute(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}")
                        for i, stmt in enumerate(statements, start=1):
                            try:
                                cur.execute(stmt)  # one statement per call (extended protocol)
                            except psycopg.Error as exc:
                                results.append({"index": i, "status": "error",
                                                "sqlstate": getattr(exc, "sqlstate", None)})
                                break
                            results.append({"index": i, "status": "ok", "rowcount": cur.rowcount,
                                            "command": (cur.statusmessage or "").split(" ")[0]})
                finally:
                    conn.rollback()  # always: nothing persists, even on success
            return results

        try:
            results = await asyncio.to_thread(work)
        except Exception as exc:  # connection-level failure: no statement ran
            import psycopg

            if isinstance(exc, psycopg.Error):
                raise ToolBlocked("db_unreachable") from exc
            raise
        return {"statements": results, "rolled_back": True}
