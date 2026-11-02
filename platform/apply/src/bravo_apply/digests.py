"""Bound values of a change, recomputed from the frozen rehearsal artifact (migration 0004 contract).

``diff_digest``        sha256 of the unified diff text (UTF-8), or None when the artifact has no diff
``policy_diff_digest`` sha256 of the canonical JSON of ``translations``, or None when there are none
``policy_targets``     ``[{type, name}]`` of every translation target, sorted by (type, name)
``artifact_digest``    sha256 of the canonical JSON of the whole artifact (same form as platform/api and
                       platform/rehearsal ``proposal.digest``)
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def artifact_digest(artifact: Any) -> str:
    return sha256_bytes(canonical(artifact))


def diff_text(artifact: dict[str, Any] | None) -> str | None:
    diff = (artifact or {}).get("diff")
    return diff if isinstance(diff, str) and diff else None


def diff_digest(artifact: dict[str, Any] | None) -> str | None:
    diff = diff_text(artifact)
    return sha256_bytes(diff.encode("utf-8")) if diff is not None else None


def translations(artifact: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    items = (artifact or {}).get("translations")
    return items if isinstance(items, list) and items else None


def policy_diff_digest(artifact: dict[str, Any] | None) -> str | None:
    items = translations(artifact)
    return sha256_bytes(canonical(items)) if items is not None else None


def policy_targets(artifact: dict[str, Any] | None) -> list[dict[str, str]]:
    out = []
    for t in translations(artifact) or []:
        target = t.get("target") if isinstance(t, dict) else None
        if isinstance(target, dict):
            out.append({"type": str(target.get("type")), "name": str(target.get("name"))})
    return sorted(out, key=lambda x: (x["type"], x["name"]))


def bound_values(artifact: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "diff_digest": diff_digest(artifact),
        "policy_diff_digest": policy_diff_digest(artifact),
        "policy_targets": policy_targets(artifact),
    }


def mismatches(artifact: dict[str, Any] | None, recorded: dict[str, Any]) -> list[str]:
    """Names of bound values whose recorded value differs from the value recomputed from ``artifact``."""
    got = bound_values(artifact)
    bad = [k for k in ("diff_digest", "policy_diff_digest") if got[k] != recorded.get(k)]
    rec_targets = recorded.get("policy_targets")
    if rec_targets is not None and sorted(rec_targets, key=lambda x: (x["type"], x["name"])) != got["policy_targets"]:
        bad.append("policy_targets")
    if "artifact_digest" in recorded and recorded["artifact_digest"] is not None and artifact is not None:
        if artifact_digest(artifact) != recorded["artifact_digest"]:
            bad.append("artifact_digest")
    return bad
