"""Platform-held test of the bucket-policy verification Job (B7, B11/P14). Runs inside the Job Pod on the GATE image.

The questions are the gate's own code: ``bravo_gate.probes.objstore.ObjectStoreProbe`` OBJ-01..06, the same functions
the platform gate asks of the destination attachment bucket. Here they are asked of the rehearsal test bucket after the
proposed policy is applied, so a verifier PASS predicts the gate's PASS for the same policy. The service never imports
this module (it is shipped to the Pod in a ConfigMap); the gate image provides ``bravo_gate`` and boto3.

Steps, all against the rehearsal test bucket (never the destination's attachment bucket):

0. egress: TCP to the internet and the metadata address must fail (the Job's NetworkPolicy allows RGW only);
1. reset: delete any bucket policy left behind, then PUT this run's canary object as the owner;
2. control: a known-open policy (unsigned s3:GetObject on the canary) must make the gate's OBJ-01 FAIL with
   ``anonymous_read_allowed``. Otherwise this bucket cannot show an open policy and a later PASS proves nothing;
3. per proposed policy: PutBucketPolicy must be accepted, then OBJ-01..06 run with write probes on (OBJ-03 unsigned PUT,
   OBJ-04 owner HEAD + PUT, i.e. the app's write path); each must be PASS; then the policy is deleted;
4. cleanup: delete the policy and the canary.

The owner is the app's stand-in (the proposal's app principal is mapped to it), the other credential is an RGW user with
no grant. The last stdout line is one JSON object; the service reads it from the Pod log. Key values are never printed.
"""

from __future__ import annotations

import json
import os
import socket
import sys

QUESTIONS = ("obj01", "obj02", "obj03", "obj04", "obj05", "obj06")


def egress(targets: list) -> list[dict]:
    out = []
    for host, port in targets:
        try:
            socket.create_connection((host, int(port)), 3).close()
            out.append({"target": f"{host}:{port}", "blocked": False})
        except OSError as exc:
            out.append({"target": f"{host}:{port}", "blocked": True, "error": type(exc).__name__})
    return out


def _code(exc: BaseException) -> str:
    resp = getattr(exc, "response", None) or {}
    err = resp.get("Error", {}) if isinstance(resp, dict) else {}
    status = (resp.get("ResponseMetadata", {}) if isinstance(resp, dict) else {}).get("HTTPStatusCode", "")
    return f"{status} {err.get('Code', type(exc).__name__)}".strip()


def run(p: dict) -> dict:
    import boto3
    from botocore.config import Config
    from bravo_gate.environments import Credential, ObjectStoreSection
    from bravo_gate.probes.objstore import ObjectStoreProbe

    out: dict = {"run": p["run"], "bucket": p["bucket"], "egress": egress(p.get("egress_canaries") or []),
                 "questions": "bravo_gate.probes.objstore OBJ-01..06"}
    section = ObjectStoreSection(
        bucket=p["bucket"], endpoint_url=p["endpoint"], plain_http_endpoint_url=p.get("plain_endpoint"),
        addressing_style="path", ca_file="/cfg/ca.crt", canary_key=p["canary_key"], write_probes=True, timeout_s=5,
        app_credential=Credential(access_key_env="S3_ACCESS_KEY_ID", secret_key_env="S3_SECRET_ACCESS_KEY"),
        other_credential=(Credential(access_key_env="OTHER_ACCESS_KEY_ID", secret_key_env="OTHER_SECRET_ACCESS_KEY")
                          if os.environ.get("OTHER_ACCESS_KEY_ID") else None))
    owner = boto3.session.Session(aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
                                  aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"]).client(
        "s3", region_name="us-east-1", endpoint_url=p["endpoint"], verify="/cfg/ca.crt",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1, "mode": "standard"},
                      connect_timeout=5, read_timeout=5, request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"))
    bucket, key = p["bucket"], p["canary_key"]

    def call(fn, **kw) -> str:
        try:
            r = fn(Bucket=bucket, **kw)
            return str((r or {}).get("ResponseMetadata", {}).get("HTTPStatusCode", "ok"))
        except Exception as exc:  # noqa: BLE001 - recorded as the response
            return _code(exc)

    out["reset"] = call(owner.delete_bucket_policy)
    out["canary_put"] = call(owner.put_object, Key=key, Body=b"bravo rehearsal verification canary\n")
    if out["canary_put"] != "200":
        out["error"] = "canary_put_failed"
        return out
    c_put = call(owner.put_bucket_policy, Policy=json.dumps(p["control_policy"]))
    c_obs = ObjectStoreProbe(section, allow_writes=True).obj01()
    c_del = call(owner.delete_bucket_policy)
    out["control"] = {"policy_put": c_put, "gate_obj01": c_obs.as_dict(), "policy_delete": c_del,
                      "ok": c_put in ("200", "204") and c_obs.verdict == "FAIL"
                      and c_obs.reason == "anonymous_read_allowed"}
    results = []
    for item in p["policies"] if out["control"]["ok"] else []:
        put_p = call(owner.put_bucket_policy, Policy=json.dumps(item["policy"]))
        applied = put_p in ("200", "204")
        checks = [{"name": "policy_applied", "ok": applied, "detail": put_p}]
        if applied:
            probe = ObjectStoreProbe(section, allow_writes=True)  # fresh canary cache per policy
            for q in QUESTIONS:
                obs = getattr(probe, q)()
                cid = "OBJ-0" + q[-1]
                ev = obs.evidence or {}
                checks.append({"name": f"gate_{cid}", "ok": obs.verdict == "PASS",
                               "detail": f"{obs.verdict} {obs.reason} {ev.get('response', '')}".strip(),
                               "verdict": obs.verdict, "reason": obs.reason})
        results.append({"index": item["index"], "policy_delete": call(owner.delete_bucket_policy), "checks": checks})
    out["policies"] = results
    out["cleanup"] = {"policy_delete": call(owner.delete_bucket_policy),
                      "canary_delete": call(owner.delete_object, Key=key)}
    return out


def main() -> int:
    with open("/cfg/params.json", encoding="utf-8") as f:
        p = json.load(f)
    try:
        result = run(p)
    except Exception as exc:  # noqa: BLE001 - the service records the error code, never a pass
        result = {"run": p.get("run"), "error": f"job_exception:{type(exc).__name__}"}
    print(json.dumps(result, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
