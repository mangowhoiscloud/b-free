"""Translated policies (governance artifact ``translations[]``), applied only inside the approved scope.

Scope rules, all checked before anything is applied:
1. the translations recompute to the approved ``policy_diff_digest`` (else ``policy_diff_mismatch``);
2. every target type is one this destination can apply (``policy_types``); others are NOT_CONNECTED and the
   change fails with ``policy_target_not_connected:<type>`` (nothing applied);
3. Kubernetes documents: namespaced kinds only, the destination namespace only, names equal to the target name
   or prefixed by it, no reserved names, no wildcard verbs or resources.
Then a server-side dry run of every document, then the real server-side apply (field manager ``bravo-apply``).
"""

from __future__ import annotations

import json
from typing import Any

from .digests import canonical, sha256_bytes
from .runtime import CommandError, Result, Step, Stop

KINDS = {
    "k8s_rbac": {"Role", "RoleBinding", "ServiceAccount"},
    "external_secret": {"ExternalSecret"},
}
RESERVED_PREFIXES = ("bravo-", "rehearsal-", "verify-", "system:")


def objects(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, dict) and isinstance(document.get("items"), list):
        return [o for o in document["items"]]
    return [document]


def check_scope(items: list[dict[str, Any]] | None, approved_digest: str | None, dest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the Kubernetes objects to apply, or raise ``Stop``. ``items`` are the artifact translations."""
    if not items and not approved_digest:
        return []
    got = sha256_bytes(canonical(items)) if items else None
    if got != approved_digest:
        raise Stop("refused", "policy_diff_mismatch", Result.REFUSED, {"approved": approved_digest, "recomputed": got})
    allowed_types = set(dest.get("policy_types") or [])
    off = sorted({t["target"]["type"] for t in items if t["target"]["type"] not in allowed_types})  # type: ignore[index]
    if off:
        raise Stop("failed", f"policy_target_not_connected:{off[0]}", Result.NOT_CONNECTED,
                   {"types": off, "destination_types": sorted(allowed_types)})
    ns, out, problems = dest["namespace"], [], []
    for t in items:  # type: ignore[union-attr]
        ttype, tname = t["target"]["type"], t["target"]["name"]
        for obj in objects(t.get("document")):
            if not isinstance(obj, dict):
                problems.append(f"{tname}:not_an_object")
                continue
            kind, meta = obj.get("kind"), obj.get("metadata") or {}
            name = str(meta.get("name", ""))
            if kind not in KINDS[ttype]:
                problems.append(f"{tname}:kind_not_allowed:{kind}")
            if meta.get("namespace") not in (None, ns):
                problems.append(f"{tname}:namespace_out_of_scope:{meta.get('namespace')}")
            if not (name == tname or name.startswith(tname + "-")) or name.startswith(RESERVED_PREFIXES):
                problems.append(f"{tname}:name_out_of_scope:{name}")
            for rule in obj.get("rules") or []:
                if "*" in (rule.get("verbs") or []) or "*" in (rule.get("resources") or []):
                    problems.append(f"{tname}:wildcard_rule")
            out.append({**obj, "metadata": {**meta, "namespace": ns}})
    if problems:
        raise Stop("refused", "policy_out_of_scope", Result.REFUSED, {"problems": problems[:30]})
    return out


def apply(k, objs: list[dict[str, Any]], namespace: str) -> Step:
    """``k`` is the destination's kubectl runner (K3sContainerd.k)."""
    if not objs:
        return Step(Result.SKIPPED, {"reason": "no_policy_diff"})
    doc = json.dumps({"apiVersion": "v1", "kind": "List", "items": objs}).encode()
    base = ["-n", namespace, "apply", "--server-side", "--field-manager=bravo-apply", "-f", "-", "-o", "name"]
    try:
        k(*base, "--dry-run=server", input_bytes=doc, timeout=120)
        applied = k(*base, input_bytes=doc, timeout=120).stdout.decode().split()
    except CommandError as exc:
        raise Stop("failed", "policy_apply_failed", Result.FAIL, {"error": exc.stderr}) from exc
    return Step(detail={"applied": applied, "namespace": namespace})
