"""Executor DB role credentials on a cluster's CNPG `chat-db` (CLOUD-E2E-CONTRACT.md 9, D-26).

For the roles the Executor and the link use (`executor_ctl`, `executor_observe`, `replicator`):
1. the password lives only in the platform OpenBao, field <role> of secret/envs/<env>/chat-db;
   a missing field is generated here (secrets.token_urlsafe) and written with a KV v2 PATCH
   (other fields of that secret are left as they are);
2. the role in the database gets a SCRAM-SHA-256 verifier computed here, sent to psql on stdin
   through `kubectl exec` into the CNPG primary (local peer auth). Neither the password nor the
   verifier appears in argv, logs or files.
With ``--schema src|dst`` and an empty database, it applies apps/demo/schema/sql 01_schema.sql,
02_roles.sql and 03_<side>.sql in one psql session on stdin. The role passwords go in as SCRAM
verifiers in psql variables (``\\set app_writer_verifier 'SCRAM-SHA-256$...'``), so no plaintext
password reaches psql, argv or the server log (audit B19). ``app_writer`` must already be in
OpenBao (the app chart uses it); the three Executor/link roles are generated when absent.
Without ``--schema`` (or when the schema exists) only the passwords of existing roles are set.

  python3 deploy/cloud/db_roles.py --env target-onprem --context bravo-onprem --schema dst
Uses the platform-admin token from the OpenBao custody file (same custody as keys.sh).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import ssl
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

ROLES = ("executor_ctl", "executor_observe", "replicator")
REPO = Path(__file__).resolve().parents[2]


def scram_verifier(password: str, iterations: int = 4096) -> str:
    """RFC 5802 / PostgreSQL SCRAM-SHA-256 secret: SCRAM-SHA-256$<iter>:<salt>$<StoredKey>:<ServerKey>."""
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


class Bao:
    def __init__(self) -> None:
        custody_dir = Path(os.environ.get("BRAVO_KEY_CUSTODY_DIR", str(Path.home() / ".bravo/custody")))
        custody = custody_dir / "openbao-init.json"
        if stat.S_IMODE(custody.stat().st_mode) != 0o600:
            raise SystemExit("custody file must be mode 600")
        self._token = json.loads(custody.read_text())["admin_token"]
        doc = json.loads(custody.read_text())
        target = doc.get("bao_target")
        if target in ("platform-eks", "platform-rke2"):  # keys.sh migrate-to-platform
            self._addr = self._port_forward("bravo-platform-rke2" if target == "platform-rke2" else "bravo-platform")
            cafile = custody_dir / target / "ca.crt"
        else:
            self._addr = f"https://127.0.0.1:{os.environ.get('PLATFORM_BAO_HOST_PORT', '8200')}"
            cafile = REPO / "deploy/pilot/bao/tls/ca.crt"
        self._ctx = ssl.create_default_context(cafile=str(cafile))
        # The pilot/platform CA has no Authority Key Identifier; Python >= 3.13 rejects that under
        # VERIFY_X509_STRICT. Chain and host name are still verified.
        self._ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)

    @staticmethod
    def _port_forward(default_ctx: str) -> str:
        """Private port-forward to the platform EKS openbao-0 for this process only (as keys.sh does)."""
        import atexit
        import socket
        import time

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        ctx = os.environ.get("BRAVO_PLATFORM_CONTEXT", default_ctx)
        proc = subprocess.Popen(["kubectl", "--context", ctx, "-n", "openbao", "port-forward", "pod/openbao-0",
                                 f"{port}:8200"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        atexit.register(proc.terminate)
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                return f"https://127.0.0.1:{port}"
            except OSError:
                if proc.poll() is not None:
                    raise SystemExit("port-forward to openbao-0 exited") from None
                time.sleep(0.25)
        raise SystemExit("platform OpenBao not reachable through port-forward")

    def _call(self, method: str, path: str, body: dict | None = None, ctype: str = "application/json") -> dict:
        req = urllib.request.Request(f"{self._addr}/v1/{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode())
        req.add_header("X-Vault-Token", self._token)
        if body is not None:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=15) as r:  # noqa: S310 - fixed https addr
                raw = r.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            raise SystemExit(f"OpenBao {method} {path}: HTTP {exc.code}") from None
        return json.loads(raw) if raw else {}

    def read(self, path: str) -> dict:
        return (self._call("GET", f"secret/data/{path}").get("data") or {}).get("data") or {}

    def patch(self, path: str, fields: dict) -> None:
        if self.read(path):
            self._call("PATCH", f"secret/data/{path}", {"data": fields}, "application/merge-patch+json")
        else:
            self._call("POST", f"secret/data/{path}", {"data": fields})


def kubectl(ctx: str, ns: str, *args: str, stdin: str | None = None) -> str:
    out = subprocess.run(["kubectl", "--context", ctx, "-n", ns, *args], input=stdin, text=True,
                         capture_output=True, timeout=60, check=False)
    if out.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args[:2])}: {out.stderr.strip()[-300:]}")
    return out.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", required=True)
    ap.add_argument("--context", required=True)
    ap.add_argument("--namespace", default="chat")
    ap.add_argument("--cluster", default="chat-db")
    ap.add_argument("--database", default="chat")
    ap.add_argument("--schema", choices=("src", "dst"), help="apply 01/02/03_<side> when the DB is empty")
    ap.add_argument("--store-id", help="attachment_store.active_store (default: the side name)")
    ap.add_argument("--app-writer-from", metavar="ENV",
                    help="copy app_writer from secret/envs/ENV/chat-db when this env has none (the "
                         "destination PgBouncer forwards app_writer to the source DB before cutover)")
    ap.add_argument("--plant-regression", action="store_true",
                    help="dst only: plant the demo regression (03_dst.sql, 심은 시나리오) for the first gate BLOCK")
    a = ap.parse_args()
    bao, path = Bao(), f"envs/{a.env}/chat-db"
    have = bao.read(path)
    new = {r: secrets.token_urlsafe(24) for r in ROLES if not have.get(r)}
    copied = []
    if a.app_writer_from and not have.get("app_writer"):
        other = bao.read(f"envs/{a.app_writer_from}/chat-db").get("app_writer")
        if not other:
            raise SystemExit(f"secret/envs/{a.app_writer_from}/chat-db has no app_writer")
        new["app_writer"] = other
        copied.append(f"app_writer<-envs/{a.app_writer_from}/chat-db")
    if new:
        bao.patch(path, new)
    fields = bao.read(path)
    missing = [r for r in ROLES if not fields.get(r)]
    if missing:
        raise SystemExit(f"read-back missing fields {missing}")
    pod = kubectl(a.context, a.namespace, "get", "pod", "-l",
                  f"cnpg.io/cluster={a.cluster},cnpg.io/instanceRole=primary",
                  "-o", "jsonpath={.items[0].metadata.name}").strip()
    if not pod:
        raise SystemExit("no CNPG primary pod found")
    psql = ("exec", "-i", pod, "-c", "postgres", "--", "psql", "-d", a.database, "-v", "ON_ERROR_STOP=1", "-q")
    schema_applied = False
    if a.schema:
        exists = kubectl(a.context, a.namespace, "exec", pod, "-c", "postgres", "--", "psql", "-d", a.database,
                         "-tAc", "SELECT to_regclass('public.messages') IS NOT NULL").strip()
        if exists != "t":
            if not fields.get("app_writer"):
                raise SystemExit(f"secret/{path} has no app_writer field (the app chart's password)")
            sql_dir = REPO / "apps/demo/schema/sql"
            head = "".join(f"\\set {r}_verifier '{scram_verifier(fields[r])}'\n" for r in ("app_writer", *ROLES))
            head += f"\\set attachment_store_id '{a.store_id or a.schema}'\n"
            head += f"\\set plant_regression {'true' if a.plant_regression else 'false'}\n"
            body = "".join((sql_dir / f).read_text() for f in ("01_schema.sql", "02_roles.sql", f"03_{a.schema}.sql"))
            kubectl(a.context, a.namespace, *psql, "-f", "-", stdin=head + body)
            schema_applied = True
    present = kubectl(a.context, a.namespace, "exec", pod, "-c", "postgres", "--", "psql", "-d", a.database,
                      "-tAc", "SELECT rolname FROM pg_roles WHERE rolname IN "
                      "('executor_ctl','executor_observe','replicator')").split()
    sql = "".join(f"ALTER ROLE {r} PASSWORD '{scram_verifier(fields[r])}';\n" for r in ROLES if r in present)
    if "executor_ctl" in present:  # for schemas applied before this grant was in 02_roles.sql
        sql += "GRANT SET ON PARAMETER log_min_error_statement, log_statement TO executor_ctl;\n"
    if sql and not schema_applied:
        kubectl(a.context, a.namespace, *psql, "-f", "-", stdin=sql)
    print(json.dumps({"env": a.env, "secret": f"secret/{path}",
                      "generated_fields": sorted(k for k in new if k in ROLES), "copied": copied,
                      "schema_applied": f"01,02,03_{a.schema}" if schema_applied else False,
                      "roles_set": [r for r in ROLES if r in present],
                      "roles_missing_in_db": [r for r in ROLES if r not in present], "pod": pod}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
