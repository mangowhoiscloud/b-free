"""Server-side proposal check for ``plan_propose`` (spec §8.3 "제안서 스키마", "가드레일"; 13 6 "출력").

The model has no submit tool. The worker validates its structured output and calls
``plan_propose``; this module validates again, on the server, because the worker is not trusted:

1. schema: the per-role Pydantic model ``bravo_agents.schemas.OUTPUT_MODELS[role]`` (extra fields
   forbidden, role literal must match the Trial's role);
2. binding: ``plan_id``, ``trial_id`` and ``attempt`` equal the Trial's envelope, and every cited
   evidence ID is in the envelope (same rule as ``bravo_agents.runner.validate_output``);
3. policy lint: fixed rules in code (secret-like values, anonymous or wildcard grants,
   cluster-scoped RBAC, reserved names, destructive SQL outside ``db_rehearse``).

The accepted proposal is the artifact: frozen at submission and bound by ``digest`` (05 3
"분리와 전달"); nothing is copied from the sandbox afterwards.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

RESERVED_PREFIXES = ("bravo-", "rehearsal-sandbox", "verify-")  # names the provisioner owns (05 3)
SECRET_LIKE = re.compile(r"(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
                         r"|(?i:password|secret|token)\s*[:=]\s*['\"][^'\"]{6,})")
DESTRUCTIVE_SQL = re.compile(r"\b(DROP\s+(DATABASE|ROLE|USER)|ALTER\s+SYSTEM|COPY\s+.*\bPROGRAM\b|CREATE\s+ROLE"
                             r"|ALTER\s+ROLE|pg_terminate_backend|pg_read_server_files|lo_import)\b", re.I | re.S)
BUCKET_TARGETS = frozenset({"rgw_bucket_policy", "s3_bucket_policy"})


@dataclass(frozen=True, slots=True)
class Binding:
    plan_id: str
    trial_id: str
    attempt: int
    evidence_ids: frozenset[str] = field(default_factory=frozenset)


def digest(artifact: Any) -> str:
    """sha256 over canonical JSON (same form as ``bravo_agents.runner.digest``). platform/api must match."""
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else [v]


def _bucket_policy(doc: dict[str, Any]) -> list[str]:
    problems = []
    for st in _as_list(doc.get("Statement") or []):
        if not isinstance(st, dict):
            return ["bucket_policy_statement_not_object"]
        if st.get("Effect") != "Allow":
            continue
        principal = st.get("Principal")
        if principal == "*" or (isinstance(principal, dict) and "*" in _as_list(principal.get("AWS", []))):
            problems.append("anonymous_allow")
        if any(a == "*" or str(a).endswith(":*") for a in _as_list(st.get("Action") or [])):
            problems.append("wildcard_action")
    return problems


def _rbac(doc: dict[str, Any]) -> list[str]:
    problems = []
    for obj in _as_list(doc.get("items") or doc):
        if not isinstance(obj, dict):
            return ["rbac_not_object"]
        if obj.get("kind") in ("ClusterRole", "ClusterRoleBinding"):
            problems.append("cluster_scoped_rbac")
        name = str((obj.get("metadata") or {}).get("name", ""))
        if any(name.startswith(p) for p in RESERVED_PREFIXES):
            problems.append("reserved_name")
        for rule in obj.get("rules") or []:
            verbs, resources = set(rule.get("verbs") or []), set(rule.get("resources") or [])
            if "*" in verbs or "*" in resources:
                problems.append("wildcard_rbac")
            if "secrets" in resources and verbs & {"list", "watch"}:
                problems.append("secret_list_watch")
    return problems


def lint(p: Any) -> list[str]:
    """Forbidden patterns. Empty list = pass. Rules are fixed in code, not in a prompt."""
    problems: list[str] = []
    data = p.model_dump(mode="json")
    if SECRET_LIKE.search(json.dumps(data, ensure_ascii=False)):
        problems.append("secret_like_value")  # D-26: no secret values in proposals
    if data["role"] == "governance":
        for t in data["translations"]:
            doc, target = t["document"], t["target"]["type"]
            if target in BUCKET_TARGETS:
                problems += _bucket_policy(doc)
            elif target == "k8s_rbac":
                problems += _rbac(doc)
            if any(t["target"]["name"].startswith(prefix) for prefix in RESERVED_PREFIXES):
                problems.append("reserved_name")
    if data["role"] == "db":
        for plan in data["plans"]:
            if plan.get("sql") and DESTRUCTIVE_SQL.search(plan["sql"]):
                problems.append("destructive_sql")
    return sorted(set(problems))


def check(raw: Any, role: str, binding: Binding | None = None) -> tuple[Any | None, list[str]]:
    """① schema, ② binding, ③ lint. Returns (proposal, problems); proposal is None on schema error."""
    try:
        from bravo_agents.schemas import OUTPUT_MODELS, referenced_evidence
    except ImportError:
        return None, ["schema_unavailable"]
    model = OUTPUT_MODELS.get(role)
    if model is None:
        return None, ["role_unknown"]
    try:
        p = model.model_validate(raw)
    except ValidationError as exc:
        return None, [f"schema:{'.'.join(str(x) for x in e['loc'])}:{e['type']}" for e in exc.errors()][:20]
    problems: list[str] = []
    if binding is not None:
        for key in ("plan_id", "trial_id", "attempt"):
            if getattr(p, key) != getattr(binding, key):
                problems.append(f"binding_{key}")
        if referenced_evidence(p) - binding.evidence_ids:
            problems.append("unknown_evidence_ref")
    return p, problems + lint(p)
