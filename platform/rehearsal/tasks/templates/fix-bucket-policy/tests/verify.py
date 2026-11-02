"""Private verifier for fix-bucket-policy. Runs in the verify namespace as a fixed-template Job.

Input: the frozen artifact (``/input/artifact.json``) bound by digest. The agent never sees
this file. It does not import or execute anything from the artifact. Last stdout line is
JSON ``{"reward": 0|1, "checks": [...]}``; the controller reads it from the Pod log.
Scope (P0): the policy semantics against the source policy's intent. Applying the policy to
a live RGW and probing it with and without credentials is the next step (05 3 검증 규칙).
"""
import json
import sys

ALLOWED_ACTIONS = {"s3:GetObject", "s3:PutObject"}


def checks(a: dict) -> list[dict]:
    p = a.get("proposal") or {}
    m = a.get("manifest") or {}
    data = m.get("data") or {}
    try:
        doc = json.loads(data.get("policy.json", ""))
    except ValueError:
        doc = None
    acts = p.get("actions") or []
    return [
        {"name": "target_is_rgw", "ok": (a.get("target") or {}).get("type") == "rgw_bucket_policy"},
        {"name": "anonymous_denied", "ok": p.get("anonymous") == "deny"},
        {"name": "principal_is_app", "ok": p.get("principal") == "chat-app"},
        {"name": "actions_least_privilege", "ok": bool(acts) and set(acts) <= ALLOWED_ACTIONS},
        {"name": "read_and_write_kept", "ok": set(acts) == ALLOWED_ACTIONS},
        {"name": "manifest_is_configmap", "ok": m.get("apiVersion") == "v1" and m.get("kind") == "ConfigMap"},
        {"name": "manifest_matches_proposal", "ok": doc == p},
    ]


def main(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as f:
            artifact = json.load(f)
        result = checks(artifact)
    except (OSError, ValueError) as exc:
        result = [{"name": "artifact_readable", "ok": False, "error": type(exc).__name__}]
    reward = 1 if result and all(c["ok"] for c in result) else 0
    print(json.dumps({"reward": reward, "checks": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
