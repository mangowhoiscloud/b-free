"""Pinned tools (TECH-STACK-SPEC 2.8): version and sha256 of the binary that runs, checked before every phase."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime import clean_env, run


@dataclass(frozen=True, slots=True)
class Pin:
    name: str
    version: str
    path: str
    sha256: str
    version_cmd: tuple[str, ...]
    version_key: str | None = None
    version_regex: str | None = None


def load_lock(path: Path) -> dict[str, Pin]:
    data = json.loads(path.read_text())
    return {
        name: Pin(name, t["version"], t["path"], t["sha256"], tuple(t["version_cmd"]), t.get("version_key"),
                  t.get("version_regex"))
        for name, t in data["tools"].items()
    }


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dig(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def reported_version(pin: Pin, output: str) -> str | None:
    if pin.version_key:
        try:
            return _dig(json.loads(output), pin.version_key)
        except ValueError:
            return None
    if pin.version_regex:
        m = re.search(pin.version_regex, output)
        return m.group(1) if m else None
    return None


def check(pin: Pin) -> dict[str, Any]:
    """One tool: {name, version, sha256, ok, reason}. Never raises."""
    p = Path(pin.path)
    out: dict[str, Any] = {"name": pin.name, "path": pin.path, "pinned_version": pin.version, "pinned_sha256": pin.sha256}
    if not p.is_file():
        return {**out, "ok": False, "reason": "binary_missing"}
    actual = file_sha256(p)
    out["sha256"] = actual
    if actual != pin.sha256:
        return {**out, "ok": False, "reason": "sha256_mismatch"}
    try:
        proc = run([pin.path, *pin.version_cmd], env=clean_env(), timeout=30, check=False)
    except OSError as exc:
        return {**out, "ok": False, "reason": f"exec_failed:{type(exc).__name__}"}
    version = reported_version(pin, (proc.stdout + proc.stderr).decode(errors="replace"))
    out["version"] = version
    if version != pin.version:
        return {**out, "ok": False, "reason": "version_mismatch"}
    return {**out, "ok": True, "reason": None}


def verify(lock: dict[str, Pin], names: list[str] | None = None) -> list[dict[str, Any]]:
    return [check(lock[n]) for n in (names or sorted(lock))]
