"""Configuration: ``config.json`` (references only) and ``tools.lock.json`` next to this package's project root.

Environment (all optional):
  BRAVO_APPLY_CONFIG        path to config.json (default: platform/apply/config.json)
  BRAVO_APPLY_TOOLS_LOCK    path to tools.lock.json (default: platform/apply/tools.lock.json)
  BRAVO_APPLY_REPO_ROOT     git repository holding the base app source (default: the repo this file is in)
  BRAVO_APPLY_CACHE_DIR     content-addressed OCI archives (default: ~/.cache/bravo-apply)
  BRAVO_APPLY_GATE_URL      platform/gate service for the post-apply re-check (unset = not connected)
  BRAVO_APPLY_DATABASE_URL  platform DB as role bravo_apply (worker mode only)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Config:
    raw: dict[str, Any]
    base_dir: Path
    lock_path: Path
    repo_root: Path
    cache_dir: Path
    gate_url: str | None

    @property
    def registry(self) -> dict[str, Any]:
        return self.raw["staging_registry"]

    @property
    def signer(self) -> dict[str, Any]:
        return self.raw["signer"]

    @property
    def scan_policy(self) -> dict[str, Any]:
        return self.raw["scan_policy"]

    def path(self, rel: str) -> Path:
        return (self.base_dir / rel).resolve()

    def app(self, name: str) -> dict[str, Any]:
        return self.raw["apps"][name]

    def destination(self, env_name: str) -> dict[str, Any] | None:
        return self.raw["destinations"].get(env_name)


def load(path: str | None = None) -> Config:
    cfg_path = Path(path or os.environ.get("BRAVO_APPLY_CONFIG") or PROJECT_DIR / "config.json").resolve()
    raw = json.loads(cfg_path.read_text())
    lock = Path(os.environ.get("BRAVO_APPLY_TOOLS_LOCK") or PROJECT_DIR / "tools.lock.json").resolve()
    repo = Path(os.environ.get("BRAVO_APPLY_REPO_ROOT") or PROJECT_DIR.parents[1]).resolve()
    cache = Path(os.environ.get("BRAVO_APPLY_CACHE_DIR") or Path.home() / ".cache" / "bravo-apply").resolve()
    gate = (os.environ.get("BRAVO_APPLY_GATE_URL") or "").strip().rstrip("/") or None
    return Config(raw=raw, base_dir=cfg_path.parent, lock_path=lock, repo_root=repo, cache_dir=cache, gate_url=gate)
