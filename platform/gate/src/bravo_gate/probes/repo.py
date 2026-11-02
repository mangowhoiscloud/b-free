"""SEC-GIT-PLAINTEXT (SEC-01, spec §8.5 "Git 평문 비밀 0건", gitleaks).

gitleaks scans the customer app checkout at the deployed commit (``repository.root``). The gate's init container made
it with the same pinned commit and read-only deploy key as the collector; the scan reads every file there, including
the ones the collector withholds from its snapshot. Findings are reported with rule, file and line only (``--redact``;
the secret value never reaches the evidence).

Verdict: findings -> FAIL ``plaintext_secret_in_repo``; no checkout, gitleaks missing or failing -> UNKNOWN;
otherwise PASS ``no_plaintext_secret``. Only the files at that commit are scanned, not the Git history (shallow,
sparse checkout); the evidence says so.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..environments import RepositorySection
from ..result import Obs, error_text, failed, passed, unknown


def _version(binary: str) -> str:
    try:
        return subprocess.run([binary, "version"], capture_output=True, text=True,  # noqa: S603 - fixed argv
                              timeout=10).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {type(exc).__name__}"


def git_plaintext(section: RepositorySection) -> Obs:
    root = Path(section.root)
    ev: dict[str, Any] = {"root": str(root), "repo": os.environ.get(section.repo_env),
                          "commit": os.environ.get(section.commit_env), "scope": "files at the commit (not history)"}
    err = root.parent / ".checkout-error"
    if not root.is_dir():
        reason = err.read_text(encoding="utf-8", errors="replace").strip()[:300] if err.is_file() else None
        return unknown("repository_checkout_missing", checkout_error=reason, **ev)
    files = sum(1 for p in root.rglob("*") if p.is_file())
    if files == 0:
        return unknown("repository_checkout_empty", **ev)
    version = _version(section.gitleaks_bin)
    ev.update(files=files, gitleaks=version)
    with tempfile.TemporaryDirectory(prefix="gate-gitleaks-") as tmp:
        report = Path(tmp) / "report.json"
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, path from the registry
                [section.gitleaks_bin, "dir", str(root), "--no-banner", "--redact", "--report-format", "json",
                 "--report-path", str(report), "--exit-code", "0", "--log-level", "error"],
                capture_output=True, text=True, timeout=section.timeout_s)
        except Exception as exc:  # noqa: BLE001
            return unknown("gitleaks_failed", error=error_text(exc), **ev)
        if proc.returncode != 0 or not report.is_file():
            return unknown("gitleaks_failed", exit_code=proc.returncode, stderr=proc.stderr.strip()[-300:], **ev)
        try:
            findings = json.loads(report.read_text(encoding="utf-8") or "[]")
        except ValueError as exc:
            return unknown("gitleaks_report_unreadable", error=error_text(exc), **ev)
    rows = sorted({(f.get("RuleID"), str(Path(f.get("File", "")).relative_to(root))
                    if str(f.get("File", "")).startswith(str(root)) else f.get("File"), f.get("StartLine"))
                   for f in findings}, key=str)
    ev["findings"] = [{"rule": r, "file": p, "line": ln} for r, p, ln in rows]
    if rows:
        return failed("plaintext_secret_in_repo", **ev)
    return passed("no_plaintext_secret", **ev)
