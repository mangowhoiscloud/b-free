#!/usr/bin/env python3
"""keys.sh llm-login openai: sign the platform's own ChatGPT session in (13 6, D-26, ADR 0009). Demo only.

Runs `codex login` (the Codex CLI's ChatGPT OAuth sign-in) under a pty with CODEX_HOME set to a fresh temp dir
(mode 700), so the operator's ~/.codex and its session are never read, refreshed or logged out. When auth.json
appears, its tokens become one JSON object on stdout for `keys.sh env-put-json platform llm-openai` (a pipe, never
a terminal): access_token, refresh_token, id_token, account_id, auth_mode=chatgpt, expires_at (from the access
token's JWT exp). The temp dir is deleted whatever happens. stderr shows progress with the state and PKCE challenge
redacted; no token is printed. --url-file writes the full sign-in URL (mode 600) for a browser step and is removed
at the end.

Note: `codex login` also asks the OS to open the URL in the default browser.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

URL_RE = re.compile(rb"https://auth\.openai\.com/oauth/authorize\?[^\s\x1b\"']+")
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
TOKENISH_RE = re.compile(r"[A-Za-z0-9_\-.]{40,}")


def log(msg: str) -> None:
    print(f"llm-login: {msg}", file=sys.stderr, flush=True)


def redact_url(url: str) -> str:
    return re.sub(r"(state|code_challenge)=[^&]+", r"\1=<redacted>", url)


def redact_text(raw: bytes) -> str:
    text = ANSI_RE.sub(b"", raw).decode("utf-8", "replace")
    text = URL_RE.sub(b"<sign-in URL>", text.encode()).decode()
    return TOKENISH_RE.sub("<redacted>", text)


def jwt_claims(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        return json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return {}


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(auth: dict) -> tuple[dict, str]:
    """auth.json (Codex AuthDotJson) -> OpenBao record and a value-free summary."""
    tokens = auth.get("tokens") or {}
    missing = [k for k in ("access_token", "refresh_token", "id_token") if not tokens.get(k)]
    if missing:
        raise SystemExit(f"llm-login: auth.json has no {','.join(missing)} (not a ChatGPT sign-in)")
    claims = jwt_claims(tokens["id_token"]).get("https://api.openai.com/auth") or {}
    account = claims.get("chatgpt_account_id") or tokens.get("account_id")
    exp = jwt_claims(tokens["access_token"]).get("exp")
    if not account or not isinstance(exp, (int, float)):
        raise SystemExit("llm-login: no account id or access-token expiry in the sign-in result")
    rec = {"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
           "id_token": tokens["id_token"], "account_id": account, "auth_mode": "chatgpt",
           "expires_at": iso(int(exp)), "last_refresh": iso(int(time.time())), "refresh_lease_until": None}
    summary = (f"account …{account[-4:]}, plan {claims.get('chatgpt_plan_type') or '?'}, "
               f"access token expires {rec['expires_at']}")
    return rec, summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url-file", help="write the full sign-in URL here (mode 600); removed at the end")
    ap.add_argument("--timeout", type=int, default=600, help="seconds to wait for the sign-in (default 600)")
    args = ap.parse_args()
    if sys.stdout.isatty():
        log("stdout must be a pipe into keys.sh env-put-json (use: keys.sh llm-login openai)")
        return 2
    if not shutil.which("codex"):
        log("codex CLI not found")
        return 2
    home = tempfile.mkdtemp(prefix="bravo-codex-login-")
    url_file = Path(args.url_file) if args.url_file else None
    proc = None
    buf = b""
    try:
        os.chmod(home, 0o700)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "CODEX_"))}
        env["CODEX_HOME"] = home
        master, slave = pty.openpty()
        proc = subprocess.Popen(["codex", "login", "-c", 'cli_auth_credentials_store="file"'],
                                stdin=slave, stdout=slave, stderr=slave, env=env, cwd=home, close_fds=True,
                                start_new_session=True)
        os.close(slave)
        log(f"codex login started (CODEX_HOME = fresh temp dir, pid {proc.pid}); waiting up to {args.timeout}s")
        url = None
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                buf += chunk
            if url is None and (m := URL_RE.search(ANSI_RE.sub(b"", buf))):
                url = m.group(0).decode()
                log(f"sign-in URL: {redact_url(url)}")
                if url_file:
                    fd = os.open(url_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(url)
                    log(f"full URL written to {url_file} (600)")
            if proc.poll() is not None:
                break
        else:
            log(f"timed out after {args.timeout}s without a sign-in")
            return 1
        auth_json = Path(home) / "auth.json"
        if proc.returncode != 0 or not auth_json.is_file():
            log(f"codex login exited {proc.returncode} without auth.json; output: {redact_text(buf)[-600:]!r}")
            return 1
        rec, summary = record(json.loads(auth_json.read_text()))
        sys.stdout.write(json.dumps(rec))
        sys.stdout.flush()
        log(f"ChatGPT session captured ({summary}); values not shown")
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        shutil.rmtree(home, ignore_errors=True)
        if url_file:
            url_file.unlink(missing_ok=True)
        log(f"temp CODEX_HOME deleted: {not os.path.exists(home)}")


if __name__ == "__main__":
    sys.exit(main())
