"""Stage recording, stop reasons and subprocess helpers shared by every step.

A step either returns ``Step`` (result OK | SKIPPED with detail) or raises ``Stop``. ``Stop.status`` is the
terminal change status the step forces (``prepare_failed``, ``refused``, ``failed``) and ``Stop.reason`` a
stable reason code. Stage details never carry secrets: only digests, versions, counts, names and reason codes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")
_SECRETISH = re.compile(r"(?i)(token|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?[^\s\"',}]+")


class Result(StrEnum):
    OK = "OK"
    FAIL = "FAIL"
    REFUSED = "REFUSED"
    NOT_CONNECTED = "NOT_CONNECTED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class Step:
    result: Result = Result.OK
    detail: dict[str, Any] = field(default_factory=dict)
    value: Any = None


class Stop(Exception):  # noqa: N818 - a control-flow signal, not an error type
    """End the phase. ``status``: prepare_failed | refused | failed."""

    def __init__(self, status: str, reason: str, result: Result = Result.FAIL, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.result = result
        self.detail = detail or {}


class Recorder(Protocol):
    def record(self, phase: str, name: str, started_at: datetime, ms: int, result: Result, detail: dict[str, Any]) -> None: ...


@dataclass
class MemoryRecorder:
    """CLI mode: keeps the rows and appends each one to ``stages.jsonl`` in the evidence directory."""

    out_dir: Path | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)

    def record(self, phase: str, name: str, started_at: datetime, ms: int, result: Result, detail: dict[str, Any]) -> None:
        row = {"seq": len(self.rows) + 1, "phase": phase, "name": name, "started_at": started_at.isoformat(),
               "ms": ms, "result": result.value, "detail": detail}
        self.rows.append(row)
        if self.out_dir is not None:
            with (self.out_dir / "stages.jsonl").open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


def scrub(text: str, limit: int = 600) -> str:
    return _SECRETISH.sub(lambda m: m.group(1) + "=<masked>", text)[-limit:]


def run_stage(rec: Recorder, phase: str, name: str, fn: Callable[[], Step]) -> Step:
    """Run one step, record exactly one stage row, re-raise ``Stop`` (unexpected errors become ``failed``)."""
    assert NAME_RE.match(name), name
    started, t0 = datetime.now(UTC), time.monotonic()
    try:
        step = fn()
    except Stop as stop:
        rec.record(phase, name, started, _ms(t0), stop.result, {"reason": stop.reason, **stop.detail})
        raise
    except Exception as exc:  # noqa: BLE001 - every crash is recorded, never swallowed
        detail = {"reason": f"{name}_error", "error": scrub(f"{type(exc).__name__}: {exc}")}
        rec.record(phase, name, started, _ms(t0), Result.FAIL, detail)
        raise Stop("prepare_failed" if phase == "prepare" else "failed", f"{name}_error", Result.FAIL, detail) from exc
    rec.record(phase, name, started, _ms(t0), step.result, step.detail)
    return step


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


# Subprocesses ---------------------------------------------------------------------------------------------

# Variables never passed to tools. trivy in particular gets none of them (CVE-2026-33634).
_DENY_PREFIXES = ("VAULT_", "BAO_", "AWS_", "GITHUB_", "GH_", "ACTIONS_", "CLOUDFLARE_", "GOOGLE_", "AZURE_",
                  "ANTHROPIC_", "OPENAI_", "BRAVO_", "PG", "DOCKER_AUTH", "REGISTRY_AUTH", "TRIVY_", "COSIGN_")


def clean_env(extra: Mapping[str, str] | None = None, keep: Sequence[str] = ()) -> dict[str, str]:
    """Minimal environment: PATH, HOME, locale, plus ``keep`` and ``extra``. No credentials."""
    base = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER") if k in os.environ}
    for k in keep:
        if k in os.environ and not k.startswith(_DENY_PREFIXES):
            base[k] = os.environ[k]
    base.update(extra or {})
    return base


def run(argv: Sequence[str], *, env: Mapping[str, str] | None = None, timeout: float = 600, stdin: Any = None,
        input_bytes: bytes | None = None, cwd: str | Path | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(list(argv), env=dict(env) if env is not None else clean_env(), timeout=timeout, stdin=stdin,
                          input=input_bytes, cwd=cwd, capture_output=True, check=False)
    if check and proc.returncode != 0:
        raise CommandError(argv, proc.returncode, proc.stderr.decode(errors="replace"))
    return proc


class CommandError(RuntimeError):
    def __init__(self, argv: Sequence[str], code: int, stderr: str) -> None:
        self.argv0 = Path(str(argv[0])).name
        self.code = code
        self.stderr = scrub(stderr)
        super().__init__(f"{self.argv0} exited {code}: {self.stderr}")
